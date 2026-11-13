"""Pagina F2: incidentele și starea lor. Consumă doar API-ul REST."""

from __future__ import annotations

import streamlit as st

from ui.components import load, property_names, short_time, status_badge

st.title("F2 · Incidents")
st.caption(
    "A report from staff or from a guest opens an incident. The host chooses between fixing it "
    "in time and relocating the guest; nothing happens before that decision."
)

incidents = load("/incidents")
names = property_names(load("/properties"))
guests = {b["id"]: b["guest_name"] for b in load("/bookings")}

if not incidents:
    st.info("No incidents. Send a staff report from the Simulator, or report a problem as a guest.")

for incident in incidents:
    with st.container(border=True):
        st.markdown(f"**{names.get(incident['property_id'], incident['property_id'])}**")
        status_badge(incident["status"])
        st.markdown(incident["description"])
        details = [
            f"reported by {incident['reporter'] or 'unknown'} ({incident['reported_via']})",
            f"severity {incident['severity']}",
        ]
        if incident["booking_id"]:
            details.append(f"affects {guests.get(incident['booking_id'], incident['booking_id'])}")
        if incident["relocation_property_id"]:
            details.append(f"relocation option: {names.get(incident['relocation_property_id'], '?')}")
        st.caption(" · ".join(details))
        if incident["resolution"]:
            st.markdown(f"Resolution: {incident['resolution']}")
        st.caption(f"opened {short_time(incident['created_at'])} UTC · updated {short_time(incident['updated_at'])} UTC")
