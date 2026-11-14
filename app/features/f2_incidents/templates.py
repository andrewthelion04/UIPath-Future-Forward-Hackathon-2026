"""Textele-șablon ale F2: fallback-ul explicit când un agent LLM cade sau dă un rezultat invalid.

Hostul vede în aprobare când un text vine dintr-un șablon. Texte vizibile în engleză.
"""

from __future__ import annotations

from app.features.f2_incidents.rules import friendly_hours, problem_phrase


def first_name(full_name: str | None) -> str:
    return (full_name or "").split()[0] if (full_name or "").strip() else "there"


def eta_phrase(eta_hours: float | None) -> str:
    return f"within {friendly_hours(eta_hours)}" if eta_hours else "as soon as possible"


def staff_question(staff_name: str, property_name: str, description: str, stay_context: str) -> str:
    return (
        f"Hi {first_name(staff_name)}, an issue was reported at {property_name}: "
        f'"{description}". {stay_context} How long would the fix take? '
        "A rough number of hours is enough."
    )


def staff_go_ahead(staff_name: str | None, task_id: str, property_name: str, task: str) -> str:
    return (
        f"Hi {first_name(staff_name)}, the host approved the work at {property_name}. "
        f'Task {task_id} is yours: "{task}". Please mark it done when it is finished.'
    )


def staff_thanks(staff_name: str | None, task_id: str) -> str:
    return f"Thanks {first_name(staff_name)}, task {task_id} is marked as done."


def relocation_offer(guest: str, property_name: str, description: str, target: str, address: str) -> str:
    return (
        f"Hello {first_name(guest)}, we are very sorry: there is an issue at {property_name} "
        f"({problem_phrase(description)}). To keep your stay comfortable, we can move you to {target} "
        f"({address}) for the rest of your stay, at no extra cost. Would you like to move? "
        "Just reply to this message."
    )


def relocation_accepted(guest: str, target: str, address: str) -> str:
    return (
        f"Thank you, {first_name(guest)}. Your stay continues at {target} ({address}). "
        "We will send you the check-in details shortly. Sorry again for the inconvenience!"
    )


def fix_apology(guest: str, property_name: str, description: str, eta_hours: float | None) -> str:
    return (
        f"Hello {first_name(guest)}, we are sorry about the issue at {property_name} "
        f"({problem_phrase(description)}). Our maintenance team is on it and it should be fixed "
        f"{eta_phrase(eta_hours)}. Thank you for your patience!"
    )


def issue_fixed(guest: str, property_name: str, description: str) -> str:
    return (
        f"Hello {first_name(guest)}, good news: the issue at {property_name} ({problem_phrase(description)}) "
        "has been fixed. Thank you for your patience, and enjoy the rest of your stay!"
    )
