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
from app.core.channel_gateway import ChannelError
from app.core.contracts.events import GuestOfferAnswered, IssueReported
from app.core.contracts.models import ApprovalOption, GuestTopic
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
    follow_up_note: str | None = Field(default=None, max_length=400, description="With host_follow_up: one sentence telling the host what the guest wants or what is missing.")


class RequestHostApprovalArgs(ToolArgs):
    request: GuestRequest
    request_summary: str | None = Field(default=None, max_length=1500, description="Optional context; the server builds the authoritative summary.")
    proposed_reply: str | None = Field(default=None, max_length=1500, description="The server generates the actual confirmation from validated parameters.")


class AnswerOfferArgs(ToolArgs):
    topic_id: str = Field(description="`topic_id` of the open topic whose pending offer the guest answered.")
    choice: Literal["accept", "decline"] = Field(
        description="accept = the guest clearly agrees to the offer; decline = the guest clearly "
        "prefers not to. A question or a maybe is neither: answer it instead."
    )


class ReportIssueArgs(ToolArgs):
    description: str = Field(min_length=5, description="The problem, in the guest's own terms.")
    severity: Literal["low", "medium", "high"] = "medium"


def topic_payload(topic: GuestTopic) -> dict[str, Any]:
    """Un subiect deschis, în forma scurtă pe care o primește agentul."""
    payload: dict[str, Any] = {"topic_id": topic.id, "title": topic.title, "facts": topic.facts}
    if topic.offer is not None:
        payload["offer"] = {
            "status": topic.offer.status,  # doar `pending` poate primi un răspuns
            "question": topic.offer.question,
            "details": topic.offer.details,
            "expires_at": topic.offer.expires_at.isoformat(timespec="minutes") if topic.offer.expires_at else None,
        }
    return payload


def offer_acknowledgement(guest_name: str, choice: str) -> str:
    """Confirmarea de primire, scrisă în cod: nu afirmă rezultatul, doar că răspunsul a ajuns."""
    first = guest_name.split()[0] if guest_name.split() else "there"
    if choice == "accept":
        return (
            f"Thank you, {first}! I have passed your answer on to the host's team. "
            "You will receive the details in a separate message shortly."
        )
    return f"Understood, {first}. I have let the host's team know that you prefer to stay."


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


# Garda pe text liber. O promisiune = o formulare care acordă sau confirmă ceva, în aceeași
# propoziție cu subiectul unei excepții (oră, animale, persoane în plus, bani, fumat, petreceri)
# sau, pentru formulările ferme, cu mutarea oaspetelui. Restul e informație și trece.
STRONG_COMMITMENT = re.compile(
    r"\b(approved|confirmed|guarantee(?:d|s)?|promis(?:e|ed|es)|permission|granted|go ahead|"
    r"(?:is|are) reserved|all set|(?:is|has been) arranged|aprobat[ăa]?|confirmat[ăa]?|garantat[ăa]?)\b"
)
SOFT_COMMITMENT = re.compile(
    r"\b(you can|you may|feel free|we can accommodate|no problem|of course|allow(?:s|ed)?|"
    r"permits?|permitted|welcome|works|(?:is|are|that's) fine|"
    r"(?:can|may|could) (?:\w+ ){0,2}?(?:arrive|check|bring|stay|join|come|leave|keep|smoke|have)|"
    r"puteti|puteți|poti|poți|este permis)\b"
)
EXCEPTION_TOPIC = re.compile(
    r"early|earlier|\blate\b|later|check[ -]?in|check[ -]?out|arriv|depart|\bleav|"
    r"\bpets?\b|\bdogs?\b|\bcats?\b|animal|pupp|kitten|"
    r"extra (?:guests?|persons?|people)|additional (?:guests?|persons?|people)|more (?:guests|people)|"
    r"another (?:guest|person)|visitors?|\bfriends?\b|"
    r"refund|discount|compensat|reimburs|upgrade|free of charge|for free|free night|complimentary|waiv|"
    r"smok|part(?:y|ies)\b|extend|extra night|stay longer|"
    r"devreme|t[âa]rziu|c[âa]ine|pisic|reducere|ramburs"
)
RELOCATION_TOPIC = re.compile(r"\bmove\b|\bmoved\b|\bmoving\b|relocat|transfer")
_NEGATION = re.compile(r"(?:\bnot|\bnever|n't|\bno longer)(?:\s+(?:yet|been|be|currently))*\s*$")


