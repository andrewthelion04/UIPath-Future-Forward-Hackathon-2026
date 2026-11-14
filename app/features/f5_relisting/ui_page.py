"""Pagina F5: propunerile de relistare după anulare. Consumă doar API-ul REST."""

from __future__ import annotations

import streamlit as st

from ui.components import load, property_names, short_time, status_badge

st.title("F5 · Relisting after cancellation")
st.caption(
    "After a last-minute cancellation, the cheapest comparable listing with the same amenities "
    "sets the reference. Nightly prices keep the calendar's proportions, respect each night's "
    "host floor and regular price, expire at check-in, and "
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
        regular.metric("Base reference", f"{proposal['base_price']:g} {currency}")
        comparable.metric(
            "Cheapest comparable",
            f"{proposal['comparable_price']:g} {currency}" if proposal["comparable_price"] else "none",
        )
        proposed.metric(
            "Proposed base reference",
            f"{proposal['proposed_price']:g} {currency}" if proposal["proposed_price"] else "none",
        )
        floor.metric("Base floor", f"{proposal['floor_price']:g} {currency}")
        nightly = proposal.get("nightly_prices", [])
        if nightly:
            st.dataframe([
                {"Night": n["day"], "Regular price": n["original_price"],
                 "Host floor": n["floor_price"], "Proposed price": n["proposed_price"],
                 "Currency": currency}
                for n in nightly
            ], hide_index=True)
        if proposal.get("expires_at"):
            st.caption(f"Expires at check-in: {proposal['expires_at']}")
        if proposal["status"] == "expired":
            st.warning("This proposal has expired. Approving it will leave the calendar unchanged.")
        elif proposal["status"] == "conflict":
            st.warning("The calendar changed after this proposal. Review the nights in your PMS.")
        details = [f"cancelled by {guests.get(proposal['booking_id'], proposal['booking_id'])}"]
        if proposal["comparable_title"]:
            details.append(f"comparable: {proposal['comparable_title']}")
        details.append(f"market source: {proposal['market_source']}")
        st.caption(" · ".join(details))
        if proposal.get("market_attribution"):
            st.caption(proposal["market_attribution"])
        if proposal["note"]:
            st.markdown(proposal["note"])
        st.caption(f"created {short_time(proposal['created_at'])} UTC")
