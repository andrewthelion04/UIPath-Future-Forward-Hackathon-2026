"""Jurnalul de evenimente: ce s-a publicat, cine a preluat și cum a ieșit fiecare handler."""

from __future__ import annotations

import streamlit as st

from ui.components import load, short_time

st.title("Events")
st.caption(
    "Features talk only through events. Each handler runs isolated: one that fails or times "
    "out shows up here and never blocks the others."
)

events = load("/events", limit=200)
if not events:
    st.info("No events yet. Trigger a flow from the Simulator.")
    st.stop()

flows = list(dict.fromkeys(event["correlation_id"] for event in events))
labels = {
    flow: next(
        f"{short_time(e['occurred_at'])} · started by {e['type']} ({e['source']})"
        for e in reversed(events)
        if e["correlation_id"] == flow
    )
    for flow in flows
}
selected = st.selectbox(
    "Flow", [None, *flows], format_func=lambda flow: labels[flow] if flow else "All flows, newest first"
)
if selected:
    events = [event for event in reversed(events) if event["correlation_id"] == selected]

for event in events:
    handlers = event["handlers"]
    failed = [h for h in handlers if h["status"] not in ("ok", "running")]
    summary = ", ".join(f"{h['handler']}: {h['status']}" for h in handlers) or "no subscriber"
    icon = ":material/error:" if failed else ":material/bolt:"
    title = f"{short_time(event['occurred_at'])} · {event['type']} · from {event['source']} · {summary}"
    with st.expander(title, icon=icon, expanded=bool(failed)):
        st.json(event["payload"])
        for handler in handlers:
            if handler["error"]:
                st.error(f"{handler['handler']}: {handler['error']}")
        if handlers:
            st.dataframe(
                [
                    {
                        "Handler": h["handler"],
                        "Feature": h["feature"],
                        "Status": h["status"],
                        "Duration (ms)": h["duration_ms"],
                    }
                    for h in handlers
                ],
                hide_index=True,
            )
        st.caption(f"event {event['id']} · flow {event['correlation_id']}")
