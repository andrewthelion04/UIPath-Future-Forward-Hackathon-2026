"""Datele lumii: proprietăți, rezervări, calendar, conexiuni PMS și corespondențe de ID-uri."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, flash, load, property_names, show_flash

st.title("World data")
show_flash()

properties = load("/properties")
names = property_names(properties)
properties_tab, bookings_tab, calendar_tab, integrations_tab, host_tab = st.tabs(
    ["Properties", "Bookings", "Calendar", "PMS connections", "Host rules"]
)

with properties_tab:
    st.dataframe(
        [
            {
                "Name": p["name"],
                "City": p["city"],
                "Bedrooms": p["bedrooms"],
                "Sleeps": p["max_guests"],
                "Base price": f"{p['base_price']:g} {p['currency']}",
                "Amenities": ", ".join(p["amenities"]),
                "PMS": p["pms_provider"],
                "ID": p["id"],
            }
            for p in properties
        ],
        hide_index=True,
    )

with bookings_tab:
    st.dataframe(
        [
            {
                "Guest": b["guest_name"],
                "Property": names.get(b["property_id"], "?"),
                "Check-in": b["check_in"],
                "Check-out": b["check_out"],
                "Guests": b["guest_count"],
                "Status": b["status"],
                "PMS": b["pms_provider"],
                "Channel": b["source_channel"],
                "ID": b["id"],
            }
            for b in load("/bookings")
        ],
        hide_index=True,
    )

with calendar_tab:
    if properties:
        prop = st.selectbox("Property", properties, format_func=lambda p: p["name"], key="calendar_property")
        days = st.slider("Days ahead", 7, 60, 21)
        try:
            calendar = api.get(f"/properties/{prop['id']}/calendar", days=days)
        except api.ApiError as exc:
            st.warning(f"The calendar of this PMS connection cannot be read: {exc}")
        else:
            st.dataframe(
                [
                    {
                        "Day": d["day"],
                        "Available": "yes" if d["available"] else "booked / blocked",
                        "Price": f"{d['price']:g} {d['currency']}",
                    }
                    for d in calendar
                ],
                hide_index=True,
            )

with integrations_tab:
    connections = load("/integrations")
    for connection in connections:
        with st.container(border=True):
            selected = " · selected in config" if connection["selected"] else ""
            st.markdown(f"**{connection['provider']}**{selected}")
            enabled = [name for name, on in connection["capabilities"].items() if on]
            st.caption("Capabilities: " + (", ".join(enabled) or "none"))
            if st.button("Sync properties and bookings", key=f"sync_{connection['provider']}"):
                report = act(api.post, f"/integrations/{connection['provider']}/sync")
                if report is not None:
                    flash(
                        f"{connection['provider']}: {report['properties_created']} new and "
                        f"{report['properties_updated']} updated properties, "
                        f"{report['bookings_created']} new and {report['bookings_updated']} updated "
                        f"bookings, {report['cancellations_detected']} cancellations."
                    )
                    st.rerun()
    st.markdown("**ID mappings** (external IDs live only here and in the adapters)")
    st.dataframe(load("/integrations/mappings"), hide_index=True)

with host_tab:
    host = load("/host")
    st.markdown(f"**{host['name']}**")
    st.json(host["rules"])
