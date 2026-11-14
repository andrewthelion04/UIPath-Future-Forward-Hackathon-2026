"""Politică deterministă F3 pentru demo și teste fără cheie.

Simulează alegerea uneltelor; validările rămân în unelte. Nu evaluează promptul real.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.core.agent.mock_support import call, called_tools, failed, finish, last_result, objective_text
from app.ports.llm import ChatMessage, ChatRequest

ISSUE_WORDS = (
    "broken", "not working", "doesn't work", "does not work", "isn't working", "stopped working",
    "leak", "dirty", "no hot water", "smell", "mold", "cockroach", "out of order", "won't turn on",
    "defect", "nu merge", "stricat",
)
EARLY_WORDS = ("early check", "check in early", "check-in early", "arrive early", "arrive earlier")
LATE_WORDS = ("late check", "check out late", "check-out late", "leave later", "later check")
NUMBER_WORDS = {word: number for number, word in enumerate(
    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
)}
NUMBER = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"


# Subiectele deschise (ex. un incident) și oferta făcută oaspetelui.
# Un „ok, thanks” nu acceptă o mutare: e nevoie de un acord explicit sau de un „da” singur.
ACCEPT = re.compile(
    r"\b(accept|move us|go for it|sounds good|yes,? please|let'?s (?:do it|move|go)|"
    r"we(?:'ll| will| would like to| want to) (?:take it|move))\b|"
    r"\b(?:yes|yeah|ok(?:ay)?|sure)\b.*\b(?:move|relocat|take it)|"
    r"^(?:yes|yeah|yep|sure|ok(?:ay)?)[\s.!]*$"
)
DECLINE = re.compile(
    r"^no\b|\b(no,? thanks?|no,? thank you|decline|prefer to stay|rather stay|we(?:'ll| will)? stay|"
    r"stay (?:here|put|where we are)|(?:don'?t|do not) want to move|not (?:going to )?move)\b"
)
DETAIL_WORDS = (
    "photo", "picture", "link", "listing", "look like", "parking", "bedroom", "how many", "how big",
    "where is", "address", "how far", "amenit", "balcony", "washer", "details", "more about",
    "tell me about", "what is it like", "what's it like", "sleeps",
)
STATUS_WORDS = ("when", "how long", "fixed", "repair", "update", "status", "any news", "technician", "happening")
HANDOVER_WORDS = (
    "not happy", "unacceptable", "speak to", "talk to", "call me", "manager", "host directly",
    "compensation", "refund", "discount", "complain", "not satisfied", "another apartment",
    "different apartment", "other option",
)
WHERE_WORDS = ("address", "where", "which apartment", "new place", "location")


def guest_text(request: ChatRequest) -> str:
    match = re.search(r"<<<\n(.*?)\n>>>", objective_text(request), re.DOTALL)
    return match.group(1).strip() if match else ""


def classify(text: str) -> str:
    """Intenția dominantă rămâne compatibilă; fluxul tratează separat intențiile mixte."""
    lowered = text.lower()
    if any(word in lowered for word in ISSUE_WORDS):
        return "issue"
    if any(word in lowered for word in EARLY_WORDS):
        return "early_check_in"
    if any(word in lowered for word in LATE_WORDS):
        return "late_check_out"
    return "info"


def _number(value: str) -> int:
    return int(value) if value.isdigit() else NUMBER_WORDS[value]


def _requested_time(text: str) -> str | None:
    if "noon" in text:
        return "12:00"
    if "midnight" in text:
        return "00:00"
    match = re.search(r"\b(\d{1,2}):(\d{2})(?:\s*(am|pm))?\b", text)
    if match is None:
        match = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text)
        if match is None:
            return None
        hour, minute, meridiem = int(match[1]), 0, match[2]
    else:
        hour, minute, meridiem = int(match[1]), int(match[2]), match[3]
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return f"{hour:02d}:{minute:02d}" if 0 <= hour < 24 and minute < 60 else None


def _is_pet_question(text: str) -> bool:
    return bool(re.search(r"\b(pets?|dogs?|cats?)\b", text))


def _is_pet_policy_question(text: str) -> bool:
    return ("pet policy" in text or "pets allowed" in text or bool(re.search(
        r"\b(?:are|is)\b.*\b(?:pets?|dogs?|cats?)\b.*\b(?:allowed|welcome)\b", text
    ))) and not any(word in text for word in ("bring", "our dog", "our cat", "exception"))


def _request_candidates(text: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrage parametrii; uneltele decid dacă respectă limitele și aprobările existente."""
    lowered = text.lower()
    booking, rules = context["booking"], context["host_rules"]
    requested_time = _requested_time(lowered)
    explicit_day = re.search(r"\b\d{4}-\d{2}-\d{2}\b", lowered)
    candidates = []
    early = any(word in lowered for word in EARLY_WORDS)
    late = any(word in lowered for word in LATE_WORDS)
    if requested_time and re.search(r"check[ -]?in|arriv", lowered):
        early = early or requested_time < rules["check_in_time"]
    if requested_time and re.search(r"check[ -]?out|leav", lowered):
        late = late or requested_time > rules["check_out_time"]
    for is_requested, kind, day_key, limit_key in (
        (early, "early_check_in", "check_in", "early_check_in_earliest"),
        (late, "late_check_out", "check_out", "late_check_out_latest"),
    ):
        if is_requested:
            candidates.append({
                "request_type": kind,
                "requested_date": explicit_day[0] if explicit_day else booking[day_key],
                "requested_time": requested_time or rules.get(limit_key),
            })
    if _is_pet_question(lowered) and not _is_pet_policy_question(lowered) and not rules["pets_allowed"]:
        count = re.search(rf"\b({NUMBER})\s+(?:pets?|dogs?|cats?)\b", lowered)
        candidates.append({"request_type": "pets", "pet_count": _number(count[1]) if count else 1})
    extra = re.search(rf"\b({NUMBER})\s+(?:extra|additional|more)\s+(?:guests?|people|persons?|friends?)\b", lowered)
    total = re.search(rf"\b({NUMBER})\s+(?:guests?|people|persons?)\b", lowered)
    if total is None:
        total = re.search(rf"\bwe(?:'ll| will)?\s+(?:be|are)\s+({NUMBER})\b", lowered)
    guest_count = (
        booking["guest_count"] + _number(extra[1]) if extra
        else _number(total[1]) if total else None
    )
    if guest_count is not None and guest_count > booking["guest_count"]:
        candidates.append({"request_type": "extra_guests", "guest_count": guest_count})
    return candidates


