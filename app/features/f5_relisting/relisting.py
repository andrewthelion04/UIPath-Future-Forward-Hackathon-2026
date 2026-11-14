"""F5 — Relistare după anulare: anulare -> comparabile -> preț propus -> aprobare -> calendar.

Prețul se calculează în cod din datele adaptorului de piață (nu de către model), iar limita
de reducere din regulile hostului se impune tot în cod.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import SettingsConfigDict

from app.core.clock import utcnow
from app.core.contracts.events import ApprovalDecided, BookingCancelled
from app.core.contracts.models import ApprovalOption, CalendarDay, ComparableQuery
from app.core.db import from_json, to_json
from app.core.feature_context import FeatureContext, FeatureSettings
from app.core.ids import new_id
from app.features.f5_relisting.pricing import (
    NightlyPrice, cap_price, cheapest_comparable, distribute_prices, floor_price,
    is_comparable, propose_price, validate_calendar,
)

logger = logging.getLogger("staypilot.f5")

APPROVAL_KIND = "relist_price"
ProposalStatus = Literal[
    "awaiting_approval", "applied", "rejected", "failed", "no_comparable", "conflict", "expired"
]


class F5Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F5_", extra="ignore", env_ignore_empty=True)

    undercut_pct: float = Field(default=3.0, ge=0, lt=100, allow_inf_nan=False)
    market_timeout_s: float = Field(default=20.0, gt=0, allow_inf_nan=False)
    calendar_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    timezone: str = "Europe/Bucharest"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class RelistProposal(BaseModel):
    id: str
    booking_id: str
    property_id: str
    start_date: date
    end_date: date
    base_price: float
    floor_price: float
    comparable_ref: str | None = None
    comparable_title: str | None = None
    comparable_price: float | None = None
    proposed_price: float | None = None
    nightly_prices: list[NightlyPrice] = Field(default_factory=list)
    expires_at: datetime | None = None
    currency: str = "EUR"
    market_source: str | None = None
    market_attribution: str | None = None
    status: ProposalStatus
    note: str | None = None
    approval_id: str | None = None
    created_at: datetime
    updated_at: datetime


# --- Serviciul -----------------------------------------------------------------------


class Relisting:
    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx
        self._settings = ctx.load_settings(F5Settings)
        self._timezone = ZoneInfo(self._settings.timezone)
        self._locks: dict[str, asyncio.Lock] = {}
        # Migrare aditivă locală: păstrăm propunerile și restul lumii existente.
        columns = {r["name"] for r in ctx.db.query("PRAGMA table_info(f5_relist_proposals)")}
        for name, definition in (
            ("nightly_prices", "TEXT NOT NULL DEFAULT '[]'"), ("expires_at", "TEXT")
        ):
            if name not in columns:
                ctx.db.execute(f"ALTER TABLE f5_relist_proposals ADD COLUMN {name} {definition}")

    def _now(self) -> datetime:
        local = self._ctx.clock.now().astimezone(self._timezone)
        if self._ctx.settings.fixed_today is None:
            return local
        # Doar FIXED_TODAY înlocuiește data; în producție folosim data proprietății.
        return datetime.combine(self._ctx.clock.today(), local.time(), self._timezone)

    # --- Anulare -> propunere ------------------------------------------------------

    async def on_booking_cancelled(self, event: BookingCancelled) -> None:
        async with self._locks.setdefault(event.property_id, asyncio.Lock()):
            await self._propose(event)

    async def _propose(self, event: BookingCancelled) -> None:
        ctx = self._ctx
        if ctx.db.query_one("SELECT id FROM f5_relist_proposals WHERE booking_id = ?", (event.booking_id,)):
            return
        prop = ctx.world.get_property(event.property_id)
        booking = ctx.world.get_booking(event.booking_id)
        if prop is None or booking is None or booking.property_id != prop.id or booking.status != "cancelled":
            return
        rules = ctx.world.host_rules()
        today = self._now().date()
        start, end = max(event.check_in, today), event.check_out
        days_until = (event.check_in - today).days

        if end <= start:
            return  # sejurul a trecut deja; nu mai e nimic de relistat
        if days_until > rules.last_minute_window_days:
            ctx.notify(
                f"Cancellation at {prop.name}: not last-minute",
                f"{booking.guest_name} cancelled {event.check_in} to {event.check_out} "
                f"({days_until} days ahead). The nights are open again at the regular price; "
                f"relisting is proposed only within {rules.last_minute_window_days} days of check-in.",
                property_id=prop.id,
                booking_id=booking.id,
            )
            return

        proposal = RelistProposal(
            id=new_id("rel"),
            booking_id=booking.id,
            property_id=prop.id,
            start_date=start,
            end_date=end,
            base_price=prop.base_price,
            floor_price=0,
            expires_at=datetime.combine(
                event.check_in, time.fromisoformat(rules.check_in_time), self._timezone
            ),
            currency=prop.currency,
            market_source=ctx.market.name,
            market_attribution=ctx.market.attribution,
            status="awaiting_approval",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        if self._now() >= proposal.expires_at:
            self._insert(proposal.model_copy(update={"status": "expired"}))
            self._notify_expired(proposal)
            return

        try:
            floor = floor_price(prop.base_price, rules.max_relist_discount_pct)
            days = await self._read_calendar(proposal)
            if not all(d.available for d in days):
                self._insert(proposal)
                self._stop(proposal, "conflict", "Some nights are already booked or blocked.")
                return
        except Exception as exc:
            self._insert(proposal)
            self._stop(proposal, "failed", f"Calendar or pricing data unavailable: {exc}")
            return
        proposal = proposal.model_copy(update={"floor_price": floor})

        try:
            candidates = await asyncio.wait_for(
                ctx.market.find_comparables(
                    ComparableQuery(
                        city=prop.city,
                        bedrooms=prop.bedrooms,
                        max_guests=prop.max_guests,
                        amenities=prop.amenities,
                        check_in=start,
                        check_out=end,
                    )
                ),
                timeout=self._settings.market_timeout_s,
            )
        except Exception as exc:  # fallback explicit: fără date de piață nu propunem preț
            problem = (
                f"timeout after {self._settings.market_timeout_s:g}s"
                if isinstance(exc, TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            self._insert(proposal.model_copy(update={"status": "failed", "note": problem}))
            ctx.notify(
                f"Last-minute cancellation at {prop.name}: market data unavailable",
                f"{booking.guest_name} cancelled {start} to {end}. Comparable listings could not "
                f"be fetched ({problem}), so no price is proposed. Relist manually; your regular "
                f"price is {prop.base_price:g} {prop.currency}.",
                level="warning",
                property_id=prop.id,
                booking_id=booking.id,
            )
            return

        comparable = cheapest_comparable(candidates, prop)
        if comparable is None:
            self._insert(
                proposal.model_copy(
                    update={"status": "no_comparable", "note": f"{len(candidates)} candidates, none comparable"}
                )
            )
            ctx.notify(
                f"Last-minute cancellation at {prop.name}: no comparable listing",
                f"{booking.guest_name} cancelled {start} to {end}. No listing with the same "
                f"amenities was found among {len(candidates)} candidates, so no price is "
                f"proposed. Your regular price is {prop.base_price:g} {prop.currency}.",
                level="warning",
                property_id=prop.id,
                booking_id=booking.id,
            )
            return

        price, clamped = propose_price(comparable.nightly_price, self._settings.undercut_pct, floor)
        price = cap_price(price, prop.base_price)
        nightly = distribute_prices(
            days, prop.base_price, comparable.nightly_price,
            self._settings.undercut_pct, rules.max_relist_discount_pct,
        )
        note = (
            f"Limited by your maximum relisting discount of {rules.max_relist_discount_pct:g}%."
            if clamped
            else None
        )
        proposal = proposal.model_copy(
            update={
                "comparable_ref": comparable.ref,
                "comparable_title": comparable.title,
                "comparable_price": comparable.nightly_price,
                "proposed_price": price,
                "nightly_prices": nightly,
                "note": note,
            }
        )
        self._insert(proposal)
        if self._expire(proposal):
            return

        price_label = self._price_label(proposal)

        summary = [
            f"{booking.guest_name} cancelled {start} to {end}, {days_until} day(s) before check-in.",
            f"Cheapest comparable listing with the same amenities: {comparable.title} at "
            f"{comparable.nightly_price:g} {comparable.currency}/night (source: {comparable.source}).",
            f"Nightly prices: {price_label}. Each night stays within its host floor and regular price.",
            *[f"{n.day}: {n.original_price:g} → {n.proposed_price:g} {prop.currency} "
              f"(floor {n.floor_price:g})." for n in nightly],
            f"Expires at check-in: {proposal.expires_at.isoformat()}.",
        ]
        if proposal.market_attribution:
            summary.append(proposal.market_attribution)
        if note:
            summary.append(note)
        approval = await ctx.approvals.request(
            feature=ctx.feature,
            kind=APPROVAL_KIND,
            title=f"Relist {prop.name} at {price_label}?",
            summary="\n".join(summary),
            options=[ApprovalOption(key="approve", label=f"Relist at {price_label}")],
            payload={"proposal_id": proposal.id},
            booking_id=booking.id,
            property_id=prop.id,
            dedupe_key=f"{ctx.feature}:relist:{booking.id}",
        )
        self._update(proposal.id, approval_id=approval.id)

    # --- Decizie -> calendar -------------------------------------------------------

    async def on_decision(self, event: ApprovalDecided) -> None:
        proposal = self.get(str(event.payload.get("proposal_id")))
        if proposal is None:
            return
        async with self._locks.setdefault(proposal.property_id, asyncio.Lock()):
            # Lockul comun al calendarului: verificarea și scrierea prețurilor nu se
            # intercalează cu alt feature care scrie aceleași nopți (ex. o relocare F2).
            async with self._ctx.calendar.lock(proposal.property_id):
                await self._apply_decision(event)

    async def _apply_decision(self, event: ApprovalDecided) -> None:
        ctx = self._ctx
        proposal = self.get(str(event.payload.get("proposal_id")))
        if (proposal is None or proposal.status != "awaiting_approval"
                or proposal.approval_id != event.approval_id):
            return
        prop = ctx.world.get_property(proposal.property_id)
        name = prop.name if prop else proposal.property_id
        span = f"{proposal.start_date} to {proposal.end_date}"

        if event.decision == "rejected":
            self._update(proposal.id, status="rejected")
            ctx.notify(
                f"Relisting of {name} rejected",
                f"The price for {span} was left unchanged.",
                property_id=proposal.property_id,
                booking_id=proposal.booking_id,
            )
            return

        if not proposal.nightly_prices or proposal.expires_at is None:
            self._stop(proposal, "failed", "This older proposal has no nightly snapshot. Relist manually.")
            return
        # Limita de bani a hostului se aplică la momentul scrierii, nu doar la propunere.
        limit = ctx.world.host_rules().max_relist_discount_pct
        try:
            below = [
                night for night in proposal.nightly_prices
                if night.proposed_price < floor_price(night.original_price, limit)
            ]
        except ValueError as exc:
            self._stop(proposal, "failed", f"Pricing rules are invalid: {exc}. No changes were made.")
            return
        if below:
            self._stop(proposal, "conflict", (
                f"Your maximum relisting discount is now {limit:g}%, and "
                f"{len(below)} proposed night(s) go below it. No changes were made."
            ))
            return
        try:
            days = await self._read_calendar(proposal)
        except Exception as exc:
            self._stop(proposal, "failed", f"Calendar verification failed: {exc}. No changes were made.")
            return
        if self._expire(proposal):
            return
        if any(not day.available for day in days):
            self._stop(proposal, "conflict", "Some nights were booked or blocked after this proposal. No changes were made.")
            return
        if ([day.day for day in days] != [n.day for n in proposal.nightly_prices]
                or any(day.price != n.original_price for day, n in zip(days, proposal.nightly_prices))):
            self._stop(proposal, "conflict", "Regular prices changed after this proposal. Review prices manually. No changes were made.")
            return

        applied: list[str] = []
        try:
            # Nopțile sunt deja libere; nu redeschidem disponibilitatea redundant.
            for night in proposal.nightly_prices:
                if self._expire(proposal):
                    return
                await asyncio.wait_for(
                    ctx.calendar.set_price(
                        proposal.property_id, night.day, night.day + timedelta(days=1), night.proposed_price
                    ), timeout=self._settings.calendar_timeout_s,
                )
                applied.append(str(night.day))
        except asyncio.CancelledError:
            # Handlerul a fost oprit (timeoutul event bus-ului sau shutdown) la jumătate:
            # aprobarea e deja consumată, deci consemnăm starea și progresul înainte să ieșim.
            self._stop(proposal, "failed", (
                "The price update was interrupted before it finished. "
                f"Confirmed updated nights: {', '.join(applied) or 'none'}. "
                "The interrupted call may have completed remotely; verify all nights in your PMS."
            ))
            raise
        except Exception as exc:
            # Portul nu oferă tranzacții; un rollback ar putea suprascrie o rezervare nouă.
            self._stop(proposal, "failed", (
                f"Price update failed: {type(exc).__name__}: {exc}. "
                f"Confirmed updated nights: {', '.join(applied) or 'none'}. "
                "The failing call may have completed remotely; verify all nights in your PMS."
            ))
            return
        self._update(proposal.id, status="applied")
        ctx.notify(
            f"{name} relisted at {self._price_label(proposal)}",
            "Applied approved nightly prices: " + self._schedule(proposal),
            level="success",
            property_id=proposal.property_id,
            booking_id=proposal.booking_id,
        )

    async def _read_calendar(self, proposal: RelistProposal) -> list[CalendarDay]:
        try:
            days = await asyncio.wait_for(
                self._ctx.calendar.get_days(proposal.property_id, proposal.start_date, proposal.end_date),
                timeout=self._settings.calendar_timeout_s,
            )
        except TimeoutError as exc:
            raise TimeoutError(f"Calendar timed out after {self._settings.calendar_timeout_s:g}s") from exc
        return validate_calendar(
            days, proposal.property_id, proposal.start_date, proposal.end_date, proposal.currency
        )

    @staticmethod
    def _price_label(proposal: RelistProposal) -> str:
        prices = [n.proposed_price for n in proposal.nightly_prices]
        if not prices:
            return "unavailable"
        low, high = min(prices), max(prices)
        amount = f"{low:g}" if low == high else f"{low:g}–{high:g}"
        return f"{amount} {proposal.currency}/night"

    @staticmethod
    def _schedule(proposal: RelistProposal) -> str:
        return "; ".join(f"{n.day}: {n.proposed_price:g} {proposal.currency}" for n in proposal.nightly_prices)

    def _stop(self, proposal: RelistProposal, status: ProposalStatus, problem: str) -> None:
        self._update(proposal.id, status=status, note=problem)
        prop = self._ctx.world.get_property(proposal.property_id)
        name = prop.name if prop else proposal.property_id
        self._ctx.notify(
            f"Relisting of {name} could not be applied" if status == "failed" else f"Relisting of {name}: calendar conflict",
            problem + " Review and relist manually in your PMS. " + self._schedule(proposal),
            level="warning", property_id=proposal.property_id, booking_id=proposal.booking_id,
        )

    def _notify_expired(self, proposal: RelistProposal) -> None:
        self._ctx.notify(
            "Relisting proposal expired",
            "Check-in has been reached. This proposal can no longer change the calendar. "
            "Review any previously applied nightly prices in your PMS.",
            level="warning", property_id=proposal.property_id, booking_id=proposal.booking_id,
        )

    def _expire(self, proposal: RelistProposal) -> bool:
        if proposal.status != "awaiting_approval":
            return proposal.status == "expired"
        deadline = proposal.expires_at or datetime.combine(proposal.start_date, time.min, self._timezone)
        try:  # ora de check-in mutată mai devreme după propunere scurtează termenul
            current = datetime.combine(
                deadline.astimezone(self._timezone).date(),
                time.fromisoformat(self._ctx.world.host_rules().check_in_time),
                self._timezone,
            )
            deadline = min(deadline, current)
        except ValueError:
            pass  # oră invalidă în reguli: rămâne termenul salvat
        if self._now() < deadline:
            return False
        changed = self._ctx.db.execute(
            "UPDATE f5_relist_proposals SET status = 'expired', note = ?, updated_at = ? "
            "WHERE id = ? AND status = 'awaiting_approval'",
            ("Check-in reached; approval can no longer change the calendar.", utcnow().isoformat(), proposal.id),
        )
        if changed.rowcount:
            if proposal.approval_id:  # nu lăsăm în inbox o aprobare care nu mai poate face nimic
                self._ctx.approvals.withdraw(proposal.approval_id, reason="Check-in reached.")
            self._notify_expired(proposal)
        return True

    # --- Persistență ---------------------------------------------------------------

    def _insert(self, proposal: RelistProposal) -> None:
        data = proposal.model_dump(mode="json")
        data["nightly_prices"] = to_json(data["nightly_prices"])
        columns = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        self._ctx.db.execute(
            f"INSERT INTO f5_relist_proposals ({columns}) VALUES ({marks})", tuple(data.values())
        )

    def _update(self, proposal_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._ctx.db.execute(
            f"UPDATE f5_relist_proposals SET {assignments} WHERE id = ?",
            (*fields.values(), proposal_id),
        )

    def get(self, proposal_id: str) -> RelistProposal | None:
        row = self._ctx.db.query_one(
            "SELECT * FROM f5_relist_proposals WHERE id = ?", (proposal_id,)
        )
        if row is None:
            return None
        proposal = self._from_row(row)
        if self._expire(proposal):
            return proposal.model_copy(update={"status": "expired", "note": "Check-in reached; approval can no longer change the calendar."})
        return proposal

    def list(self, limit: int = 50) -> list[RelistProposal]:
        rows = self._ctx.db.query(
            "SELECT * FROM f5_relist_proposals ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return [proposal for row in rows if (proposal := self.get(row["id"])) is not None]

    @staticmethod
    def _from_row(row: dict[str, Any]) -> RelistProposal:
        return RelistProposal(**{**row, "nightly_prices": from_json(row.get("nightly_prices"), [])})
