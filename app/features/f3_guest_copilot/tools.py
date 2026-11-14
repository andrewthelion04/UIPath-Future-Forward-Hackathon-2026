"""Uneltele agentului F3. Citesc datele noastre; cifrele vin de aici, nu din model.

Limitele se impun în cod: cel mult `MAX_REPLIES_PER_RUN` răspunsuri și o singură cerere de
aprobare per rulare, iar o promisiune către oaspete pleacă doar după aprobarea hostului.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import Field

from app.core.agent.tools import RunContext, Tool, ToolArgs
from app.core.contracts.events import IssueReported
from app.core.contracts.models import ApprovalOption
from app.core.feature_context import FeatureContext
from app.features.f3_guest_copilot.requests import (
    APPROVAL_KIND,
    GuestRequest,
    approval_context,
    calendar_evidence,
    canonical_request,
    confirmation_reply,
    current_booking,
    host_summary,
    request_key,
    scope_snapshot,
    validate_approval,
)

MAX_REPLIES_PER_RUN = 2
MAX_CALENDAR_NIGHTS = 14


class GetBookingContextArgs(ToolArgs):
    pass


class CheckCalendarArgs(ToolArgs):
    start_date: date = Field(description="First night to check, YYYY-MM-DD.")
    end_date: date = Field(description="Day after the last night to check, YYYY-MM-DD (exclusive).")


class GetConversationArgs(ToolArgs):
    limit: int = Field(10, ge=1, le=30, description="How many recent messages to return.")


class SendGuestReplyArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=1500, description="The message for the guest.")
    approval_id: str | None = Field(default=None, description="Required for a confirmation; must match an approved F3 request.")
    request: GuestRequest | None = Field(default=None, description="The exact request authorized by approval_id.")
    host_follow_up: bool = Field(default=False, description="Notify the host when facts are missing or manual follow-up is needed.")


class RequestHostApprovalArgs(ToolArgs):
    request: GuestRequest
    request_summary: str | None = Field(default=None, max_length=1500, description="Optional context; the server builds the authoritative summary.")
    proposed_reply: str | None = Field(default=None, max_length=1500, description="The server generates the actual confirmation from validated parameters.")


class ReportIssueArgs(ToolArgs):
    description: str = Field(min_length=5, description="The problem, in the guest's own terms.")
    severity: Literal["low", "medium", "high"] = "medium"


def standard_time_claim(clause: str, check_in: str, check_out: str) -> bool:
    """Fiecare oră trebuie legată de operația corectă, nu doar găsită în reguli."""
    topics = list(re.finditer(r"check[ -]?in|check[ -]?out|arriv\w*|depart\w*", clause))
    all_times = re.findall(r"\b\d{1,2}:\d{2}\b", clause)
    assigned = 0
    for index, topic in enumerate(topics):
        end = topics[index + 1].start() if index + 1 < len(topics) else len(clause)
        times = re.findall(r"\b\d{1,2}:\d{2}\b", clause[topic.end():end])
        expected = check_out if re.search(r"out|depart", topic.group()) else check_in
        if any(value != expected for value in times):
            return False
        assigned += len(times)
    return bool(all_times) and assigned == len(all_times)


def guard_informational_reply(ctx: FeatureContext, booking_id: str, text: str) -> None:
    """Filtru suplimentar pentru text liber; autorizarea strictă folosește cereri tipizate."""
    lowered = text.lower()
    rules = ctx.world.host_rules()
    notes = " ".join(rules.house_notes).lower()
    if "welcome guide" in lowered and "welcome guide" not in notes:
        raise ValueError("a welcome guide is not established by the property data")
    if re.search(r"public parking|parking (?:is )?(?:available )?nearby|parking spot is included", lowered):
        if not any(term in notes for term in ("public parking", "parking nearby", "parking is included")):
            raise ValueError("parking location, inclusion and fees must come from explicit host data")
    affirmative_pattern = re.compile(
        r"\b(approved|confirmed|guarantee(?:d)?|promis(?:e|ed)|permission|go ahead|you can|you may|feel free|"
        r"we can accommodate|no problem|of course|allow|allowed|permits?|welcome|is reserved|"
        r"all set|works|is fine|(?:can|may) (?:arrive|check|bring|stay)|"
        r"aprobat|confirmat|garantat|puteti|puteți|poti|poți|este permis)\b",
    )
    booking = ctx.world.get_booking(booking_id)
    first = booking.guest_name.split()[0].lower() if booking and booking.guest_name.split() else "there"
    # Negația sau așteptarea dintr-o propoziție nu autorizează promisiunea din alta.
    clauses = re.split(r"[.!?;\n]|\b(?:but|however|dar)\b", lowered)
    for clause in clauses:
        affirmative = any(
            not re.search(r"(?:not|not yet|not been|not yet been)\s*$", clause[max(0, match.start() - 24):match.start()])
            for match in affirmative_pattern.finditer(clause)
        )
        negated = re.search(r"not (?:yet )?(?:approved|confirmed|guaranteed)|cannot accommodate|can't confirm|not allowed", clause)
        pending = re.search(r"host (?:will|needs to|must) confirm|awaiting (?:the )?host|passed your request|pending", clause)
        fact = clause.strip().removeprefix(f"hi {first}, ")
        pets_fact = rules.pets_allowed and fact in (
            "pets are welcome", "pets are allowed", "the host rules permit pets",
            "pets are welcome under the host's rules",
        )
        standard_time_fact = (
            re.search(r"check[ -]?in|check[ -]?out", clause)
            and not re.search(r"early|late|extra|pet|dog|discount|refund", clause)
            and standard_time_claim(clause, rules.check_in_time, rules.check_out_time)
        )
        if affirmative and not pets_fact and not standard_time_fact:
            raise ValueError("a commitment requires the exact approved request and its reviewed reply")
        if not pending and not negated and re.search(r"early|late|check[ -]?in|check[ -]?out|arriv|devreme|târziu|tarziu", clause):
            times = re.findall(r"\b\d{1,2}:\d{2}\b", clause)
            times += ["12:00"] if "noon" in clause else []
            if times and ("noon" in clause or not standard_time_claim(clause, rules.check_in_time, rules.check_out_time)):
                raise ValueError("a nonstandard arrival or departure time requires approval")


def build_tools(ctx: FeatureContext) -> list[Tool]:
    async def get_booking_context(_: GetBookingContextArgs, run: RunContext) -> dict[str, Any]:
        booking = ctx.world.get_booking(run.state["booking_id"])
        if booking is None:
            raise LookupError("booking not found")
        prop = ctx.world.get_property(booking.property_id)
        requests = approval_context(ctx, booking.id)
        return {
            "today": ctx.clock.today().isoformat(),
            "booking": {
                "guest_name": booking.guest_name,
                "guest_count": booking.guest_count,
                "check_in": booking.check_in.isoformat(),
                "check_out": booking.check_out.isoformat(),
                "nights": (booking.check_out - booking.check_in).days,
                "status": booking.status.value,
                "source_channel": booking.source_channel.value,
            },
            "property": (
                prop.model_dump(include={"name", "address", "city", "bedrooms", "max_guests", "amenities"})
                if prop
                else None
            ),
            "host_rules": ctx.world.host_rules().model_dump(),
            "pending_approvals": [item for item in requests if item["status"] == "pending"],
            "guest_requests": requests,
        }

    async def check_calendar(args: CheckCalendarArgs, run: RunContext) -> list[dict[str, Any]]:
        nights = (args.end_date - args.start_date).days
        if nights <= 0:
            raise ValueError("end_date must be after start_date")
        if nights > MAX_CALENDAR_NIGHTS:
            raise ValueError(f"range too large; ask for at most {MAX_CALENDAR_NIGHTS} nights")
        days = await ctx.calendar.get_days(run.state["property_id"], args.start_date, args.end_date)
        expected = {args.start_date + timedelta(days=offset) for offset in range(nights)}
        if (len(days) != nights or {day.day for day in days} != expected
                or any(day.property_id != run.state["property_id"] for day in days)):
            raise ValueError("calendar is incomplete; availability is unknown")
        return [
            {"day": d.day.isoformat(), "available": d.available, "price": d.price, "currency": d.currency}
            for d in days
        ]

    async def get_conversation(args: GetConversationArgs, run: RunContext) -> list[dict[str, Any]]:
        message_id = run.state.get("message_id")
        if message_id:
            cutoff = ctx.db.query_one(
                "SELECT rowid AS position FROM messages WHERE id = ? AND booking_id = ?",
                (message_id, run.state["booking_id"]),
            )
            if cutoff is None:
                raise LookupError("the current guest message is not in this conversation")
            rows = ctx.db.query(
                "SELECT * FROM (SELECT rowid AS position, * FROM messages WHERE booking_id = ? "
                "AND (direction = 'outbound' OR rowid <= ?) ORDER BY rowid DESC LIMIT ?) "
                "ORDER BY position",
                (run.state["booking_id"], cutoff["position"], args.limit),
            )
            return [{"at": row["created_at"], "from": row["sender"], "text": row["body"]} for row in rows]
        history = ctx.conversations.history(run.state["booking_id"], limit=args.limit)
        return [
            {"at": m.created_at.isoformat(timespec="minutes"), "from": m.sender, "text": m.body}
            for m in history
        ]

    async def send_guest_reply(args: SendGuestReplyArgs, run: RunContext) -> dict[str, Any]:
        if run.state.get("replies_sent", 0) >= MAX_REPLIES_PER_RUN:
            raise RuntimeError(
                f"reply limit reached ({MAX_REPLIES_PER_RUN} per run); call finish instead"
            )
        if args.approval_id is not None:
            if args.request is None:
                raise ValueError("the exact request is required with approval_id")
            approval = await validate_approval(ctx, args.approval_id, run.state["booking_id"], request=args.request)
            if args.text != approval.payload["proposed_reply"]:
                raise ValueError("only the exact reviewed confirmation may be sent")
        else:
            if args.request is not None:
                raise ValueError("a request confirmation requires approval_id")
            guard_informational_reply(ctx, run.state["booking_id"], args.text)
        message = await ctx.channel.send_to_guest(
            run.state["booking_id"], args.text, sender="copilot", feature=ctx.feature
        )
        run.state["replies_sent"] = run.state.get("replies_sent", 0) + 1
        if args.host_follow_up:
            ctx.notify(
                "Guest request needs manual follow-up",
                "Some requested facts or permissions are unavailable. Please review the conversation.",
                level="warning", booking_id=run.state["booking_id"], property_id=run.state["property_id"],
            )
        return {"sent": True, "message_id": message.id}

    async def request_host_approval(args: RequestHostApprovalArgs, run: RunContext) -> dict[str, Any]:
        if run.state.get("approvals_requested", 0) >= 1:
            raise RuntimeError("an approval was already requested in this run")
        booking = current_booking(ctx, run.state["booking_id"])
        request = canonical_request(ctx, booking.id, args.request)
        snapshot = scope_snapshot(ctx, booking, request)
        evidence = await calendar_evidence(ctx, booking, request)
        fresh_booking = current_booking(ctx, booking.id)
        fresh_request = canonical_request(ctx, booking.id, args.request)
        if (fresh_request != request or scope_snapshot(ctx, fresh_booking, fresh_request) != snapshot
                or confirmation_reply(fresh_booking, fresh_request) != confirmation_reply(booking, request)):
            raise ValueError("booking conditions changed while checking the calendar; retry with current facts")
        proposed_reply = confirmation_reply(booking, request)
        summary = host_summary(ctx, fresh_booking, fresh_request, evidence)
        key = request_key(booking.id, request, snapshot)
        existing = ctx.db.query_one("SELECT id FROM approvals WHERE dedupe_key = ?", (key,))
        prop = ctx.world.get_property(booking.property_id)
        approval = await ctx.approvals.request(
            feature=ctx.feature,
            kind=APPROVAL_KIND,
            title=f"Guest request from {booking.guest_name}" + (f" ({prop.name})" if prop else ""),
            summary=(
                f"{summary}\n\n"
                f'If you approve, the guest receives:\n"{proposed_reply}"'
            ),
            options=[ApprovalOption(key="approve", label="Approve and send the reply")],
            payload={"booking_id": booking.id, "request": request.canonical_data(),
                     "scope_snapshot": snapshot, "calendar_evidence": evidence, "proposed_reply": proposed_reply},
            booking_id=booking.id,
            property_id=booking.property_id,
            dedupe_key=key,
        )
        run.state["approvals_requested"] = run.state.get("approvals_requested", 0) + 1
        return {
            "approval_id": approval.id,
            "status": approval.status,
            "request": approval.payload["request"],
            "proposed_reply": approval.payload["proposed_reply"],
            "reused": existing is not None,
            "note": (
                "The request is with the host; do not promise the outcome."
                if approval.status == "pending" else
                "This exact request was approved. Use its reviewed reply, approval_id and request; sending revalidates current conditions."
                if approval.status == "approved" else
                "This exact request was rejected. Communicate the recorded refusal without requesting it again."
            ),
        }

    async def report_issue(args: ReportIssueArgs, run: RunContext) -> dict[str, Any]:
        booking = ctx.world.get_booking(run.state["booking_id"])
        if booking is None:
            raise LookupError("booking not found")
        # F3 nu cunoaște F2: emite evenimentul, iar cine e abonat îl preia.
        await ctx.bus.publish(
            IssueReported(
                property_id=booking.property_id,
                booking_id=booking.id,
                description=args.description,
                reported_via="guest",
                reporter=booking.guest_name,
                severity=args.severity,
                source=f"feature:{ctx.feature}",
            )
        )
        return {"reported": True, "note": "The host's incident flow has been started."}

    return [
        Tool(
            "get_booking_context",
            "Read the booking, its property and the host rules. Call this before answering.",
            GetBookingContextArgs,
            get_booking_context,
        ),
        Tool(
            "check_calendar",
            "Availability and nightly price of this booking's property for a date range.",
            CheckCalendarArgs,
            check_calendar,
        ),
        Tool(
            "get_conversation",
            "Recent messages exchanged with this guest.",
            GetConversationArgs,
            get_conversation,
        ),
        Tool(
            "send_guest_reply",
            "Send an informational reply, or the exact reviewed confirmation with its approved "
            "approval_id and matching request. Unknown facts need host_follow_up.",
            SendGuestReplyArgs,
            send_guest_reply,
        ),
        Tool(
            "request_host_approval",
            "Request or reuse the host's decision on early check-in, late check-out, extra "
            "guests or pets. The server validates limits and builds the reviewed reply. "
            "Other exceptions need manual host follow-up.",
            RequestHostApprovalArgs,
            request_host_approval,
        ),
        Tool(
            "report_issue",
            "Forward a defect or problem the guest reported so the host's incident flow handles it.",
            ReportIssueArgs,
            report_issue,
        ),
    ]
