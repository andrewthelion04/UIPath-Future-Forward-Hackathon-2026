"""Pagina F4: recomandările rezultate din recenzii. Consumă doar API-ul REST."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import act, flash, load, multiline, property_names, short_time, show_flash

PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PRIORITY_COLOR = {"high": "red", "medium": "orange", "low": "blue"}

st.title("F4 · Review analysis")
st.caption(
    "Reviews come from the configured source, are saved as a dated snapshot (only when they "
    "changed), and an agent turns them into prioritised recommendations. If the source fails, "
    "the last snapshot is used and you are told."
)
show_flash()

properties = load("/properties")
names = property_names(properties)
everything = {"id": None, "name": "All properties"}
choice = st.selectbox("Property", [everything, *properties], format_func=lambda p: p["name"])
if st.button("Analyse reviews", type="primary"):
    if act(api.post, "/sim/review-analysis", {"property_id": choice["id"]}, wait=True) is not None:
        flash(f"Analysis finished for {choice['name']}. See Notifications for what changed.")
        st.rerun()

insights = load("/f4/insights", property_id=choice["id"])
if not insights:
    st.info("No analysis yet for this selection.")


def show_changes(changes: dict | None) -> None:
    if not changes:
        st.caption("First analysis for this property.")
        return
    cols = st.columns(3)
    for col, key, label in zip(
        cols, ("new", "persisting", "resolved"), ("New", "Still present", "No longer mentioned"), strict=True
    ):
        col.markdown(f"**{label}**  \n" + (", ".join(changes[key]) or "—"))


for insight in insights:
    with st.container(border=True):
        st.markdown(f"**{names.get(insight['property_id'], insight['property_id'])}**")
        if insight["used_fallback"]:
            st.badge("based on the last saved snapshot", icon=":material/warning:", color="orange")
        items = sorted(insight.get("items") or [], key=lambda i: PRIORITY_RANK.get(i["priority"], 9))
        if items:
            for item in items:
                st.badge(item["priority"].upper(), color=PRIORITY_COLOR.get(item["priority"], "gray"))
                st.markdown(f"**{item['theme']}** — {item['action']}")
                for quote in item["quotes"]:
                    st.caption(f"“{quote}”")
        else:
            st.markdown(multiline(insight["recommendations"]))
        show_changes(insight.get("changes"))
        if insight["evidence"]:
            with st.expander("What guests wrote"):
                for quote in insight["evidence"]:
                    st.markdown(f"- {quote}")
        st.caption(
            f"{insight['review_count']} reviews · average {insight['average_rating']} · "
            f"{short_time(insight['created_at'])} UTC"
        )

snapshots = load("/f4/snapshots", property_id=choice["id"])
if snapshots:
    with st.expander(f"Saved review snapshots ({len(snapshots)})"):
        for snap in snapshots:
            st.markdown(
                f"- {names.get(snap['property_id'], snap['property_id'])} · {snap['source']} · "
                f"{snap['review_count']} reviews · {short_time(snap['fetched_at'])} UTC"
            )