def _first_name(context: dict[str, Any]) -> str:
    return str(context["booking"]["guest_name"]).split()[0]


def _reply_or_finish(
    request: ChatRequest, done: list[str], text: str, evidence: list[str],
    *, host_follow_up: bool = False, approval: dict[str, Any] | None = None, prior_replies: int = 0,
    follow_up_note: str | None = None,
) -> ChatMessage:
    if prior_replies and failed(last_result(request, "send_guest_reply")):
        return finish("The first reply could not be delivered; the host must follow up.", evidence,
                      reason="An earlier send failed and must not be reported as successful.")
    if done.count("send_guest_reply") <= prior_replies:
        args: dict[str, Any] = {
            "text": text,
            "reason": "The reply uses verified facts or states what the host still needs to decide.",
        }
        if host_follow_up:
            args["host_follow_up"] = True
            if follow_up_note:
                args["follow_up_note"] = follow_up_note
        if approval:
            args.update(approval_id=approval["approval_id"], request=approval["request"])
        return call("send_guest_reply", **args)
    sent = last_result(request, "send_guest_reply")
    if failed(sent):
        return finish(
            f"The reply could not be delivered: {sent['error']}", evidence,
            reason="Sending failed; the host has to follow up.",
        )
    return finish("Answered from verified facts and the recorded request status.", evidence,
                  reason="The guest has an answer and required actions have been attempted.")


