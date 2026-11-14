"""Inboxul de aprobări al hostului (human-in-the-loop)."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, flash_outcome, load, multiline, short_time, show_flash

st.title("Approvals")
st.caption("Every action with impact waits here for the host's decision.")
show_flash()

pending = load("/approvals")
if not pending:
    st.info("Nothing is waiting for a decision.")


def decide(approval: dict, decision: str, option: str | None, note: str) -> None:
    body = {"decision": decision, "option": option, "note": note or None}
    result = act(api.post, f"/approvals/{approval['id']}/decision", body, wait=True)
    if result is not None:
        verb = "Approved" if decision == "approve" else "Rejected"
        flash_outcome(
            result,
            f"{verb}: {approval['title']}",
            f"{verb}: {approval['title']}. The action is still running; see Notifications for the result.",
        )
        st.rerun()


for approval in pending:
    with st.container(border=True):
        st.subheader(approval["title"])
        st.caption(f"{approval['feature']} · {approval['kind']} · {short_time(approval['created_at'])} UTC")
        st.markdown(multiline(approval["summary"]))

        options = approval["options"]
        chosen = options[0]["key"]
        if len(options) > 1:
            chosen = st.radio(
                "Choose",
                [option["key"] for option in options],
                format_func=lambda key, options=options: next(o["label"] for o in options if o["key"] == key),
                captions=[option["description"] for option in options],
                key=f"option_{approval['id']}",
            )
        elif options[0].get("description"):
            # O singură opțiune: descrierea ei (ex. textul exact pentru oaspete) e tot ce aprobă hostul.
            st.markdown(multiline(options[0]["description"]))
        note = st.text_input("Note (optional)", key=f"note_{approval['id']}")
        approve_column, reject_column, _ = st.columns([1, 1, 4])
        label = "Approve" if len(options) > 1 else options[0]["label"]
        if approve_column.button(label, type="primary", key=f"approve_{approval['id']}", width="stretch"):
            decide(approval, "approve", chosen, note)
        if reject_column.button("Reject", key=f"reject_{approval['id']}", width="stretch"):
            decide(approval, "reject", None, note)

with st.expander("Decided approvals"):
    decided = [a for a in load("/approvals", status="all") if a["status"] != "pending"]
    if not decided:
        st.caption("No decisions yet.")
    else:
        st.dataframe(
            [
                {
                    "Decided": short_time(a["decided_at"]),
                    "Feature": a["feature"],
                    "Title": a["title"],
                    "Decision": a["status"],
                    "Option": a["chosen_option"],
                    "By": a["decided_by"],
                    "Note": a["decision_note"],
                }
                for a in decided
            ],
            hide_index=True,
        )