def _affirms(pattern: re.Pattern[str], clause: str) -> bool:
    """Există o formulare permisivă care nu e negată („has not yet been confirmed” nu promite)."""
    return any(
        " not " not in f" {match.group()} "
        and not _NEGATION.search(clause[max(0, match.start() - 24):match.start()])
        for match in pattern.finditer(clause)
    )


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
    booking = ctx.world.get_booking(booking_id)
    first = booking.guest_name.split()[0].lower() if booking and booking.guest_name.split() else "there"
    # Negația sau așteptarea dintr-o propoziție nu autorizează promisiunea din alta.
    clauses = re.split(r"[.!?;\n]|\b(?:but|however|dar)\b", lowered)
    for clause in clauses:
        # O formulare permisivă contează doar lângă subiectul unei excepții: „you can see the
        # photos here” e informație, „you can arrive early” e o promisiune.
        exception_topic = EXCEPTION_TOPIC.search(clause) is not None
        relocation_topic = RELOCATION_TOPIC.search(clause) is not None
        affirmative = (
            (exception_topic or relocation_topic) and _affirms(STRONG_COMMITMENT, clause)
        ) or (exception_topic and _affirms(SOFT_COMMITMENT, clause))
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
            raise ValueError(
                "a commitment requires the exact approved request and its reviewed reply; "
                "state the facts or say the host will follow up, without wording that grants "
                "or confirms it"
            )
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
                prop.model_dump(
                    include={"name", "address", "city", "bedrooms", "max_guests", "amenities", "listing_url"}
                )
                if prop
                else None
            ),
            "host_rules": ctx.world.host_rules().model_dump(),
            "pending_approvals": [item for item in requests if item["status"] == "pending"],
            "guest_requests": requests,
            # Ce se întâmplă acum cu sejurul, publicat de feature-ul care se ocupă (ex. un incident).
            "open_topics": [topic_payload(topic) for topic in ctx.guest_topics.for_booking(booking.id)],
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
            note = (args.follow_up_note or "").strip()
            ctx.notify(
                "Guest request needs manual follow-up",
                "Some requested facts or permissions are unavailable. Please review the conversation."
                + (f"\n\nWhat the guest needs: {note}" if note else ""),
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

    async def answer_offer(args: AnswerOfferArgs, run: RunContext) -> dict[str, Any]:
        # Agentul nu execută și nu confirmă oferta: doar transmite răspunsul oaspetelui către
        # feature-ul care a făcut-o. Rezultatul îl comunică acela, cu textul aprobat de host.
        booking = ctx.world.get_booking(run.state["booking_id"])
        if booking is None:
            raise LookupError("booking not found")
        topic = ctx.guest_topics.get(args.topic_id)
        if topic is None or topic.booking_id != booking.id or topic.status != "open":
            raise LookupError("no open topic with this topic_id on this booking; read open_topics again")
        if topic.offer is None or topic.offer.status != "pending":
            raise ValueError("this topic has no offer waiting for an answer")
        if not ctx.guest_topics.answer_offer(topic.id, args.choice):
            raise ValueError("the offer was answered or withdrawn in the meantime")
        acknowledged = True
        try:  # confirmarea de primire pleacă înaintea evenimentului: ordinea mesajelor rămâne firească
            await ctx.channel.send_to_guest(
                booking.id, offer_acknowledgement(booking.guest_name, args.choice),
                sender="copilot", feature=ctx.feature,
            )
            run.state["replies_sent"] = run.state.get("replies_sent", 0) + 1
        except ChannelError:
            acknowledged = False  # ChannelGateway a notificat hostul; răspunsul rămâne valabil
        await ctx.bus.publish(
            GuestOfferAnswered(
                topic_id=topic.id,
                booking_id=booking.id,
                feature=topic.feature,
                choice=args.choice,
                source=f"feature:{ctx.feature}",
            )
        )
        run.state["offer_answered"] = True
        return {
            "recorded": True,
            "choice": args.choice,
            "acknowledgement_sent": acknowledged,
            "note": "The guest already received an acknowledgement. The outcome is sent to the guest "
            "separately by the host's team: do not state or confirm it yourself. Call finish unless "
            "the guest also asked something else.",
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
            "answer_offer",
            "Record the guest's clear answer (accept or decline) to a pending offer listed in "
            "open_topics. Sends the guest an acknowledgement; the outcome is confirmed separately.",
            AnswerOfferArgs,
            answer_offer,
        ),
        Tool(
            "report_issue",
            "Forward a new defect or problem the guest reported so the host's incident flow handles "
            "it. Not for a problem already listed in open_topics, unless it got worse.",
            ReportIssueArgs,
            report_issue,
        ),
    ]
