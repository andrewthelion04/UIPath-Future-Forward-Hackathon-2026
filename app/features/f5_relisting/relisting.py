"""F5 — Relistare după anulare: anulare -> comparabile -> preț propus -> aprobare -> calendar.

Prețul se calculează în cod din datele adaptorului de piață (nu de către model), iar limita
de reducere din regulile hostului se impune tot în cod.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import SettingsConfigDict

from app.core.agent.loop import AgentSpec
from app.core.agent.tools import RunContext
from app.core.approvals import ApprovalNotFound
from app.core.clock import utcnow
from app.core.contracts.events import ApprovalDecided, BookingCancelled
from app.core.contracts.models import ApprovalOption, CalendarDay, ComparableQuery
from app.core.db import from_json, to_json
from app.core.feature_context import FeatureContext, FeatureSettings
from app.core.ids import new_id
from app.features.f5_relisting.agents import AGENT_NAME, SYSTEM_PROMPT, build_tools
from app.features.f5_relisting.mock_llm import policy
from app.features.f5_relisting.pricing import (
    NightlyPrice, cap_price, cheapest_comparable, distribute_prices, floor_price,
    is_comparable, propose_price, validate_calendar,
)

APPROVAL_KIND = "relist_price"
ProposalStatus = Literal[
    "awaiting_approval", "applied", "rejected", "failed", "no_comparable", "conflict", "expired"
]


class F5Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F5_", extra="ignore", env_ignore_empty=True)

    undercut_pct: float = Field(default=3.0, ge=0, lt=100, allow_inf_nan=False)
    market_timeout_s: float = Field(default=20.0, gt=0, allow_inf_nan=False)
    calendar_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    last_minute_window_hours: float | None = Field(default=None, gt=0, le=8760, allow_inf_nan=False)
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
        self.spec = AgentSpec(
            name=AGENT_NAME,
            feature=ctx.feature,
            system_prompt=SYSTEM_PROMPT,
            tools=build_tools(self),
        )
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
            if self._ctx.db.query_one(
                "SELECT id FROM f5_relist_proposals WHERE booking_id = ?", (event.booking_id,)
            ):
                return
            state: dict[str, Any] = {"event": event}
            result = await self._ctx.agent.run(
                self.spec,
                objective=(
                    f"Review the cancellation of booking {event.booking_id} at property "
                    f"{event.property_id}, then prepare a safe relisting proposal if eligible."
                ),
                state=state,
                booking_id=event.booking_id,
                property_id=event.property_id,
            )
            terminal_outcomes = {
                "invalid_cancellation", "stay_has_ended", "expired", "not_last_minute",
                "calendar_conflict", "calendar_unavailable", "market_unavailable",
                "no_comparable", "approval_failed", "proposal_created",
            }
            if result.status != "finished" or state.get("outcome") not in terminal_outcomes:
                reason = result.error or result.result or result.status
                self._ctx.notify(
                    "F5 relisting agent did not complete",
                    f"Booking {event.booking_id}: {result.status} ({reason}). "
                    "No calendar price was changed by the agent; review the cancellation "
                    "and any pending proposal manually.",
                    level="warning",
                    booking_id=event.booking_id,
                    property_id=event.property_id,
                )

    async def _agent_inspect_cancellation(
        self, event: BookingCancelled, run: RunContext
    ) -> dict[str, Any]:
        if "inspection_result" in run.state:
            return run.state["inspection_result"]

        def remember(result: dict[str, Any]) -> dict[str, Any]:
            run.state["inspection_result"] = result
            return result

        ctx = self._ctx
        prop = ctx.world.get_property(event.property_id)
        booking = ctx.world.get_booking(event.booking_id)
        if prop is None or booking is None or booking.property_id != prop.id or booking.status != "cancelled":
            run.state["outcome"] = "invalid_cancellation"
            return remember({
                "status": "invalid_cancellation",
                "detail": "The booking is no longer a valid cancellation.",
            })

        rules = ctx.world.host_rules()
        now = self._now()
        today = now.date()
        start, end = max(event.check_in, today), event.check_out
        if end <= start:
            run.state["outcome"] = "stay_has_ended"
            return remember({"status": "stay_has_ended", "detail": "No future nights remain to relist."})

        deadline = datetime.combine(
            event.check_in, time.fromisoformat(rules.check_in_time), self._timezone
        )
        remaining_hours = (deadline - now).total_seconds() / 3600
        window_hours = (
            self._settings.last_minute_window_hours
            if self._settings.last_minute_window_hours is not None
            else rules.last_minute_window_days * 24
        )
        proposal = RelistProposal(
            id=new_id("rel"),
            booking_id=booking.id,
            property_id=prop.id,
            start_date=start,
            end_date=end,
            base_price=prop.base_price,
            floor_price=0,
            expires_at=deadline,
            currency=prop.currency,
            market_source=ctx.market.name,
            market_attribution=ctx.market.attribution,
            status="awaiting_approval",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        if remaining_hours <= 0:
            self._insert(proposal.model_copy(update={"status": "expired"}))
            self._notify_expired(proposal)
            run.state["outcome"] = "expired"
            return remember({"status": "expired", "detail": "The host's check-in time has passed."})

        if remaining_hours > window_hours:
            ctx.notify(
                f"Cancellation at {prop.name}: not last-minute",
                f"{booking.guest_name} cancelled {event.check_in} to {event.check_out}. "
                f"Check-in is in {remaining_hours:.1f} hours, outside the configured "
                f"{window_hours:g}-hour relisting window. The nights remain at the regular price.",
                property_id=prop.id,
                booking_id=booking.id,
            )
            run.state["outcome"] = "not_last_minute"
            return remember({"status": "not_last_minute", "remaining_hours": remaining_hours})

        try:
            floor = floor_price(prop.base_price, rules.max_relist_discount_pct)
            days = await self._read_calendar(proposal)
            if not all(d.available for d in days):
                self._insert(proposal)
                self._stop(proposal, "conflict", "Some nights are already booked or blocked.")
                run.state["outcome"] = "calendar_conflict"
                return remember({"status": "calendar_conflict", "detail": "Some nights are not available."})
        except Exception as exc:
            self._insert(proposal)
            self._stop(proposal, "failed", f"Calendar or pricing data unavailable: {exc}")
            run.state["outcome"] = "calendar_unavailable"
            return remember({
                "status": "calendar_unavailable",
                "detail": f"{type(exc).__name__}: {exc}",
            })
        proposal = proposal.model_copy(update={"floor_price": floor})
        run.state["context"] = {
            "booking": booking,
            "property": prop,
            "rules": rules,
            "proposal": proposal,
            "days": days,
        }
        run.state["outcome"] = "eligible"
        ctx.notify(
            f"F5 agent started relisting review for {prop.name}",
            f"Cancellation for {booking.guest_name} received. Check-in is in "
            f"{remaining_hours:.1f} hours; the released nights are available. "
            f"The agent is searching {ctx.market.name} market data before proposing any price.",
            level="info",
            property_id=prop.id,
            booking_id=booking.id,
        )
        return remember({
            "status": "eligible",
            "property": prop.name,
            "guest": booking.guest_name,
            "check_in": event.check_in.isoformat(),
            "check_out": event.check_out.isoformat(),
            "remaining_hours": round(remaining_hours, 2),
            "window_hours": window_hours,
            "regular_price": prop.base_price,
            "currency": prop.currency,
            "market_source": ctx.market.name,
        })

    async def _agent_search_comparables(self, run: RunContext) -> dict[str, Any]:
        ctx = self._ctx
        state = run.state
        context = state.get("context")
        if context is None:
            raise ValueError("Inspect an eligible cancellation before searching the market.")
        if "search_result" in state:
            return state["search_result"]
        prop: Any = context["property"]
        proposal: RelistProposal = context["proposal"]
        booking: Any = context["booking"]
        try:
            candidates = await asyncio.wait_for(
                ctx.market.find_comparables(
                    ComparableQuery(
                        city=prop.city,
                        bedrooms=prop.bedrooms,
                        max_guests=prop.max_guests,
                        amenities=prop.amenities,
                        check_in=proposal.start_date,
                        check_out=proposal.end_date,
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
            failed_proposal = proposal.model_copy(update={"status": "failed", "note": problem})
            self._insert(failed_proposal)
            ctx.notify(
                f"Last-minute cancellation at {prop.name}: market data unavailable",
                f"{booking.guest_name} cancelled {proposal.start_date} to {proposal.end_date}. "
                f"Comparable listings could not "
                f"be fetched ({problem}), so no price is proposed. Relist manually; your regular "
                f"price is {prop.base_price:g} {prop.currency}.",
                level="warning",
                property_id=prop.id,
                booking_id=booking.id,
            )
            state["outcome"] = "market_unavailable"
            result = {"status": "market_unavailable", "error": problem}
            state["search_result"] = result
            return result

        matching = [candidate for candidate in candidates if is_comparable(candidate, prop)]
        if not matching:
            no_match = proposal.model_copy(
                update={
                    "status": "no_comparable",
                    "note": f"{len(candidates)} candidates, none comparable",
                }
            )
            self._insert(no_match)
            ctx.notify(
                f"Last-minute cancellation at {prop.name}: no comparable listing",
                f"{booking.guest_name} cancelled {proposal.start_date} to {proposal.end_date}. "
                f"No listing with the same "
                f"amenities was found among {len(candidates)} candidates, so no price is "
                f"proposed. Your regular price is {prop.base_price:g} {prop.currency}.",
                level="warning",
                property_id=prop.id,
                booking_id=booking.id,
            )
            state["outcome"] = "no_comparable"
            result = {"status": "no_comparable", "candidate_count": len(candidates)}
            state["search_result"] = result
            return result

        matching.sort(key=lambda candidate: (candidate.nightly_price, candidate.ref))
        state["comparables"] = matching
        result = {
            "status": "ready",
            "candidate_count": len(candidates),
            "comparable_count": len(matching),
            "comparables": [
                {
                    "ref": candidate.ref,
                    "title": candidate.title[:120],
                    "nightly_price": candidate.nightly_price,
                    "currency": candidate.currency,
                }
                # Lista completă rămâne în stare; modelul are nevoie doar de minimul validat.
                for candidate in matching[:1]
            ],
        }
        state["search_result"] = result
        state["outcome"] = "comparables_ready"
        ctx.notify(
            f"F5 market search completed for {prop.name}",
            f"Checked {len(candidates)} market listing(s); {len(matching)} match the "
            f"property's bedrooms, capacity, amenities, and currency. "
            f"The agent is evaluating the eligible listings.",
            level="info",
            property_id=prop.id,
            booking_id=booking.id,
        )
        return result

    async def _agent_prepare_relisting(self, comparable_ref: str, run: RunContext) -> dict[str, Any]:
        ctx = self._ctx
        state = run.state
        if "proposal_result" in state:
            return state["proposal_result"]
        context = state.get("context")
        candidates = state.get("comparables")
        if context is None or candidates is None:
            raise ValueError("Inspect the cancellation and search for comparables first.")
        prop: Any = context["property"]
        booking: Any = context["booking"]
        rules: Any = context["rules"]
        proposal: RelistProposal = context["proposal"]
        comparable = next((candidate for candidate in candidates if candidate.ref == comparable_ref), None)
        if comparable is None:
            raise ValueError("The selected listing is not among the validated comparables.")
        cheapest = cheapest_comparable(candidates, prop)
        if cheapest is None or comparable.ref != cheapest.ref:
            raise ValueError("Select the cheapest validated comparable listing.")

        floor = proposal.floor_price
        price, clamped = propose_price(comparable.nightly_price, self._settings.undercut_pct, floor)
        price = cap_price(price, prop.base_price)
        nightly = distribute_prices(
            context["days"], prop.base_price, comparable.nightly_price,
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
        if proposal.expires_at is None:
            raise ValueError("The proposal has no check-in deadline.")
        if self._now() >= proposal.expires_at:
            self._insert(proposal.model_copy(update={"status": "expired"}))
            self._notify_expired(proposal)
            state["outcome"] = "expired"
            result = {"status": "expired", "detail": "The host's check-in time has passed."}
            state["proposal_result"] = result
            return result
        self._insert(proposal)
        if self._expire(proposal):
            state["outcome"] = "expired"
            result = {"status": "expired", "detail": "The proposal expired before approval."}
            state["proposal_result"] = result
            return result

        price_label = self._price_label(proposal)
        remaining_hours = max(0, (proposal.expires_at - self._now()).total_seconds() / 3600)
        summary = [
            f"{booking.guest_name} cancelled {proposal.start_date} to {proposal.end_date}; "
            f"check-in is in {remaining_hours:.1f} hour(s).",
            f"Agent-selected cheapest validated comparable: {comparable.title} at "
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
        try:
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
        except Exception as exc:
            detail = f"Could not request host approval: {type(exc).__name__}: {exc}"
            self._update(proposal.id, status="failed", note=detail)
            ctx.notify(
                f"F5 approval request failed for {prop.name}",
                detail + " No calendar price was changed.",
                level="error",
                property_id=prop.id,
                booking_id=booking.id,
            )
            state["outcome"] = "approval_failed"
            result = {"status": "failed", "error": detail}
            state["proposal_result"] = result
            return result
        self._update(proposal.id, approval_id=approval.id)
        proposal = proposal.model_copy(update={"approval_id": approval.id})
        state["proposal"] = proposal
        state["outcome"] = "proposal_created"
        ctx.notify(
            f"F5 proposal awaiting approval for {prop.name}",
            f"Agent selected {comparable.title} at {comparable.nightly_price:g} "
            f"{comparable.currency}/night. Proposed prices: {self._schedule(proposal)}. "
            f"Market source: {proposal.market_source}. The host's approval is required; "
            "the calendar has not been changed.",
            level="action_required",
            property_id=prop.id,
            booking_id=booking.id,
            approval_id=approval.id,
        )
        result = self._proposal_tool_result(proposal)
        state["proposal_result"] = result
        return result

    def _proposal_tool_result(self, proposal: RelistProposal) -> dict[str, Any]:
        return {
            "status": proposal.status,
            "proposal_id": proposal.id,
            "approval_id": proposal.approval_id,
            "comparable_title": (proposal.comparable_title or "")[:120],
            "comparable_price": proposal.comparable_price,
            "currency": proposal.currency,
            "price_label": self._price_label(proposal),
            "nightly_prices": [
                {"day": night.day.isoformat(), "price": night.proposed_price}
                for night in proposal.nightly_prices[:3]
            ],
            "night_count": len(proposal.nightly_prices),
        }

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
        if not self._decision_is_authorized(event, proposal):
            ctx.notify(
                "Unverified relisting decision ignored",
                "The decision does not match a persisted host approval for this proposal. "
                "No calendar price was changed.",
                level="warning", property_id=proposal.property_id, booking_id=proposal.booking_id,
            )
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
        applied: list[str] = []
        try:
            try:
                days = await self._read_calendar(proposal)
            except Exception as exc:
                self._stop(proposal, "failed", f"Calendar verification failed: {exc}. No changes were made.")
                return
            if self._expire(proposal):
                return
            problem = self._current_policy_problem(proposal)
            if problem:
                self._stop(proposal, "conflict", problem + " No changes were made.")
                return
            if any(not day.available for day in days):
                self._stop(proposal, "conflict", "Some nights were booked or blocked after this proposal. No changes were made.")
                return
            if ([day.day for day in days] != [n.day for n in proposal.nightly_prices]
                    or any(day.price != n.original_price for day, n in zip(days, proposal.nightly_prices))):
                self._stop(proposal, "conflict", "Regular prices changed after this proposal. Review prices manually. No changes were made.")
                return

            # Nopțile sunt deja libere; nu redeschidem disponibilitatea redundant.
            for night in proposal.nightly_prices:
                # O nouă citire micșorează fereastra de concurență, fără a pretinde CAS.
                current = await self._read_calendar(proposal.model_copy(update={
                    "start_date": night.day, "end_date": night.day + timedelta(days=1),
                }))
                if self._expire(proposal):
                    return
                problem = self._current_policy_problem(proposal)
                if not current[0].available or current[0].price != night.original_price:
                    problem = f"Calendar changed before updating {night.day}."
                if problem:
                    self._stop(proposal, "failed" if applied else "conflict",
                               problem + " " + self._progress_note(applied))
                    return
                await asyncio.wait_for(
                    ctx.calendar.set_price(
                        proposal.property_id, night.day, night.day + timedelta(days=1), night.proposed_price
                    ), timeout=self._settings.calendar_timeout_s,
                )
                applied.append(str(night.day))
        except asyncio.CancelledError:
            # Timeoutul handlerului și oprirea procesului întrerup și apelurile în curs.
            self._stop(proposal, "failed", "Relisting was interrupted. " + self._progress_note(applied))
            raise
        except Exception as exc:
            # Portul nu oferă tranzacții; un rollback ar putea suprascrie o rezervare nouă.
            self._stop(proposal, "failed", (
                f"Price update failed: {type(exc).__name__}: {exc}. "
                + self._progress_note(applied)
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

    def _decision_is_authorized(self, event: ApprovalDecided, proposal: RelistProposal) -> bool:
        try:
            approval = self._ctx.approvals.get(event.approval_id)
        except ApprovalNotFound:
            return False
        return (
            approval.feature == event.feature == self._ctx.feature
            and approval.kind == event.kind == APPROVAL_KIND
            and approval.status == event.decision
            and approval.status in {"approved", "rejected"}
            and approval.payload.get("proposal_id") == proposal.id
            and approval.booking_id == proposal.booking_id
            and approval.property_id == proposal.property_id
            and (approval.status == "rejected" or approval.chosen_option == "approve")
        )

    def _current_policy_problem(self, proposal: RelistProposal) -> str | None:
        booking = self._ctx.world.get_booking(proposal.booking_id)
        prop = self._ctx.world.get_property(proposal.property_id)
        if (booking is None or prop is None or booking.status != "cancelled"
                or booking.property_id != proposal.property_id or booking.check_in != proposal.start_date
                or booking.check_out != proposal.end_date or prop.currency != proposal.currency):
            return "The booking or property changed after this proposal."
        rules = self._ctx.world.host_rules()
        # Validează inclusiv noua oră; un config nevalid nu justifică scrierea.
        time.fromisoformat(rules.check_in_time)
        for night in proposal.nightly_prices:
            minimum = max(0.01, floor_price(night.original_price, rules.max_relist_discount_pct))
            if not minimum <= night.proposed_price <= night.original_price:
                return (f"Your maximum relisting discount is now {rules.max_relist_discount_pct:g}%, "
                        "and the approved prices no longer satisfy the current host discount policy.")
        return None

    @staticmethod
    def _progress_note(applied: list[str]) -> str:
        return (
            f"Confirmed updated nights: {', '.join(applied) or 'none'}. "
            "An interrupted or failing call may have completed remotely; verify all nights in your PMS."
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
        booking = self._ctx.world.get_booking(proposal.booking_id)
        if booking is not None:
            try:
                check_in_time = time.fromisoformat(self._ctx.world.host_rules().check_in_time)
                deadline = min(deadline, datetime.combine(booking.check_in, check_in_time, self._timezone))
            except ValueError:
                # Politica invalidă va bloca aplicarea; nu prelungim termenul salvat.
                pass
        if proposal.expires_at != deadline:
            self._update(proposal.id, expires_at=deadline.isoformat())
            proposal.expires_at = deadline
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
