"""Uneltele agentului F3. Citesc datele noastre; cifrele vin de aici, nu din model.

Limitele se impun în cod: cel mult `MAX_REPLIES_PER_RUN` răspunsuri și o singură cerere de
aprobare per rulare, iar o promisiune către oaspete pleacă doar după aprobarea hostului.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field

from app.core.agent.tools import RunContext, Tool, ToolArgs
from app.core.contracts.events import IssueReported
from app.core.contracts.models import ApprovalOption
from app.core.feature_context import FeatureContext

APPROVAL_KIND = "guest_request"
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


class RequestHostApprovalArgs(ToolArgs):
    request_summary: str = Field(
        min_length=5, description="What the guest asks for and the facts the host needs to decide."
    )
    proposed_reply: str = Field(
        min_length=5, description="The exact message the guest receives if the host approves."
    )


class ReportIssueArgs(ToolArgs):
    description: str = Field(min_length=5, description="The problem, in the guest's own terms.")
    severity: Literal["low", "medium", "high"] = "medium"


def build_tools(ctx: FeatureContext) -> list[Tool]:
    async def get_booking_context(_: GetBookingContextArgs, run: RunContext) -> dict[str, Any]:
        booking = ctx.world.get_booking(run.state["booking_id"])
        if booking is None:
            raise LookupError("booking not found")
        prop = ctx.world.get_property(booking.property_id)
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
        }

    async def check_calendar(args: CheckCalendarArgs, run: RunContext) -> list[dict[str, Any]]:
        nights = (args.end_date - args.start_date).days
        if nights <= 0:
            raise ValueError("end_date must be after start_date")
        if nights > MAX_CALENDAR_NIGHTS:
            raise ValueError(f"range too large; ask for at most {MAX_CALENDAR_NIGHTS} nights")
        days = await ctx.calendar.get_days(run.state["property_id"], args.start_date, args.end_date)
        return [
            {"day": d.day.isoformat(), "available": d.available, "price": d.price, "currency": d.currency}
            for d in days
        ]

    async def get_conversation(args: GetConversationArgs, run: RunContext) -> list[dict[str, Any]]:
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
        message = await ctx.channel.send_to_guest(
            run.state["booking_id"], args.text, sender="copilot", feature=ctx.feature
        )
        run.state["replies_sent"] = run.state.get("replies_sent", 0) + 1
        return {"sent": True, "message_id": message.id}

    async def request_host_approval(args: RequestHostApprovalArgs, run: RunContext) -> dict[str, Any]:
        if run.state.get("approvals_requested", 0) >= 1:
            raise RuntimeError("an approval was already requested in this run")
        booking = ctx.world.get_booking(run.state["booking_id"])
        if booking is None:
            raise LookupError("booking not found")
        prop = ctx.world.get_property(booking.property_id)
        approval = await ctx.approvals.request(
            feature=ctx.feature,
            kind=APPROVAL_KIND,
            title=f"Guest request from {booking.guest_name}" + (f" ({prop.name})" if prop else ""),
            summary=(
                f"{args.request_summary}\n\n"
                f'If you approve, the guest receives:\n"{args.proposed_reply}"'
            ),
            options=[ApprovalOption(key="approve", label="Approve and send the reply")],
            payload={"booking_id": booking.id, "proposed_reply": args.proposed_reply},
            booking_id=booking.id,
            property_id=booking.property_id,
        )
        run.state["approvals_requested"] = run.state.get("approvals_requested", 0) + 1
        return {
            "approval_id": approval.id,
            "status": approval.status,
            "note": "The host has been asked. Tell the guest the host will confirm; do not promise the outcome.",
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
            "Send a message to the guest. Informational only: never promise an exception here.",
            SendGuestReplyArgs,
            send_guest_reply,
        ),
        Tool(
            "request_host_approval",
            "Ask the host to decide on an exception or commitment (early check-in, late "
            "check-out, discount...). If approved, the proposed reply is sent to the guest.",
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