def _fact_parts(text: str, context: dict[str, Any], *, include_standard: bool) -> tuple[list[str], list[str], bool]:
    booking, prop, rules = context["booking"], context.get("property") or {}, context["host_rules"]
    lowered = text.lower()
    amenities = prop.get("amenities", [])
    notes = rules.get("house_notes", [])
    parts: list[str] = []
    evidence: list[str] = []
    follow_up = False
    if any(word in lowered for word in ("wifi", "wi-fi", "internet", "password")):
        if "wifi" in amenities:
            parts.append("The property has Wi-Fi.")
            evidence.append("wifi is listed in the property amenities")
        else:
            parts.append("I do not have verified Wi-Fi information for this property; the host will follow up.")
            follow_up = True
        if any(word in lowered for word in ("password", "network")):
            verified = [note for note in notes if re.search(r"wi[ -]?fi|network|password", note, re.I)]
            parts.extend(verified)
            if not verified:
                parts.append("I do not have a verified network name or password to share; the host will follow up.")
                follow_up = True
    if "park" in lowered:
        parts.append("Parking is listed among the property's amenities." if "parking" in amenities
                     else "I do not have verified parking information for this property.")
        verified = [note for note in notes if "park" in note.lower()]
        parts.extend(verified)
        if not verified:
            parts.append("I do not have verified parking availability, allocation or cost details; the host will follow up.")
            follow_up = True
        evidence.append(f"parking listed in amenities: {'parking' in amenities}")
    if _is_pet_question(lowered) and (rules["pets_allowed"] or _is_pet_policy_question(lowered)):
        parts.append("The host rules permit pets." if rules["pets_allowed"]
                     else "Pets are not allowed under the standard house rules. An exception requires the host's decision.")
        evidence.append(f"pets_allowed: {rules['pets_allowed']}")
    if any(word in lowered for word in ("access", "door", "key", "lockbox", "code", "entry", "get in")):
        verified = [note for note in notes if re.search(r"access|door|key|lockbox|code|entry", note, re.I)]
        parts.extend(verified)
        if not verified:
            parts.append("I do not have verified access instructions; the host will follow up.")
            follow_up = True
        evidence.extend(verified)
    standard_question = re.search(r"check[ -]?in|check[ -]?out|arrival|arriv(?:e|ing)|departure|departing|address|leave", lowered)
    if include_standard and standard_question:
        if prop.get("name") and prop.get("address"):
            parts.append(
                f"Check-in at {prop['name']} ({prop['address']}) is from "
                f"{rules['check_in_time']} on {booking['check_in']}, and check-out is by "
                f"{rules['check_out_time']} on {booking['check_out']}."
            )
            evidence.append(f"standard check-in {rules['check_in_time']}; check-out {rules['check_out_time']}")
        else:
            parts.append("I do not have complete property details; the host will follow up.")
            follow_up = True
    if re.search(r"extra guests?|additional guests?|more (?:people|guests?)", lowered) and not _request_candidates(text, context):
        parts.append("How many people would stay in total? I need that number before asking the host.")
    if re.search(r"discount|refund|reimburs|compensation|upgrade|waiv|complimentary|free stay", lowered):
        parts.append("I do not have verified details for that request; the host will follow up.")
        evidence.append("the requested exception is not supported by the available facts and typed request tools")
        follow_up = True
    if include_standard and not parts:
        parts.append("I do not have verified information to answer that question; the host will follow up.")
        evidence.append("the available booking context does not answer this question")
        follow_up = True
    return parts, evidence, follow_up


def _place_description(details: dict[str, Any]) -> list[str]:
    amenities = ", ".join(str(item).replace("_", " ") for item in details.get("amenities", []))
    parts = [
        f"{details.get('name')} is at {details.get('address')}.",
        f"It has {details.get('bedrooms')} bedroom(s) and sleeps {details.get('sleeps')}"
        + (f", with {amenities}." if amenities else "."),
    ]
    if details.get("listing_url"):
        parts.append(f"Photos and the full description are here: {details['listing_url']}")
    return parts


