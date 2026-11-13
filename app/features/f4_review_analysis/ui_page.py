"""Pagina F4: recomandările rezultate din recenzii. Consumă doar API-ul REST."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, flash, load, multiline, property_names, short_time, show_flash

st.title("F4 · Review analysis")
st.caption(
    "Reviews come from the configured source, are saved as a dated snapshot, and an agent turns "
    "them into recommendations. If the source fails, the last snapshot is used and you are told."
)
show_flash()

properties = load("/properties")
names = property_names(properties)
everything = {"id": None, "name": "All properties"}
choice = st.selectbox("Property", [everything, *properties], format_func=lambda p: p["name"])
if st.button("Analyse reviews", type="primary"):
    if act(api.post, "/sim/review-analysis", {"property_id": choice["id"]}, wait=True) is not None:
        flash(f"Analysis finished for {choice['name']}.")
        st.rerun()

insights = load("/f4/insights", property_id=choice["id"])
if not insights:
    st.info("No analysis yet for this selection.")

for insight in insights:
    with st.container(border=True):
        st.markdown(f"**{names.get(insight['property_id'], insight['property_id'])}**")
        if insight["used_fallback"]:
            st.badge("based on the last saved snapshot", icon=":material/warning:", color="orange")
        st.markdown(multiline(insight["recommendations"]))
        if insight["evidence"]:
            with st.expander("What guests wrote"):
                for quote in insight["evidence"]:
                    st.markdown(f"- {quote}")
        st.caption(
            f"{insight['review_count']} reviews · average {insight['average_rating']} · "
            f"{short_time(insight['created_at'])} UTC"
        )
