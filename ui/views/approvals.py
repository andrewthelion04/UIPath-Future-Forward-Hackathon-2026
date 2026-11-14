"""Inboxul de aprobări al hostului (human-in-the-loop)."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, flash, flash_outcome, load, multiline, short_time, show_flash

st.title("Approvals / Answers")
st.caption("Approve proposed actions or answer guests directly in their booking conversation.")
show_flash()

answers = load("/answers")
for answer in answers:
    with st.container(border=True):
        st.subheader(f"Reply to {answer['guest_name']} · {answer['property_name']}")
        st.caption(f"Guest question · {short_time(answer['created_at'])} UTC")
        st.write(answer["question"])
        if answer["context_note"]:
            st.caption(answer["context_note"])
        with st.expander("Conversation context"):
            for message in load(f"/bookings/{answer['booking_id']}/messages", limit=12):
                st.text(f"{message['sender']}: {message['body']}")
        uncertain = answer["status"] == "sending"
        if uncertain:
            st.warning("Delivery is not confirmed. Check the PMS conversation before sending another message.")
            delivered = st.radio("After checking the PMS conversation:", ["The message was delivered", "The message was not delivered"], key=f"delivery_{answer['id']}")
            if st.button("Confirm delivery check", key=f"check_{answer['id']}"):
                if act(api.post, f"/answers/{answer['id']}/delivery-check", {"delivered":delivered == "The message was delivered"}) is not None:
                    st.rerun()
        with st.form(f"answer_{answer['id']}"):
            text = st.text_area("Your reply", value=answer.get("reply") or "", max_chars=4000,
                                key=f"reply_{answer['id']}")
            st.caption("Your exact message will be sent to the guest through their booking's PMS.")
            if st.form_submit_button("Send reply to guest", disabled=uncertain):
                if not text.strip():
                    st.error("Enter a reply before sending.")
                elif act(api.post, f"/answers/{answer['id']}/reply", {"text":text}) is not None:
                    flash("Your reply was sent to the guest.")
                    st.rerun()

pending = load("/approvals")
if not pending and not answers:
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

with st.expander("Sent answers"):
    for answer in load("/answers", status="all"):
        if answer["status"] == "sent":
            st.markdown(f"**{answer['guest_name']} · {answer['property_name']}**")
            st.text(answer["question"])
            st.text(answer["reply"])

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