def _topic_flow(
    request: ChatRequest, text: str, context: dict[str, Any], done: list[str]
) -> ChatMessage | None:
    """Mesajul privește un subiect deschis (incident, ofertă)? Altfel None: fluxul obișnuit."""
    topics = context.get("open_topics") or []
    if not topics:
        return None
    lowered = text.lower()
    name = _first_name(context)
    pending = next((t for t in topics if (t.get("offer") or {}).get("status") == "pending"), None)
    answered = "answer_offer" in done

    if answered:
        result = last_result(request, "answer_offer")
        if failed(result):
            return _reply_or_finish(
                request, done,
                f"Hi {name}, I could not record your answer automatically. The host will contact you directly.",
                [str(result)], host_follow_up=True, follow_up_note=f'Guest answered an offer: "{text}"',
            )
        return finish("The guest's answer to the offer was recorded and acknowledged.",
                      [f"answer_offer: {result.get('choice')}"],
                      reason="The host's team confirms the outcome separately.")

    if any(word in lowered for word in HANDOVER_WORDS):
        return _reply_or_finish(
            request, done,
            f"Hi {name}, I understand. I have asked the host to contact you directly about this, "
            "and they will get back to you as soon as possible.",
            [f"open topic: {topics[0].get('title')}"],
            host_follow_up=True, follow_up_note=f'Guest wants the host directly: "{text}"',
        )

    is_question = "?" in lowered
    if pending is not None and any(word in lowered for word in DETAIL_WORDS):
        details = pending["offer"].get("details") or {}
        parts = _place_description(details)
        if details.get("extra_cost") == "none":
            parts.append("The move is at no extra cost.")
        parts.append("Would you like to move there?")
        return _reply_or_finish(request, done, f"Hi {name}, " + " ".join(parts),
                                [f"offer details: {details.get('name')}"])

    moved = next((t for t in topics if "current_accommodation" in (t.get("facts") or {})), None)
    if moved is not None and any(word in lowered for word in WHERE_WORDS):
        place = moved["facts"]["current_accommodation"]
        parts = [f"you are now staying at {place.get('name')}, {place.get('address')}."]
        if place.get("listing_url"):
            parts.append(f"The listing is here: {place['listing_url']}")
        if moved["facts"].get("check_in_details"):
            parts.append(str(moved["facts"]["check_in_details"]))
        return _reply_or_finish(request, done, f"Hi {name}, " + " ".join(parts),
                                [f"moved to {place.get('name')}"])

    if is_question and any(word in lowered for word in STATUS_WORDS):
        facts = topics[0].get("facts") or {}
        if facts.get("repair_estimate"):
            estimate = str(facts["repair_estimate"])
            when = estimate if estimate.startswith("as soon") else f"in {estimate}"
            status = f"The repair is scheduled with our maintenance team and should be done {when}."
        elif pending is not None:
            stays = str(facts.get("if_the_guest_stays", "")).replace("The repair", "the repair")
            status = (f"We offered you a move to {pending['offer']['details'].get('name')}. "
                      f"If you prefer to stay, {stays}")
        else:
            status = str(facts.get("status", "")).replace("the guest's", "your").replace("the guest", "you")
        return _reply_or_finish(
            request, done,
            f"Hi {name}, here is the latest on the problem ({facts.get('problem')}). {status}",
            [f"open topic: {topics[0].get('title')}"],
        )

    # O întrebare nu e un răspuns: oferta se acceptă sau se refuză doar printr-o afirmație clară.
    if pending is not None and not is_question:
        choice = "decline" if DECLINE.search(lowered) else "accept" if ACCEPT.search(lowered) else None
        if choice:
            return call("answer_offer", topic_id=pending["topic_id"], choice=choice,
                        reason=f"The guest clearly wants to {choice} the offer made by the host.")
    return None


def policy(request: ChatRequest) -> ChatMessage:
    text = guest_text(request)
    done = called_tools(request)
    if "get_booking_context" not in done:
        return call("get_booking_context", reason="I need the booking, property, rules and existing requests before answering.")
    context = last_result(request, "get_booking_context")
    if failed(context) or not isinstance(context, dict):
        return finish("Could not read the booking data; the host must answer.", [str(context)],
                      reason="The data tool failed and I must not invent facts.")
    topic_step = _topic_flow(request, text, context, done)
    if topic_step is not None:
        return topic_step
    issue = any(word in text.lower() for word in ISSUE_WORDS)
    if issue and "report_issue" not in done:
        return call("report_issue", description=text, severity="medium",
                    reason="The guest reports a property problem; the incident flow must handle it.")
    candidates = _request_candidates(text, context)
    if candidates:
        return _exception_flow(request, text, context, done, candidates, issue)
    parts, evidence, follow_up = _fact_parts(text, context, include_standard=not issue)
    if issue:
        reported = last_result(request, "report_issue")
        parts.insert(0, "I have informed the host and the team about the problem." if not failed(reported)
                     else "I could not forward the problem automatically; the host will follow up.")
        follow_up = follow_up or failed(reported)
        evidence.append(f"issue forwarded: {not failed(reported)}")
    return _reply_or_finish(request, done, f"Hi {_first_name(context)}, " + " ".join(parts),
                            evidence or ["used the available booking context"], host_follow_up=follow_up)


