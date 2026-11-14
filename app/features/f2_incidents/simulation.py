"""Simularea personalului de mentenanță: răspunsuri credibile, fără LLM.

Folosite de politica mock a agentului `f2_incidents.sim_staff` și ca fallback când agentul
nu reușește. Răspunsurile diferă după tipul problemei, ca demo-ul să poată arăta ambele
ramuri: o reparație rapidă (remediere) și una lungă (relocare). Nu e logică de business:
cu un model real, personalul simulat e jucat de model.
"""

from __future__ import annotations

import re

# (categorie, cuvinte-cheie). Ordinea contează: prima potrivire câștigă.
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("leak", ("leak", "leaking", "sink", "pipe", "drain", "toilet", "flood", "dripping", "water damage")),
    ("climate", ("air conditioning", "aircon", "a/c", " ac ", "cooling", "heating", "heater",
                 "boiler", "radiator", "hot water", "too hot", "very hot", "freezing", "cold")),
    ("electric", ("light", "lamp", "bulb", "socket", "power", "electric", "fuse")),
    ("internet", ("wifi", "wi-fi", "internet", "router", "tv", "television")),
    ("lock", ("lock", "door", "key", "window")),
    ("pests", ("smell", "mold", "mould", "pest", "cockroach", "insect", "mice", "bugs")),
]  # fmt: skip

STAFF_REPLIES = {
    "leak": "On my way. A leak like that is usually a quick fix: about 2 hours, including drying the area.",
    "climate": "The unit probably needs a replacement part. I can only get it tomorrow morning, "
    "so realistically about 30 hours until it works again.",
    "electric": "Easy one, I can fix it in about 30 minutes.",
    "internet": "I'll reset the router and check the line, about 1 hour.",
    "lock": "I need to replace the lock cylinder, about 3 hours.",
    "pests": "That needs a specialist company. Roughly 48 hours before it is sorted.",
}
VAGUE_REPLY = "Hard to say before I see it. I'll go there after my current job."
CLARIFIED_REPLY = "I looked at the photos the cleaner sent: about 4 hours once I'm there."

TASK_NOTES = {
    "leak": "Replaced the seal under the sink and dried the cabinet. No more leaking.",
    "climate": "Installed the new part and tested the unit for 20 minutes. Working normally.",
    "electric": "Replaced the faulty part and tested every light in the room.",
    "internet": "Router reset and line checked; connection is stable again.",
    "lock": "New lock cylinder installed; spare keys are in the lockbox.",
    "pests": "Specialist treated the apartment; aired it out afterwards.",
}
DEFAULT_TASK_NOTE = "Fixed and tested. Everything works as it should."


def category_of(text: str) -> str | None:
    lowered = " " + re.sub(r"[^a-z0-9/\- ]+", " ", text.lower()) + " "
    for name, words in CATEGORIES:
        if any(word in lowered for word in words):
            return name
    return None


def scripted_staff_reply(description: str, round_index: int) -> str:
    """Răspunsul personalului simulat la întrebarea `round_index` (0 = prima)."""
    category = category_of(description)
    if category is not None:
        return STAFF_REPLIES[category]
    return VAGUE_REPLY if round_index == 0 else CLARIFIED_REPLY


def scripted_task_note(description: str) -> str:
    category = category_of(description)
    return TASK_NOTES.get(category, DEFAULT_TASK_NOTE) if category else DEFAULT_TASK_NOTE


# --- Citirea unei estimări dintr-un text (doar pentru politica mock) ---------------------

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "a few": 3,
}  # fmt: skip


def parse_hours(text: str) -> float | None:
    """Estimarea în ore dintr-un mesaj liber; None dacă nu conține un timp utilizabil."""
    lowered = text.lower()
    for word, value in _WORD_NUMBERS.items():
        lowered = re.sub(rf"\b{word}\b", str(value), lowered)
    if re.search(r"\bhalf an hour\b", lowered):
        return 0.5
    if re.search(r"\b(within the hour|an hour)\b", lowered):
        return 1.0
    number = r"(\d+(?:\.\d+)?)"
    ranged = re.search(rf"{number}\s*(?:-|to)\s*{number}\s*(?:hours?|hrs?|h)\b", lowered)
    if ranged:
        return float(ranged.group(2))
    hours = re.search(rf"{number}\s*(?:hours?|hrs?|h)\b", lowered)
    if hours:
        return float(hours.group(1))
    minutes = re.search(rf"{number}\s*(?:minutes?|mins?)\b", lowered)
    if minutes:
        return round(float(minutes.group(1)) / 60, 2)
    days = re.search(rf"{number}\s*days?\b", lowered)
    if days:
        return float(days.group(1)) * 24
    return None
