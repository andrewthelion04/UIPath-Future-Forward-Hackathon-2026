"""Pagina F1: tester de webhook și jurnalul payload-urilor brute. Consumă doar API-ul REST."""

from __future__ import annotations

import json

import streamlit as st

from ui import api_client as api
from ui.components import act, flash, load, short_time, show_flash, status_badge

st.title("F1 · PMS integration")
st.caption(
    "Transport only: an inbound payload is saved raw, filtered (anti-loop), deduplicated, "
    "mapped to an internal booking and published as GuestMessageReceived."
)
show_flash()

connections = [c["provider"] for c in load("/integrations")]
mappings = load("/integrations/mappings")


def mapped_reservation(provider: str, fallback: str) -> str:
    return next(
        (m["external_id"] for m in mappings
         if m["entity_type"] == "reservation" and m["connection"] == provider),
        fallback,
    )


reservation = mapped_reservation("mock", "MOCK-RES-1001")


def sample(message_id: str, sender_type: str = "guest", reservation_id: str = reservation) -> str:
    return json.dumps(
        {
            "event": "message.created",
            "message": {
                "id": message_id,
                "reservation_id": reservation_id,
                "sender_type": sender_type,
                "body": "What time is check-in?",
                "sent_at": "2026-06-01T09:30:00+00:00",
            },
        },
        indent=2,
    )


SAMPLES = {
    "Guest message": sample("DEMO-MSG-1"),
    "Same message again (duplicate)": sample("DEMO-MSG-1"),
    "Our own message echoed back (anti-loop)": sample("DEMO-MSG-2", sender_type="host"),
    "Unmapped reservation": sample("DEMO-MSG-3", reservation_id="MOCK-RES-UNKNOWN"),
    "Not a message event": json.dumps({"event": "reservation.updated"}, indent=2),
    "OwnerRez · guest message": json.dumps(
        {
            "action": "entity_create",
            "entity_type": "thread_message",
            "entity_id": 3001,
            "entity": {
                "body": "What time is check-in?",
                "from_role": "Guest",
                "date_utc": "2026-06-01T09:30:00Z",
                "thread": {"booking_id": mapped_reservation("ownerrez", "501")},
            },
        },
        indent=2,
    ),
    "OwnerRez · reservation cancelled": json.dumps(
        {
            "action": "entity_update",
            "entity_type": "booking",
            "entity_id": mapped_reservation("ownerrez", "501"),
            "entity": {"status": "canceled"},
        },
        indent=2,
    ),
    "Hospitable · guest message": json.dumps(
        {
            "action": "message.created",
            "data": {
                "id": "DEMO-HSP-MSG-1",
                "reservation_id": mapped_reservation("hospitable", "res-uuid-1"),
                "sender_type": "guest",
                "body": "What time is check-in?",
            },
        },
        indent=2,
    ),
    "Hospitable · reservation cancelled": json.dumps(
        {
            "action": "reservation.cancelled",
            "data": {"id": mapped_reservation("hospitable", "res-uuid-1")},
        },
        indent=2,
    ),
}

st.subheader("Webhook tester")
provider = st.selectbox("PMS connection", connections)
choice = st.selectbox(
    "Sample payload", list(SAMPLES), help="Choose the matching PMS connection and edit the IDs before sending."
)
if "reservation cancelled" in choice:
    st.caption("Reservation webhooks sync the current PMS state. Cancel the booking in the PMS first.")
raw = st.text_area("Payload", SAMPLES[choice], height=230, key=f"payload_{choice}")
if st.button("POST to webhook", type="primary"):
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        st.error(f"Not valid JSON: {exc}")
    else:
        result = act(api.post, f"/webhooks/{provider}", payload)
        if result is not None:
            st.markdown(f"`POST /webhooks/{provider}` answered 200 with:")
            status_badge(result["status"])
            if result["detail"]:
                st.caption(result["detail"])

st.subheader("Inbound log")
log = load("/f1/webhook-log", limit=50)
unmapped_providers = sorted({entry["provider"] for entry in log if entry["outcome"] == "unmapped"})
if unmapped_providers:
    retry_provider = st.selectbox("Reprocess messages from", ["All connections", *unmapped_providers])
    if st.button("Reprocess unmapped messages"):
        result = act(
            api.post,
            "/f1/reprocess-unmapped",
            provider=None if retry_provider == "All connections" else retry_provider,
            wait=True,
        )
        if result is not None:
            flash(f"Published {result['reprocessed']} message(s); {result['remaining']} still unmapped.")
            st.rerun()
if not log:
    st.caption("Nothing received yet.")
for entry in log:
    title = f"{short_time(entry['received_at'])} · {entry['source']} · {entry['outcome']}"
    with st.expander(title):
        status_badge(entry["outcome"])
        if entry["detail"]:
            st.caption(entry["detail"])
        st.json(entry["payload"])