def _exception_flow(
    request: ChatRequest, text: str, context: dict[str, Any], done: list[str],
    candidates: list[dict[str, Any]], issue: bool,
) -> ChatMessage:
    candidate = candidates[0]
    kind = candidate["request_type"]
    booking, rules = context["booking"], context["host_rules"]
    evidence = [f"request type: {kind}"]
    if kind in ("early_check_in", "late_check_out"):
        early = kind == "early_check_in"
        night = date.fromisoformat(booking["check_in"]) - timedelta(days=1) if early else date.fromisoformat(booking["check_out"])
        if "check_calendar" not in done:
            return call("check_calendar", start_date=night.isoformat(), end_date=(night + timedelta(days=1)).isoformat(),
                        reason="The neighboring night informs the host's decision without guaranteeing availability.")
        days = last_result(request, "check_calendar")
        complete = (isinstance(days, list) and len(days) == 1 and days[0].get("day") == night.isoformat())
        night_state = ("free" if days[0].get("available") else "booked") if complete else "unknown (calendar unavailable)"
        summary = f"{booking['guest_name']} requests {kind.replace('_', ' ')}. The night of {night.isoformat()} is {night_state}."
        evidence.append(f"night of {night.isoformat()} is {night_state}")
    else:
        summary = f"{booking['guest_name']} requests {kind.replace('_', ' ')} with parameters {candidate}."
    if "request_host_approval" not in done:
        return call("request_host_approval", request=candidate, request_summary=summary,
                    reason="The host must decide on this exact exception after the tool validates its limits.")
    approval = last_result(request, "request_host_approval")
    facts, fact_evidence, follow_up = _fact_parts(text, context, include_standard=False)
    evidence.extend(fact_evidence)
    if issue:
        reported = last_result(request, "report_issue")
        facts.insert(0, "I have informed the host and the team about the problem." if not failed(reported)
                     else "I could not forward the problem automatically; the host will follow up.")
        follow_up = follow_up or failed(reported)
        evidence.append(f"issue forwarded: {not failed(reported)}")
    if len(candidates) > 1:
        facts.append("Your other request also needs the host's attention; the host will follow up.")
        follow_up = True
    if failed(approval) or not isinstance(approval, dict):
        error = str((approval or {}).get("error", "request could not be submitted"))
        holding = "I could not submit this request automatically. The host will follow up."
        follow_up = True
        evidence.append(error)
    elif approval.get("status") == "approved":
        # Numai textul canonic întors de unealtă, cu aceeași cerere și aprobare.
        # Un al doilea răspuns păstrează confirmarea exactă fără să ascundă alte intenții.
        if facts and "send_guest_reply" not in done:
            return _reply_or_finish(request, done, f"Hi {_first_name(context)}, " + " ".join(facts),
                                    evidence, host_follow_up=follow_up)
        return _reply_or_finish(request, done, approval["proposed_reply"], evidence,
                                approval=approval, prior_replies=1 if facts else 0)
    elif approval.get("status") == "rejected":
        holding = "The host previously declined this request, so I cannot accommodate it."
        evidence.append("the same request was previously rejected")
    else:
        label = {"early_check_in": "an early check-in", "late_check_out": "a late check-out",
                 "pets": "bringing your pet", "extra_guests": "additional guests"}[kind]
        holding = (f"Your request for {label} is already with the host; I will let you know when the host decides."
                   if approval.get("reused") else
                   f"I have passed your request for {label} to the host and will confirm as soon as I hear back.")
        if kind in ("early_check_in", "late_check_out"):
            holding = f"The standard time is {rules['check_in_time' if kind == 'early_check_in' else 'check_out_time']}. " + holding
    return _reply_or_finish(request, done, f"Hi {_first_name(context)}, " + " ".join([*facts, holding]),
                            evidence, host_follow_up=follow_up)
