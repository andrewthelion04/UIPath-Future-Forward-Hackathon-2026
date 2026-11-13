"""F5 — Relistare după anulare: anulare -> comparabile -> preț propus -> aprobare -> calendar.

Prețul se calculează în cod din datele adaptorului de piață (nu de către model), iar limita
de reducere din regulile hostului se impune tot în cod.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from app.core.clock import utcnow
from app.core.contracts.events import ApprovalDecided, BookingCancelled
from app.core.contracts.models import ApprovalOption, Comparable, ComparableQuery, Property
from app.core.feature_context import FeatureContext, FeatureSettings
from app.core.ids import new_id
from app.ports.pms import CapabilityNotSupported

logger = logging.getLogger("staypilot.f5")

APPROVAL_KIND = "relist_price"
ProposalStatus = Literal["awaiting_approval", "applied", "rejected", "failed", "no_comparable"]


class F5Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F5_", extra="ignore", env_ignore_empty=True)

    undercut_pct: float = 3.0  # cu cât coborâm sub cel mai ieftin comparabil
    market_timeout_s: float = 20.0


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
    currency: str = "EUR"
    market_source: str | None = None
    market_attribution: str | None = None
    status: ProposalStatus
    note: str | None = None
    approval_id: str | None = None
    created_at: datetime
    updated_at: datetime


# --- Regulile de business, funcții pure ----------------------------------------------


def is_comparable(candidate: Comparable, prop: Property) -> bool:
    """Comparabil = aceleași facilități (cel puțin), același număr de dormitoare, loc suficient."""
    return (
        set(prop.amenities) <= set(candidate.amenities)
        and candidate.bedrooms == prop.bedrooms
        and candidate.max_guests >= prop.max_guests
    )


def cheapest_comparable(candidates: list[Comparable], prop: Property) -> Comparable | None:
    matching = [c for c in candidates if is_comparable(c, prop) and c.currency == prop.currency]
    return min(matching, key=lambda c: (c.nightly_price, c.ref)) if matching else None


def floor_price(base_price: float, max_discount_pct: float) -> float:
    return round(base_price * (1 - max_discount_pct / 100), 2)


def propose_price(comparable_price: float, undercut_pct: float, floor: float) -> tuple[float, bool]:
    """Puțin sub cel mai ieftin comparabil, dar niciodată sub pragul hostului. -> (preț, limitat)."""
    price = round(comparable_price * (1 - undercut_pct / 100), 2)
    return (floor, True) if price < floor else (price, False)


# --- Serviciul -----------------------------------------------------------------------


class Relisting:
    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx
        self._settings = ctx.load_settings(F5Settings)

    # --- Anulare -> propunere ------------------------------------------------------

    async def on_booking_cancelled(self, event: BookingCancelled) -> None:
        ctx = self._ctx
        prop = ctx.world.get_property(event.property_id)
        booking = ctx.world.get_booking(event.booking_id)
        if prop is None or booking is None:
            return
        rules = ctx.world.host_rules()
        today = ctx.clock.today()
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

        floor = floor_price(prop.base_price, rules.max_relist_discount_pct)
        proposal = RelistProposal(
            id=new_id("rel"),
            booking_id=booking.id,
            property_id=prop.id,
            start_date=start,
            end_date=end,
            base_price=prop.base_price,
            floor_price=floor,
            currency=prop.currency,
            market_source=ctx.market.name,
            market_attribution=ctx.market.attribution,
            status="awaiting_approval",
            created_at=utcnow(),
            updated_at=utcnow(),
        )

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
                "note": note,
            }
        )
        self._insert(proposal)

        summary = [
            f"{booking.guest_name} cancelled {start} to {end}, {days_until} day(s) before check-in.",
            f"Cheapest comparable listing with the same amenities: {comparable.title} at "
            f"{comparable.nightly_price:g} {comparable.currency}/night (source: {comparable.source}).",
            f"Proposed price: {price:g} {prop.currency}/night. Your regular price is "
            f"{prop.base_price:g}; your floor is {floor:g}.",
        ]
        if note:
            summary.append(note)
        approval = await ctx.approvals.request(
            feature=ctx.feature,
            kind=APPROVAL_KIND,
            title=f"Relist {prop.name} at {price:g} {prop.currency}/night?",
            summary="\n".join(summary),
            options=[ApprovalOption(key="approve", label=f"Relist at {price:g} {prop.currency}")],
            payload={"proposal_id": proposal.id},
            booking_id=booking.id,
            property_id=prop.id,
            dedupe_key=f"{ctx.feature}:relist:{booking.id}",
        )
        self._update(proposal.id, approval_id=approval.id)

    # --- Decizie -> calendar -------------------------------------------------------

    async def on_decision(self, event: ApprovalDecided) -> None:
        ctx = self._ctx
        proposal = self.get(str(event.payload.get("proposal_id")))
        if proposal is None or proposal.proposed_price is None:
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

        problems: list[str] = []
        try:
            await ctx.calendar.set_availability(
                proposal.property_id, proposal.start_date, proposal.end_date, True
            )
        except CapabilityNotSupported:
            # La un PMS real, nopțile se redeschid singure când rezervarea e anulată.
            problems.append("availability could not be set through the PMS; check it is open")
        except Exception as exc:
            problems.append(f"availability update failed: {exc}")
        try:
            await ctx.calendar.set_price(
                proposal.property_id, proposal.start_date, proposal.end_date, proposal.proposed_price
            )
        except Exception as exc:
            problems.append(f"price update failed: {exc}")
            self._update(proposal.id, status="failed", note="; ".join(problems))
            ctx.notify(
                f"Relisting of {name} could not be applied",
                f"Set {proposal.proposed_price:g} {proposal.currency}/night for {span} manually "
                f"in your PMS. Details: {'; '.join(problems)}.",
                level="warning",
                property_id=proposal.property_id,
                booking_id=proposal.booking_id,
            )
            return

        self._update(proposal.id, status="applied", note="; ".join(problems) or proposal.note)
        ctx.notify(
            f"{name} relisted at {proposal.proposed_price:g} {proposal.currency}/night",
            f"Nights {span} are open at the new price."
            + (f" Note: {'; '.join(problems)}." if problems else ""),
            level="success",
            property_id=proposal.property_id,
            booking_id=proposal.booking_id,
        )

    # --- Persistență ---------------------------------------------------------------

    def _insert(self, proposal: RelistProposal) -> None:
        data = proposal.model_dump(mode="json")
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
        return RelistProposal(**row) if row else None

    def list(self, limit: int = 50) -> list[RelistProposal]:
        rows = self._ctx.db.query(
            "SELECT * FROM f5_relist_proposals ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return [RelistProposal(**row) for row in rows]
