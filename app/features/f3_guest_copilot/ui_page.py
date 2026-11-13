"""Pagina F3: conversațiile cu oaspeții și rulările copilotului. Consumă doar API-ul REST."""

from __future__ import annotations

import streamlit as st

from ui.components import booking_label, load, multiline, property_names, short_time, status_badge

FEATURE = "f3_guest_copilot"

st.title("F3 · Guest copilot")
st.caption(
    "Answers guests from our own data. Anything that needs an exception goes to the host first; "
    "a reported problem is handed to the incident flow through an event."
)

bookings = load("/bookings")
names = property_names(load("/properties"))
runs = load("/agent/runs", feature=FEATURE, limit=100)
if not runs:
    st.info("The copilot has not handled any message yet. Use the guest chat in the Simulator.")
    st.stop()

with_runs = {run["booking_id"] for run in runs}
booking = st.selectbox(
    "Conversation", [b for b in bookings if b["id"] in with_runs], format_func=lambda b: booking_label(b, names)
)

conversation_column, runs_column = st.columns(2)
with conversation_column:
    st.subheader("Conversation")
    for message in load(f"/bookings/{booking['id']}/messages"):
        with st.chat_message("user" if message["sender"] == "guest" else "assistant"):
            st.markdown(message["body"])
            st.caption(f"{message['sender']} · {short_time(message['created_at'])} UTC")

with runs_column:
    st.subheader("Copilot runs")
    for run in [r for r in runs if r["booking_id"] == booking["id"]]:
        with st.container(border=True):
            status_badge(run["status"])
            st.markdown(multiline(run["result"] or run["error"] or ""))
            st.caption(
                f"{short_time(run['started_at'])} UTC · {run['steps']} steps · "
                f"{run['prompt_tokens'] + run['completion_tokens']} tokens · "
                "full detail under Agent timeline"
            )
