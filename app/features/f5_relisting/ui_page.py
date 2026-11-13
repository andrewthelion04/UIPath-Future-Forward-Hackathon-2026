"""Pagina F5: propunerile de relistare după anulare. Consumă doar API-ul REST."""

from __future__ import annotations

import streamlit as st

from ui.components import load, property_names, short_time, status_badge

st.title("F5 · Relisting after cancellation")
st.caption(
    "After a last-minute cancellation, the cheapest comparable listing with the same amenities "
    "sets the reference. The proposed price is slightly lower, never below the host's limit, and "
    "reaches the calendar only after approval."
)

proposals = load("/f5/proposals")
names = property_names(load("/properties"))
guests = {b["id"]: b["guest_name"] for b in load("/bookings")}

attribution = load("/health").get("market_attribution")
if attribution:
    st.caption(attribution)  # creditul sursei de date (ex. Inside Airbnb, CC BY 4.0)

if not proposals:
    st.info("No proposals yet. Cancel a booking that starts within a few days from the Simulator.")

for proposal in proposals:
    with st.container(border=True):
        name = names.get(proposal["property_id"], proposal["property_id"])
        st.markdown(f"**{name}** · {proposal['start_date']} → {proposal['end_date']}")
        status_badge(proposal["status"])
        currency = proposal["currency"]
        regular, comparable, proposed, floor = st.columns(4)
        regular.metric("Regular price", f"{proposal['base_price']:g} {currency}")
        comparable.metric(
            "Cheapest comparable",
            f"{proposal['comparable_price']:g} {currency}" if proposal["comparable_price"] else "none",
        )
        proposed.metric(
            "Proposed price",
            f"{proposal['proposed_price']:g} {currency}" if proposal["proposed_price"] else "none",
        )
        floor.metric("Host floor", f"{proposal['floor_price']:g} {currency}")
        details = [f"cancelled by {guests.get(proposal['booking_id'], proposal['booking_id'])}"]
        if proposal["comparable_title"]:
            details.append(f"comparable: {proposal['comparable_title']}")
        details.append(f"market source: {proposal['market_source']}")
        st.caption(" · ".join(details))
        if proposal["note"]:
            st.markdown(proposal["note"])
        st.caption(f"created {short_time(proposal['created_at'])} UTC")
