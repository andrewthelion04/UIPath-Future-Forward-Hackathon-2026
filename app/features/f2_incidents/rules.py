"""Regulile F2, ca funcții pure (testate separat). Aici se decide, nu în LLM.

- compatibilitatea unei proprietăți pentru relocare;
- timpul până la check-in;
- dacă reparația e „la timp”;
- recomandarea către host;
- validarea răspunsurilor agenților (trierea, ciornele de mesaje).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time

from app.core.contracts.models import Property
from app.features.f2_incidents.models import OPTION_RELOCATE, OPTION_REMEDIATE

MAX_ETA_HOURS = 24 * 14  # o estimare peste două săptămâni e tratată ca invalidă
MAX_GUEST_MESSAGE_CHARS = 1200
MAX_STAFF_QUESTIONS = 2  # întrebarea inițială + cel mult o clarificare
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def is_compatible(candidate: Property, original: Property, guest_count: int) -> bool:
    """Regula de compatibilitate pentru relocare: același oraș, loc pentru toți, cel puțin la fel de mare."""
    return (
        candidate.id != original.id
        and candidate.city.strip().lower() == original.city.strip().lower()
        and candidate.max_guests >= guest_count
        and candidate.bedrooms >= original.bedrooms
    )


def parse_clock(value: str, default: time = time(15, 0)) -> time:
    """`"15:00"` -> `time(15, 0)`; o valoare invalidă -> `default`."""
    try:
        hours, minutes = value.strip().split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return default


def check_in_moment(check_in: date, check_in_time: str) -> datetime:
    return datetime.combine(check_in, parse_clock(check_in_time))


def hours_until_check_in(check_in: date, check_in_time: str, now_local: datetime) -> float:
    """Ore până la sosire (oră locală, fără fus). Negativ dacă oaspetele a sosit deja."""
    delta = check_in_moment(check_in, check_in_time) - now_local
    return round(delta.total_seconds() / 3600, 1)


def fix_in_time(
    eta_hours: float | None,
    hours_until: float | None,
    in_house: bool,
    *,
    margin_hours: float,
    inhouse_tolerable_hours: float,
) -> bool:
    """Reparația e „la timp” dacă estimarea e cunoscută și:
    oaspetele e cazat și estimarea e cel mult pragul tolerabil; sau oaspetele nu a sosit și
    reparația se termină cu cel puțin `margin_hours` înainte de check-in. Fără rezervare
    afectată (`hours_until` None și nu e cazat), orice estimare cunoscută e „la timp”.
    """
    if eta_hours is None:
        return False
    if in_house:
        return eta_hours <= inhouse_tolerable_hours
    if hours_until is None:
        return True
    return eta_hours + margin_hours <= hours_until


def recommend(*, has_booking: bool, in_time: bool, relocation_available: bool) -> str:
    """Oferă relocarea doar dacă reparația nu e la timp și există unde muta oaspetele."""
    if has_booking and not in_time and relocation_available:
        return OPTION_RELOCATE
    return OPTION_REMEDIATE


def explain_recommendation(
    option: str,
    *,
    eta_hours: float | None,
    hours_until: float | None,
    in_house: bool,
    has_booking: bool,
    in_time: bool,
    relocation_available: bool,
    inhouse_tolerable_hours: float,
) -> str:
    """Motivul recomandării, pentru host. Textul doar explică; decizia e în `recommend`."""
    eta = format_hours(eta_hours) if eta_hours is not None else "an unknown time"
    if not has_booking:
        return "No guest is affected, so the fix can simply be scheduled."
    if option == OPTION_RELOCATE:
        if in_house:
            return (
                f"The guest is in the property and the fix needs {eta}, more than the "
                f"{format_hours(inhouse_tolerable_hours)} a guest can reasonably wait."
            )
        return f"The fix needs {eta}, but check-in is in {format_hours(hours_until)}."
    if in_time:
        where = "the guest can wait" if in_house else f"check-in is in {format_hours(hours_until)}"
        return f"The fix needs about {eta} and {where}, so it can be done in time."
    if not relocation_available:
        return f"The fix needs {eta} and no compatible property is free, so fixing in place is the only option."
    return f"The fix needs {eta}."


def format_hours(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        value = round(hours, 1)
        return f"{value:g} h"
    return f"{hours / 24:.1f} days"


def friendly_hours(hours: float | None) -> str:
    """Varianta pentru oaspete: „about 30 hours”, „about 45 minutes”, „about 2 days”."""
    if hours is None:
        return "an unknown time"
    if hours < 1:
        return f"about {max(5, round(hours * 60 / 5) * 5)} minutes"
    if hours < 48:
        value = round(hours)
        return f"about {value} hour{'s' if value != 1 else ''}"
    days = round(hours / 24)
    return f"about {days} days"


def problem_phrase(description: str) -> str:
    """Descrierea ca fragment de frază: fără punct final, cu literă mică la început."""
    text = description.strip().rstrip(".!")
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


# --- Validarea răspunsurilor agenților ------------------------------------------------

_PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>|\bTODO\b|\bXX+\b|\bN/A\b")


def valid_guest_message(text: str | None) -> bool:
    """Text nevid, nu prea lung, fără placeholder-e rămase."""
    if not text or not text.strip():
        return False
    if len(text) > MAX_GUEST_MESSAGE_CHARS:
        return False
    return _PLACEHOLDER.search(text) is None


def parse_triage_result(result: str | None, open_ids: set[str]) -> str | None:
    """`attach:<id>` -> id-ul (doar dacă e un incident deschis al proprietății); altfel None."""
    if not result:
        return None
    match = re.fullmatch(r"\s*attach\s*:\s*(\S+)\s*", result)
    if match and match.group(1) in open_ids:
        return match.group(1)
    return None


def valid_eta(hours: float | None) -> float | None:
    if hours is None:
        return None
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > MAX_ETA_HOURS:
        return None
    return round(value, 2)


def higher_severity(current: str, new: str) -> str:
    return new if SEVERITY_RANK.get(new, 1) > SEVERITY_RANK.get(current, 1) else current
