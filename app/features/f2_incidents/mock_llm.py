"""Politicile deterministe ale LLM-ului mock pentru agenții F2.

Imită un model: se uită la obiectiv și la rezultatele uneltelor de până acum și alege pasul
următor. Sunt calea folosită acum în demo (nu avem cheie LLM), deci produc texte credibile.
Cu `LLM_PROVIDER=openrouter`, aceleași fluxuri rulează pe modelul real și politicile nu mai
contează. Regulile de business nu stau aici, ci în `rules.py` și `service.py`.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.agent.mock_support import call, called_tools, failed, finish, last_result, objective_text
from app.features.f2_incidents.agents import CLOSURE, PROPOSAL, SIM_STAFF, STAFF_LIAISON, TRIAGE
from app.features.f2_incidents.simulation import (
    category_of,
    parse_hours,
    scripted_staff_reply,
    scripted_task_note,
)
from app.ports.llm import ChatMessage, ChatRequest, MockPolicy

STOP_WORDS = {
    "the", "a", "an", "is", "are", "in", "on", "at", "of", "to", "and", "it", "not", "no", "under",
    "very", "there", "this", "that", "with", "our", "my", "has", "have", "been", "be", "of", "for",
}  # fmt: skip


def data_blocks(request: ChatRequest) -> list[str]:
    return [m.strip() for m in re.findall(r"<<<\n(.*?)\n>>>", objective_text(request), re.DOTALL)]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS}


def same_problem(a: str, b: str) -> bool:
    category_a, category_b = category_of(a), category_of(b)
    if category_a and category_a == category_b:
        return True
    left, right = words(a), words(b)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= 0.5


# --- Trierea ---------------------------------------------------------------------------------


def triage_policy(request: ChatRequest) -> ChatMessage:
    if "list_open_incidents" not in called_tools(request):
        return call(
            "list_open_incidents",
            reason="I need the open incidents at this property to compare the new report.",
        )
    incidents = last_result(request, "list_open_incidents")
    report = (data_blocks(request) or [""])[0]
    if failed(incidents) or not isinstance(incidents, list):
        return finish("new", ["Could not list open incidents."], reason="Safe default.")
    for incident in incidents:
        for text in incident.get("reports", []):
            if same_problem(report, text):
                return finish(
                    f"attach:{incident['id']}",
                    [f'New report: "{report}"', f'Open incident {incident["id"]}: "{text}"'],
                    reason="Both reports describe the same problem.",
                )
    return finish("new", [f'New report: "{report}"', "No open incident describes this problem."],
                  reason="It is a different problem.")  # fmt: skip


# --- Conversația cu mentenanța -------------------------------------------------------------


def _confidence(text: str) -> str:
    lowered = text.lower()
    if any(w in lowered for w in ("hard to say", "not sure", "no idea", "depends")):
        return "low"
    if any(w in lowered for w in ("probably", "realistically", "roughly", "about", "around")):
        return "medium"
    return "high"


def staff_liaison_policy(request: ChatRequest) -> ChatMessage:
    done = called_tools(request)
    if "get_incident_context" not in done:
        return call("get_incident_context", reason="I need the problem and the affected stay first.")
    if "get_staff_conversation" not in done:
        return call("get_staff_conversation", reason="I need to see what was already said to the staff.")
    if "record_estimate" in done and not failed(last_result(request, "record_estimate")):
        return finish("Estimate recorded.", reason="The estimate is recorded.")
    if "send_message_to_staff" in done:
        sent = last_result(request, "send_message_to_staff")
        if not failed(sent):
            return finish("Message sent; waiting for the staff reply.", reason="Waiting for the reply.")

    context = last_result(request, "get_incident_context") or {}
    messages = (last_result(request, "get_staff_conversation") or {}).get("messages", [])
    replies = [m["text"] for m in messages if m.get("from") != "assistant"]

    if not replies:  # încă nu am întrebat
        if "send_message_to_staff" in done:  # trimiterea a eșuat
            return finish("Could not contact the staff.", reason="The message could not be sent.")
        contact = (context.get("maintenance_contact") or "there").split()[0]
        text = (
            f"Hi {contact}, a problem was reported at {context.get('property')}: "
            f"\"{context.get('problem')}\". {context.get('affected_stay')} "
            "How long do you think the fix will take?"
        )
        return call("send_message_to_staff", text=text, reason="Ask maintenance for a time estimate.")

    reply = replies[-1]
    hours = parse_hours(reply)
    if hours is not None:
        return call(
            "record_estimate",
            hours=hours,
            confidence=_confidence(reply),
            quote=reply,
            reason="The staff reply states how long the fix takes.",
        )
    if context.get("questions_left", 0) > 0 and "send_message_to_staff" not in done:
        return call(
            "send_message_to_staff",
            text="Thanks! Could you give me a rough number of hours? It helps the host decide "
            "whether to move the guest.",
            reason="The reply has no usable time and I may ask one clarification.",
        )
    return call(
        "record_estimate",
        hours=None,
        confidence="low",
        quote=reply,
        reason="No usable time was given and no questions are left.",
    )


# --- Mesajele către oaspete --------------------------------------------------------------------


def _eta_text(context: dict[str, Any]) -> str:
    estimate = str(context.get("fix_estimate") or "")
    return "as soon as possible" if estimate.startswith("unknown") else f"within {estimate}"


def proposal_policy(request: ChatRequest) -> ChatMessage:
    done = called_tools(request)
    if "get_incident_context" not in done:
        return call("get_incident_context", reason="I need the facts before writing to the guest.")
    saved = last_result(request, "save_guest_messages")
    if saved is not None and not failed(saved):
        return finish("Guest messages saved.", reason="All needed messages are saved.")
    if saved is not None and len([n for n in done if n == "save_guest_messages"]) >= 2:
        return finish("Could not save valid messages.", reason="Giving up after two attempts.")

    c = last_result(request, "get_incident_context") or {}
    name = c.get("guest_first_name") or "there"
    prop, problem = c.get("property"), str(c.get("problem") or "")
    eta = _eta_text(c)
    target = c.get("relocation_target") or {}
    texts = {
        "relocation_offer": (
            f"Hi {name}, we are really sorry: there is a problem at {prop} ({problem}). Our "
            f"maintenance team expects to fix it {eta}. So that your stay is not affected, we can "
            f"move you to {target.get('name')} ({target.get('address')}) for the rest of your "
            "stay, at no extra cost. Would you like to move? Just reply to this message."
        ),
        "relocation_accepted": (
            f"Great, {name}! Your stay continues at {target.get('name')}, {target.get('address')}. "
            "We will send you the check-in details in a moment. Thank you for understanding, and "
            "sorry again for the trouble."
        ),
        "relocation_declined": (
            f"Of course, {name}, you can stay. We are sorry for the problem at {prop} ({problem}). "
            f"Our maintenance team is on it and expects it to be fixed {eta}. Thank you for your patience!"
        ),
        "fix_apology": (
            f"Hi {name}, we are sorry for the problem at {prop} ({problem}). Our maintenance team "
            f"is already on it and expects it to be fixed {eta}. Thank you for your patience!"
        ),
    }
    needed = c.get("messages_needed") or []
    return call(
        "save_guest_messages",
        reason="Draft the messages the host will approve verbatim.",
        **{key: texts[key] for key in needed if key in texts},
    )


# --- Închiderea -------------------------------------------------------------------------------


def closure_policy(request: ChatRequest) -> ChatMessage:
    done = called_tools(request)
    if "get_incident_history" not in done:
        return call("get_incident_history", reason="I need what happened before summarising it.")
    saved = last_result(request, "save_closure")
    if saved is not None and not failed(saved):
        return finish("Closure summary saved.", reason="The summary is ready for the host.")
    if saved is not None:
        return finish("Could not save the summary.", reason="The summary was rejected.")

    h = last_result(request, "get_incident_history") or {}
    reports = h.get("reports") or []
    tasks = h.get("tasks") or []
    lines = [f"The problem at {h.get('property')} ({h.get('problem')}) has been fixed."]
    lines.append(
        f"It was reported {len(reports)} time(s)"
        + (f", first by {reports[0]['by']} ({reports[0]['via']})." if reports else ".")
    )
    if h.get("maintenance_quote"):
        lines.append(f"Maintenance estimated {h.get('maintenance_estimate')}.")
    decision = {"remediate": "fix it in place", "relocate": "offer the guest a relocation"}
    if h.get("host_decision"):
        lines.append(f"You chose to {decision.get(h['host_decision'], h['host_decision'])}.")
    choice = {"accept": "accepted the move", "stay": "preferred to stay", "timeout": "did not answer, so they stayed"}
    if h.get("guest_choice"):
        lines.append(f"The guest {choice.get(h['guest_choice'], h['guest_choice'])}.")
    for task in tasks:
        lines.append(f"Task {task['id']} done by {task['by']}: {task['note'] or 'no note'}")
    notes = " ".join(str(t.get("note") or "") for t in tasks).lower()
    if any(w in notes for w in ("temporary", "for now", "provisional", "will come back")):
        lines.append("Note: the staff note suggests a temporary fix. Consider rejecting to schedule a follow-up.")

    guest = None
    if h.get("guest_message_needed"):
        name = h.get("guest_first_name") or "there"
        guest = (
            f"Hi {name}, good news: the problem at {h.get('property')} has been fixed. Thank you "
            "for your patience, and enjoy the rest of your stay!"
        )
    return call("save_closure", summary="\n".join(lines), guest_message=guest,
                reason="Summarise the incident for the host's approval.")  # fmt: skip


# --- Simulare: personalul de mentenanță ---------------------------------------------------------


def sim_staff_policy(request: ChatRequest) -> ChatMessage:
    objective = objective_text(request)
    mode = "done" if "Mode: done" in objective else "reply"
    match = re.search(r"Round: (\d+)", objective)
    round_index = int(match.group(1)) if match else 0
    problem = (data_blocks(request) or [""])[0]
    text = scripted_task_note(problem) if mode == "done" else scripted_staff_reply(problem, round_index)
    return finish(text, reason="This is what the maintenance person would write.")


POLICIES: dict[str, MockPolicy] = {
    TRIAGE: triage_policy,
    STAFF_LIAISON: staff_liaison_policy,
    PROPOSAL: proposal_policy,
    CLOSURE: closure_policy,
    SIM_STAFF: sim_staff_policy,
}
