"""Cererile F3: identitate semantică, limite ferme și confirmări verificate."""

from __future__ import annotations

import hashlib
import json
from datetime import date, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.contracts.models import Approval, Booking, BookingStatus
from app.core.feature_context import FeatureContext

APPROVAL_KIND = "guest_request"
FEATURE = "f3_guest_copilot"
DECLINE_REPLY = (
    "Thank you for your patience. Unfortunately we cannot accommodate this request this time. "
    "Everything else about your stay remains as booked, and we are happy to help with anything else."
)


class GuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_type: Literal["early_check_in", "late_check_out", "extra_guests", "pets"]
    requested_date: date | None = None
    requested_time: time | None = None
    guest_count: int | None = Field(default=None, ge=1, strict=True)
    pet_count: int | None = Field(default=None, ge=1, strict=True)

    def canonical_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def current_booking(ctx: FeatureContext, booking_id: str) -> Booking:
    booking = ctx.world.get_booking(booking_id)
    if booking is None:
        raise LookupError("booking not found")
    if booking.status != BookingStatus.CONFIRMED:
        raise ValueError("only a confirmed booking can receive a guest request confirmation")
    return booking


def canonical_request(ctx: FeatureContext, booking_id: str, request: GuestRequest) -> GuestRequest:
    booking = current_booking(ctx, booking_id)
    prop = ctx.world.get_property(booking.property_id)
    if prop is None:
        raise LookupError("property not found")
    rules = ctx.world.host_rules()
    kind = request.request_type
    target_date = booking.check_out if kind == "late_check_out" else booking.check_in
    if request.requested_date not in (None, target_date):
        raise ValueError("requested date does not match this booking")
    if kind in ("early_check_in", "late_check_out") and target_date < ctx.clock.today():
        raise ValueError("the requested date has already passed; the host must follow up")
    if kind in ("extra_guests", "pets") and booking.check_out <= ctx.clock.today():
        raise ValueError("the stay has ended; the host must follow up")
    if kind in ("early_check_in", "late_check_out"):
        if request.guest_count is not None or request.pet_count is not None:
            raise ValueError("guest and pet counts do not belong to a time request")
        boundary = rules.early_check_in_earliest if kind == "early_check_in" else rules.late_check_out_latest
        if boundary is None:
            raise ValueError(f"{kind} is disabled by the host rules")
        limit = time.fromisoformat(boundary)
        standard = time.fromisoformat(rules.check_in_time if kind == "early_check_in" else rules.check_out_time)
        requested = request.requested_time or limit
        if requested.tzinfo is not None or requested.second or requested.microsecond:
            raise ValueError("requested time must be a local time with minute precision")
        allowed = limit <= requested < standard if kind == "early_check_in" else standard < requested <= limit
        if not allowed:
            raise ValueError(f"requested time is outside the host's {kind} limits")
        return GuestRequest(request_type=kind, requested_date=target_date, requested_time=requested)
    if request.requested_time is not None:
        raise ValueError("a time does not belong to this request")
    if kind == "extra_guests":
        if request.pet_count is not None or request.guest_count is None:
            raise ValueError("extra guests requires the total guest_count, without pet_count")
        if request.guest_count <= booking.guest_count:
            raise ValueError("the total guest count must exceed the current booking")
        if request.guest_count > prop.max_guests:
            raise ValueError(f"total guest count exceeds the property capacity ({prop.max_guests})")
        return GuestRequest(request_type=kind, requested_date=target_date, guest_count=request.guest_count)
    if request.guest_count is not None:
        raise ValueError("guest_count does not belong to a pets request")
    # pets_allowed=False este negociabil numai prin aprobarea explicită a hostului.
    return GuestRequest(request_type="pets", requested_date=target_date, pet_count=request.pet_count or 1)


