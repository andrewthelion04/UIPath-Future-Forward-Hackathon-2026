"""Datele lumii: proprietăți, rezervări, calendar, conexiuni PMS și corespondențe de ID-uri."""

from __future__ import annotations

import streamlit as st
from datetime import date
import calendar as month_dates
from ui.month_calendar import month_html

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
    if st.session_state.pop("clear_property_editor", False):
        st.session_state.pop("property_editor", None)
    st.caption("Edit property details below and save. Changes apply in StayPilot, survive PMS sync, and do not update OwnerRez.")
    rows = [
        {"ID": p["id"], "Name": p["name"], "City": p["city"], "Address": p["address"],
         "Bedrooms": p["bedrooms"], "Sleeps": p["max_guests"], "Base price": p["base_price"],
         "Currency": p["currency"], "Amenities": ", ".join(p["amenities"]),
         "Listing URL": p.get("listing_url") or "", "PMS": p["pms_provider"]} for p in properties
    ]
    edited = st.data_editor(rows, hide_index=True, disabled=["ID", "PMS"], key="property_editor",
        column_config={"Bedrooms": st.column_config.NumberColumn(min_value=0, step=1),
                       "Sleeps": st.column_config.NumberColumn(min_value=1, step=1),
                       "Base price": st.column_config.NumberColumn(min_value=0)})
    if st.button("Save property changes", type="primary"):
        fields = {"Name":"name", "City":"city", "Address":"address", "Bedrooms":"bedrooms",
                  "Sleeps":"max_guests", "Base price":"base_price", "Currency":"currency",
                  "Amenities":"amenities", "Listing URL":"listing_url"}
        saved, failed = 0, False
        for before, after in zip(rows, edited):
            changes = {field: after[label] for label, field in fields.items() if after[label] != before[label]}
            if "amenities" in changes:
                changes["amenities"] = [a.strip() for a in changes["amenities"].split(",") if a.strip()]
            if "listing_url" in changes:
                changes["listing_url"] = changes["listing_url"] or None
            if changes:
                result = act(api.put, f"/properties/{before['ID']}", changes)
                if result is None:
                    failed = True
                else:
                    saved += 1
        if not failed:
            flash(f"Saved {saved} properties.")
            st.session_state.clear_property_editor = True
            st.rerun()
        elif saved:
            st.warning(f"Saved {saved} properties. Other rows failed; correct the errors and save again.")
    with st.expander("Restore imported property details"):
        if properties:
            restore = st.selectbox("Property to restore", properties, format_func=lambda p:p["name"], key="restore_property")
            if st.button("Discard local edits for this property"):
                if act(api.request, "DELETE", f"/properties/{restore['id']}/overrides") is not None:
                    flash("Restored the most recently imported details.")
                    st.session_state.clear_property_editor = True
                    st.rerun()

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
        today = date.fromisoformat(load("/health")["today"])
        if "calendar_month" not in st.session_state:
            st.session_state.calendar_month = today.replace(day=1)
        prev, current, following = st.columns([1, 4, 1])
        if prev.button("← Previous", key="calendar_prev"):
            d = st.session_state.calendar_month
            st.session_state.calendar_month = date(d.year-1,12,1) if d.month == 1 else date(d.year,d.month-1,1)
        if following.button("Next →", key="calendar_next"):
            d = st.session_state.calendar_month
            st.session_state.calendar_month = date(d.year+1,1,1) if d.month == 12 else date(d.year,d.month+1,1)
        first = st.session_state.calendar_month
        current.subheader(first.strftime("%B %Y"))
        prop = st.selectbox("Property", [{"id":"all", "name":"All properties"}, *properties], format_func=lambda p: p["name"], key="calendar_property")
        shown = properties if prop["id"] == "all" else [prop]
        weeks = month_dates.Calendar().monthdatescalendar(first.year, first.month)
        calendar = []
        try:
            if prop["id"] != "all":
                calendar = api.get(f"/properties/{prop['id']}/calendar", start=weeks[0][0].isoformat(), days=len(weeks)*7)
        except api.ApiError as exc:
            st.warning(f"The calendar of this PMS connection cannot be read: {exc}")
        st.html(month_html(first.year, first.month, shown, load("/bookings"), calendar, today))
        st.caption("Booking bars end at check-out (exclusive). Select a property to read availability and rates from its PMS; the overview shows imported bookings only.")

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
