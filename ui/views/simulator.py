"""Simulatorul: declanșează cele cinci fluxuri pe lumea simulată."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, booking_label, flash, flash_outcome, load, property_names, show_flash

SAMPLE_MESSAGES = {
    "Ask about check-in": "Hi! What time is check-in, and is there wifi?",
    "Request early check-in": "Could we check in early, around noon?",
    "Report a problem": "The air conditioning is broken and it is very hot in here.",
}

st.title("Simulator")
st.caption(
    "Trigger each flow against the simulated world, then follow it under Approvals, "
    "Notifications, Agent timeline and Events."
)
show_flash()

properties = load("/properties")
bookings = load("/bookings")
names = property_names(properties)
today = load("/health")["today"]
# Doar sejururile în curs sau viitoare de pe PMS-ul simulat; cele încheiate nu au ce simula.
active = [
    b
    for b in bookings
    if b["pms_provider"] == "mock" and b["status"] == "confirmed" and b["check_out"] > today
]

chat_tab, staff_tab, cancel_tab, reviews_tab, reset_tab = st.tabs(
    ["Guest chat", "Staff report", "Cancel booking", "Review analysis", "Reset world"]
)

# --- F1 + F3: mesaj de la oaspete ---------------------------------------------------
with chat_tab:
    if not active:
        st.info("No confirmed booking on the simulated PMS. Reset the world to get them back.")
    else:
        booking = st.selectbox(
            "Guest", active, format_func=lambda b: booking_label(b, names), key="chat_booking"
        )

        def send(text: str) -> None:
            result = act(api.post, "/sim/guest-message", {"booking_id": booking["id"], "text": text}, wait=True)
            if result is not None:
                if result["outcome"] != "published":
                    flash(f"Message was not processed: {result['outcome']} {result['detail']}")
                st.rerun()

        with st.container(border=True, height=380):
            messages = load(f"/bookings/{booking['id']}/messages")
            if not messages:
                st.caption("No messages yet. Write as the guest below.")
            for message in messages:
                is_guest = message["sender"] == "guest"
                with st.chat_message("user" if is_guest else "assistant"):
                    st.markdown(message["body"])
                    label = booking["guest_name"] if is_guest else message["sender"]
                    failed = " · not delivered" if message["status"] == "failed" else ""
                    st.caption(f"{label} · {message['created_at'][11:19]} UTC{failed}")

        columns = st.columns(len(SAMPLE_MESSAGES))
        for column, (label, text) in zip(columns, SAMPLE_MESSAGES.items(), strict=True):
            if column.button(label, key=f"sample_{label}", width="stretch"):
                send(text)
        typed = st.chat_input(f"Write as {booking['guest_name']}", key="chat_input")
        if typed:
            send(typed)

# --- F2: sesizare de la personal ----------------------------------------------------
with staff_tab:
    staff = load("/staff")
    with st.form("staff_report"):
        reporter = st.selectbox(
            "Reported by", staff, format_func=lambda m: f"{m['name']} ({m['role']})", key="staff_reporter"
        )
        prop = st.selectbox("Property", properties, format_func=lambda p: p["name"], key="staff_property")
        description = st.text_area("What is wrong?", "Water is leaking under the kitchen sink.")
        severity = st.segmented_control("Severity", ["low", "medium", "high"], default="medium")
        if st.form_submit_button("Send report", type="primary", key="staff_submit"):
            body = {
                "property_id": prop["id"],
                "description": description,
                "staff_id": reporter["id"] if reporter else None,
                "severity": severity or "medium",
            }
            result = act(api.post, "/sim/staff-report", body, wait=True)
            if result is not None:
                flash_outcome(
                    result,
                    f"Report sent for {prop['name']}. The host now has a decision under Approvals.",
                    f"Report sent for {prop['name']}. The agent is still working on it; the "
                    "decision will appear under Approvals.",
                )
                st.rerun()

# --- F5: anulare de rezervare -------------------------------------------------------
with cancel_tab:
    st.caption(
        "Cancels the reservation in the simulated PMS. The next sync notices it and emits "
        "BookingCancelled, exactly as with a real PMS."
    )
    if not active:
        st.info("No confirmed booking left to cancel.")
    else:
        target = st.selectbox(
            "Booking to cancel", active, format_func=lambda b: booking_label(b, names), key="cancel_booking"
        )
        if st.button("Cancel this booking", type="primary"):
            result = act(api.post, "/sim/cancel-booking", {"booking_id": target["id"]}, wait=True)
            if result is not None:
                flash_outcome(
                    result,
                    f"{target['guest_name']}'s booking was cancelled. Check Approvals and Notifications.",
                    f"{target['guest_name']}'s booking was cancelled. The relisting proposal is "
                    "still being prepared; check Approvals in a moment.",
                )
                st.rerun()

# --- F4: analiza recenziilor --------------------------------------------------------
with reviews_tab:
    everything = {"id": None, "name": "All properties"}
    choice = st.selectbox("Property", [everything, *properties], format_func=lambda p: p["name"])
    if st.button("Analyse reviews", type="primary"):
        result = act(api.post, "/sim/review-analysis", {"property_id": choice["id"]}, wait=True)
        if result is not None:
            flash_outcome(
                result,
                f"Review analysis finished for {choice['name']}. See Notifications.",
                f"Review analysis for {choice['name']} is still running; the result arrives in "
                "Notifications.",
            )
            st.rerun()

# --- Reset --------------------------------------------------------------------------
with reset_tab:
    st.warning(
        "Deletes every message, approval, notification, incident and agent run, then rebuilds "
        "the simulated world from seed/world.json."
    )
    confirmed = st.checkbox("I understand, reset everything")
    if st.button("Reset world", disabled=not confirmed):
        summary = act(api.post, "/sim/reset")
        if summary is not None:
            flash(
                f"World reset: {summary['properties']} properties, {summary['bookings']} bookings, "
                f"{summary['reviews']} reviews."
            )
            st.rerun()