def scope_snapshot(ctx: FeatureContext, booking: Booking, request: GuestRequest) -> dict[str, Any]:
    prop = ctx.world.get_property(booking.property_id)
    if prop is None:
        raise LookupError("property not found")
    rules = ctx.world.host_rules()
    constraints: dict[str, Any]
    if request.request_type == "early_check_in":
        constraints = {"standard": rules.check_in_time, "limit": rules.early_check_in_earliest}
    elif request.request_type == "late_check_out":
        constraints = {"standard": rules.check_out_time, "limit": rules.late_check_out_latest}
    elif request.request_type == "extra_guests":
        constraints = {"max_guests": prop.max_guests}
    else:
        constraints = {"pets_allowed": rules.pets_allowed}
    return {
        "property_id": booking.property_id,
        "guest_name": booking.guest_name,
        "check_in": booking.check_in.isoformat(),
        "check_out": booking.check_out.isoformat(),
        "guest_count": booking.guest_count,
        "constraints": constraints,
    }


def request_key(booking_id: str, request: GuestRequest, snapshot: dict[str, Any]) -> str:
    canonical = json.dumps([booking_id, request.canonical_data(), snapshot], sort_keys=True, separators=(",", ":"))
    return "f3:guest_request:" + hashlib.sha256(canonical.encode()).hexdigest()


def confirmation_reply(booking: Booking, request: GuestRequest) -> str:
    first = booking.guest_name.split()[0] if booking.guest_name.split() else "there"
    day = str(request.requested_date)
    if request.request_type in ("early_check_in", "late_check_out"):
        early = request.request_type == "early_check_in"
        label = "an early check-in" if early else "a late check-out"
        return (
            f"Good news, {first}! The host confirmed {label}: "
            f"{'from' if early else 'until'} {request.requested_time.strftime('%H:%M')} on {day}."
        )
    if request.request_type == "extra_guests":
        return f"Good news, {first}! The host approved a total of {request.guest_count} guests for your stay starting {day}."
    return f"Good news, {first}! The host approved {request.pet_count} pet(s) for your stay starting {day}."


def host_summary(ctx: FeatureContext, booking: Booking, request: GuestRequest, evidence: dict[str, Any] | None) -> str:
    rules = ctx.world.host_rules()
    day = str(request.requested_date)
    if request.request_type in ("early_check_in", "late_check_out"):
        early = request.request_type == "early_check_in"
        label = "early check-in" if early else "late check-out"
        standard = rules.check_in_time if early else rules.check_out_time
        limit = rules.early_check_in_earliest if early else rules.late_check_out_latest
        summary = (
            f"{booking.guest_name} asks for {label} at {request.requested_time.strftime('%H:%M')} on {day}. "
            f"The standard time is {standard}; the {'earliest' if early else 'latest'} permitted time is {limit}."
        )
    elif request.request_type == "extra_guests":
        prop = ctx.world.get_property(booking.property_id)
        summary = (
            f"{booking.guest_name} asks to increase the party from {booking.guest_count} to "
            f"{request.guest_count} guests for the stay from {booking.check_in} to {booking.check_out}. "
            f"The property accommodates at most {prop.max_guests} guests. "
            "Approval grants permission; the PMS booking still needs to be updated manually."
        )
    else:
        summary = (
            f"{booking.guest_name} asks to bring {request.pet_count} pet(s) for the stay from "
            f"{booking.check_in} to {booking.check_out}. "
            + ("Pets are permitted by the current house rules." if rules.pets_allowed
               else "Pets are not permitted by the standard house rules; this needs your explicit exception.")
        )
    if evidence is not None:
        summary += f" The night of {evidence['night']} is {evidence['state']}."
        if evidence["state"] != "free":
            summary += " Automatic confirmation requires verified availability; please follow up manually if needed."
    return summary


