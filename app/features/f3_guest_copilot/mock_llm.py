"""Politica deterministă a LLM-ului mock pentru agentul F3.

Imită un model: se uită la mesajul oaspetelui și la rezultatele uneltelor de până acum și
alege pasul următor. Așa demo-ul și testele rulează fără cheie API, iar timeline-ul arată
un flux realist. Nu e logică de business: pe un model real, ordinea o alege modelul.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.core.agent.mock_support import (
    call,
    called_tools,
    failed,
    finish,
    last_result,
    objective_text,
)
from app.ports.llm import ChatMessage, ChatRequest

ISSUE_WORDS = (
    "broken", "not working", "doesn't work", "does not work", "isn't working", "stopped working",
    "leak", "dirty", "no hot water", "smell", "mold", "cockroach", "out of order", "won't turn on",
    "defect", "nu merge", "stricat",
)  # fmt: skip
EARLY_WORDS = ("early check", "check in early", "check-in early", "arrive early", "arrive earlier")
LATE_WORDS = ("late check", "check out late", "check-out late", "leave later", "later check")


def guest_text(request: ChatRequest) -> str:
    match = re.search(r"<<<\n(.*?)\n>>>", objective_text(request), re.DOTALL)
    return match.group(1).strip() if match else ""


def classify(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ISSUE_WORDS):
        return "issue"
    if any(word in lowered for word in EARLY_WORDS):
        return "early_check_in"
    if any(word in lowered for word in LATE_WORDS):
        return "late_check_out"
    return "info"


def policy(request: ChatRequest) -> ChatMessage:
    text = guest_text(request)
    done = called_tools(request)

    if "get_booking_context" not in done:
        return call(
            "get_booking_context",
            reason="I need the booking, the property and the host rules before answering.",
        )
    context = last_result(request, "get_booking_context")
    if failed(context) or not isinstance(context, dict):
        return finish(
            "Could not read the booking data, so no reply was sent. The host must answer.",
            [str(context)],
            reason="The data tool failed and I must not invent facts.",
        )

    intent = classify(text)
    if intent == "issue":
        return _issue_flow(request, text, context, done)
    if intent in ("early_check_in", "late_check_out"):
        return _exception_flow(request, intent, context, done)
    return _info_flow(request, text, context, done)


# --- Fluxuri ------------------------------------------------------------------------


def _first_name(context: dict[str, Any]) -> str:
    return str(context["booking"]["guest_name"]).split()[0]


def _reply_or_finish(
    request: ChatRequest, done: list[str], text: str, reason: str, result: str, evidence: list[str]
) -> ChatMessage:
    if "send_guest_reply" not in done:
        return call("send_guest_reply", text=text, reason=reason)
    sent = last_result(request, "send_guest_reply")
    if failed(sent):
        return finish(
            f"The reply could not be delivered: {sent['error']}",
            evidence,
            reason="Sending failed; the host has to follow up.",
        )
    return finish(result, evidence, reason="The guest has an answer and nothing else is pending.")


def _info_flow(request: ChatRequest, text: str, context: dict, done: list[str]) -> ChatMessage:
    booking, prop, rules = context["booking"], context["property"] or {}, context["host_rules"]
    lowered = text.lower()
    amenities = prop.get("amenities", [])
    parts = [f"Hi {_first_name(context)}, thanks for your message!"]
    evidence: list[str] = []

    if any(word in lowered for word in ("wifi", "wi-fi", "internet")):
        has_wifi = "wifi" in amenities
        parts.append(
            "Yes, the apartment has Wi-Fi; the network name and password are in the welcome guide."
            if has_wifi
            else "Unfortunately this property does not have Wi-Fi."
        )
        evidence.append(f"amenities include wifi: {has_wifi}")
    if "park" in lowered:
        has_parking = "parking" in amenities
        parts.append(
            "Yes, a parking spot is included."
            if has_parking
            else "There is no private parking, but public parking is available nearby."
        )
        evidence.append(f"amenities include parking: {has_parking}")
    if any(word in lowered for word in ("pet", "dog", "cat ")):
        parts.append(
            "Pets are welcome." if rules["pets_allowed"] else "Unfortunately pets are not allowed."
        )
        evidence.append(f"host rule pets_allowed: {rules['pets_allowed']}")
    if len(parts) == 1 or any(
        word in lowered for word in ("check", "arriv", "address", "where", "time", "leave")
    ):
        parts.append(
            f"Check-in at {prop.get('name')} ({prop.get('address')}) is from "
            f"{rules['check_in_time']} on {booking['check_in']}, and check-out is by "
            f"{rules['check_out_time']} on {booking['check_out']}."
        )
        evidence.append(
            f"check-in {rules['check_in_time']} on {booking['check_in']}, "
            f"check-out {rules['check_out_time']} on {booking['check_out']}"
        )
    parts.append("Let us know if you need anything else.")

    return _reply_or_finish(
        request,
        done,
        " ".join(parts),
        "I have the facts from the booking context and no exception is requested.",
        "Answered the guest's question with facts from the booking, property and host rules.",
        evidence,
    )


def _exception_flow(request: ChatRequest, intent: str, context: dict, done: list[str]) -> ChatMessage:
    booking, rules = context["booking"], context["host_rules"]
    first = _first_name(context)
    early = intent == "early_check_in"
    check_in, check_out = date.fromisoformat(booking["check_in"]), date.fromisoformat(booking["check_out"])
    # Early check-in depinde de noaptea dinainte; late check-out de noaptea plecării.
    night = check_in - timedelta(days=1) if early else check_out

    if "check_calendar" not in done:
        return call(
            "check_calendar",
            start_date=night.isoformat(),
            end_date=(night + timedelta(days=1)).isoformat(),
            reason=(
                "An early check-in is only realistic if nobody stays the night before."
                if early
                else "A late check-out is only realistic if nobody arrives that day."
            ),
        )
    days = last_result(request, "check_calendar")
    if isinstance(days, list) and days:
        night_state = "free" if all(day.get("available") for day in days) else "booked"
    else:
        night_state = "unknown (calendar unavailable)"

    label = "an early check-in" if early else "a late check-out"
    standard = rules["check_in_time"] if early else rules["check_out_time"]
    limit = rules["early_check_in_earliest"] if early else rules["late_check_out_latest"]
    day = booking["check_in"] if early else booking["check_out"]
    evidence = [
        f"standard time {standard}, limit allowed by host rules {limit}",
        f"night of {night.isoformat()} is {night_state}",
    ]

    if "request_host_approval" not in done:
        return call(
            "request_host_approval",
            request_summary=(
                f"{booking['guest_name']} asks for {label} on {day}. The standard time is "
                f"{standard}; your rules allow at most {limit}. The night of "
                f"{night.isoformat()} is {night_state}."
            ),
            proposed_reply=(
                f"Good news, {first}! The host confirmed {label}: "
                f"{'from' if early else 'until'} {limit} on {day}."
            ),
            reason="This is an exception to the host rules, so the host must decide.",
        )
    asked = last_result(request, "request_host_approval")
    holding = (
        f"Hi {first}, thanks for asking! The standard time is {standard}. "
        + (
            f"I have passed your request for {label} to the host and will confirm as soon as I hear back."
            if not failed(asked)
            else "The host will get back to you about your request shortly."
        )
    )
    return _reply_or_finish(
        request,
        done,
        holding,
        "The guest should know the request is with the host; I must not promise the outcome.",
        f"Asked the host to decide on {label} and told the guest the host will confirm.",
        evidence,
    )


def _issue_flow(request: ChatRequest, text: str, context: dict, done: list[str]) -> ChatMessage:
    if "report_issue" not in done:
        return call(
            "report_issue",
            description=text,
            severity="medium",
            reason="The guest reports a problem in the property; the incident flow must handle it.",
        )
    reported = last_result(request, "report_issue")
    return _reply_or_finish(
        request,
        done,
        f"Hi {_first_name(context)}, thank you for letting us know, and sorry about the trouble. "
        "I have informed the host and the team, and we will get back to you shortly with a solution.",
        "The guest should know the problem is being handled.",
        "Forwarded the reported problem to the incident flow and acknowledged it to the guest.",
        [f"issue forwarded: {not failed(reported)}"],
    )
