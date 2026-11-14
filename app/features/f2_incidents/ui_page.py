"""Pagina F2: incidentele, conversația agentului cu personalul, sarcinile. Consumă doar API-ul REST.

Deciziile hostului se iau din inboxul de aprobări, iar personalul e simulat (setările
`F2_SIM_*`). Singura acțiune de aici: când copilotul a predat hostului discuția despre o ofertă
de relocare, hostul notează ce a hotărât oaspetele, ca incidentul să meargă mai departe.
"""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, flash, load, multiline, property_names, short_time, show_flash, status_badge

STATUS_HELP = {
    "awaiting_staff": "The agent asked maintenance how long the fix takes.",
    "awaiting_decision": "The proposal is in the host's approval inbox.",
    "awaiting_guest": "The relocation offer was sent; waiting for the guest's answer.",
    "remediation_scheduled": "The repair task is with the staff.",
    "relocated": "The guest moved; the repair task is with the staff.",
    "resolved": "The host approved closing the incident.",
    "dismissed": "The host dismissed the incident.",
}
GUEST_ANSWERS = {"accept": "accepted the move", "stay": "preferred to stay", "timeout": "did not answer (stays)"}

st.title("F2 · Incidents")
st.caption(
    "A report from staff or from a guest opens an incident. The agent asks maintenance how "
    "long the fix takes, then proposes relocating the guest or fixing in place, with the exact "
    "guest messages. Nothing reaches the guest before the host approves. Staff and guest answers "
    "are simulated in the demo."
)

show_flash()


def record_answer(incident: dict, choice: str) -> None:
    result = act(api.post, f"/incidents/{incident['id']}/guest-answer", {"choice": choice}, wait=True)
    if result is not None:
        flash("Recorded: the guest " + ("accepted the move." if choice == "accept" else "stays."))
        st.rerun()


incidents = load("/incidents")
names = property_names(load("/properties"))
bookings = {b["id"]: b for b in load("/bookings")}
guests = {key: b["guest_name"] for key,b in bookings.items()}

if not incidents:
    st.info("No incidents. Send a staff report from the Simulator, or report a problem as a guest.")

for incident in incidents:
    with st.container(border=True):
        st.markdown(f"**{names.get(incident['property_id'], incident['property_id'])}**")
        status_badge(incident["status"])
        st.markdown(incident["description"])
        st.caption(STATUS_HELP.get(incident["status"], ""))

        details = [
            f"reported by {incident['reporter'] or 'unknown'} ({incident['reported_via']})",
            f"severity {incident['severity']}",
        ]
        if incident.get("report_count", 1) > 1:
            details.append(f"{incident['report_count']} reports")
        if incident["booking_id"]:
            details.append(f"affects {guests.get(incident['booking_id'], incident['booking_id'])}")
        if incident["relocation_property_id"]:
            details.append(f"relocation option: {names.get(incident['relocation_property_id'], '?')}")
        st.caption(" · ".join(details))

        facts = []
        if incident.get("eta_source"):
            eta = incident.get("eta_hours")
            facts.append(f"Fix estimate: {f'{eta:g} h' if eta else 'unknown'} ({incident['eta_source']})")
        if incident.get("recommendation"):
            facts.append(f"Recommendation: {incident['recommendation']}. {incident.get('recommendation_reason') or ''}")
        if incident.get("guest_answer"):
            facts.append(f"Guest {GUEST_ANSWERS.get(incident['guest_answer'], incident['guest_answer'])}.")
        if incident["resolution"]:
            facts.append(f"Resolution: {incident['resolution']}")
        if facts:
            st.markdown(multiline("\n".join(facts)))

        booking = bookings.get(incident.get("booking_id"))
        if incident["status"] == "relocated" and booking and booking["property_id"] != incident["relocation_property_id"]:
            st.warning("The booking still belongs to another property. This relocation has not been applied in the PMS.")
            if st.button("Review unapplied relocation", key=f"review_relocation_{incident['id']}"):
                if act(api.post, f"/incidents/{incident['id']}/review-relocation", wait=True) is not None:
                    flash("A new relocation review was requested. Check Approvals / Answers.")
                    st.rerun()

        if incident["status"] == "awaiting_guest":
            st.caption(
                "The guest answers in the chat and the copilot passes the answer on. If you "
                "settled it with the guest directly, record their answer here."
            )
            accept_column, stay_column, _ = st.columns([2, 2, 5])
            if accept_column.button("Guest accepted the move", key=f"accept_{incident['id']}", width="stretch"):
                record_answer(incident, "accept")
            if stay_column.button("Guest stays", key=f"stay_{incident['id']}", width="stretch"):
                record_answer(incident, "stay")

        if incident.get("reports") and len(incident["reports"]) > 1:
            with st.expander(f"Reports ({len(incident['reports'])})"):
                for report in incident["reports"]:
                    st.markdown(
                        f"- {short_time(report['created_at'])} · {report['reporter'] or report['reported_via']}: "
                        f"{report['description']}"
                    )

        messages = incident.get("staff_messages") or []
        if messages:
            with st.expander(f"Conversation with staff ({len(messages)})"):
                for message in messages:
                    who = "Agent" if message["direction"] == "to_staff" else message["staff_name"] or "Staff"
                    tag = " (simulated)" if message.get("simulated") else ""
                    st.markdown(f"**{who}{tag}** · {short_time(message['created_at'])}  \n{message['body']}")

        tasks = incident.get("tasks") or []
        if tasks:
            st.markdown("**Tasks**")
            for task in tasks:
                done = task["status"] == "done"
                line = f"{'✅' if done else '🛠️'} `{task['id']}` {task['description']} · {task['staff_name'] or 'unassigned'}"
                if done and task.get("completion_note"):
                    line += f"  \n  _{task['completion_note']}_"
                st.markdown(line)

        st.caption(
            f"opened {short_time(incident['created_at'])} UTC · updated {short_time(incident['updated_at'])} UTC"
        )