async def calendar_evidence(ctx: FeatureContext, booking: Booking, request: GuestRequest) -> dict[str, Any] | None:
    if request.request_type not in ("early_check_in", "late_check_out"):
        return None
    night = booking.check_in - timedelta(days=1) if request.request_type == "early_check_in" else booking.check_out
    try:
        days = await ctx.calendar.get_days(booking.property_id, night, night + timedelta(days=1))
    except Exception:
        return {"night": night.isoformat(), "state": "unknown"}
    complete = len(days) == 1 and days[0].day == night and days[0].property_id == booking.property_id
    return {"night": night.isoformat(), "state": ("free" if days[0].available else "booked") if complete else "unknown"}


def approval_context(ctx: FeatureContext, booking_id: str) -> list[dict[str, Any]]:
    # Filtrăm înainte de citire: limita globală a serviciului nu ascunde aprobări relevante.
    rows = ctx.db.query(
        "SELECT id FROM approvals WHERE feature = ? AND kind = ? AND booking_id = ? ORDER BY rowid DESC",
        (FEATURE, APPROVAL_KIND, booking_id),
    )
    return [
        {"approval_id": approval.id, "status": approval.status,
         "request": approval.payload.get("request"), "proposed_reply": approval.payload.get("proposed_reply")}
        for row in rows for approval in (ctx.approvals.get(row["id"]),)
    ]


def persisted_approval(ctx: FeatureContext, approval_id: str, booking_id: str, status: str) -> Approval:
    approval = ctx.approvals.get(approval_id)
    if (approval.feature != FEATURE or approval.kind != APPROVAL_KIND
            or approval.booking_id != booking_id or approval.payload.get("booking_id") != booking_id
            or approval.status != status):
        raise ValueError("approval does not authorize this booking and decision")
    return approval


async def validate_approval(
    ctx: FeatureContext, approval_id: str, booking_id: str, *, request: GuestRequest | None = None
) -> Approval:
    approval = persisted_approval(ctx, approval_id, booking_id, "approved")
    if approval.chosen_option != "approve":
        raise ValueError("the host has not selected the approval option")
    booking = current_booking(ctx, booking_id)
    stored = GuestRequest.model_validate(approval.payload.get("request"))
    canonical = canonical_request(ctx, booking_id, stored)
    snapshot = scope_snapshot(ctx, booking, canonical)
    if (stored != canonical or approval.property_id != booking.property_id
            or approval.payload.get("scope_snapshot") != snapshot
            or approval.dedupe_key != request_key(booking_id, canonical, snapshot)):
        raise ValueError("the approved booking or request conditions have changed")
    if request is not None and canonical_request(ctx, booking_id, request) != canonical:
        raise ValueError("approval belongs to a different request")
    if approval.payload.get("proposed_reply") != confirmation_reply(booking, canonical):
        raise ValueError("approval confirmation does not match the reviewed request")
    evidence = await calendar_evidence(ctx, booking, canonical)
    if evidence is not None and evidence["state"] != "free":
        raise ValueError("current calendar is occupied or unknown; the host must confirm manually")
    # Sincronizarea PMS poate schimba rezervarea în timpul apelului de calendar.
    fresh = persisted_approval(ctx, approval_id, booking_id, "approved")
    fresh_booking = current_booking(ctx, booking_id)
    fresh_request = canonical_request(ctx, booking_id, stored)
    if (fresh != approval or fresh_request != canonical
            or scope_snapshot(ctx, fresh_booking, fresh_request) != snapshot
            or confirmation_reply(fresh_booking, fresh_request) != approval.payload["proposed_reply"]):
        raise ValueError("the approved conditions changed while checking the calendar")
    return approval


async def decision_reply(ctx: FeatureContext, approval_id: str, booking_id: str, decision: str) -> str:
    if decision == "approved":
        approval = await validate_approval(ctx, approval_id, booking_id)
        return str(approval.payload["proposed_reply"])
    if decision != "rejected":
        raise ValueError("unknown guest request decision")
    persisted_approval(ctx, approval_id, booking_id, "rejected")
    current_booking(ctx, booking_id)
    return DECLINE_REPLY
