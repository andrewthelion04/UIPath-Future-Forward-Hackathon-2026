"""Notificările hostului."""

from __future__ import annotations

import streamlit as st

from ui import api_client as api
from ui.components import LEVEL_COLORS, LEVEL_ICONS, act, load, multiline, short_time

st.title("Notifications")
st.caption("One notification channel for all features: results, fallbacks and failures.")

unread_only = st.toggle("Unread only")
notifications = load("/notifications", unread_only=unread_only)
if any(not n["read"] for n in notifications) and st.button("Mark all as read"):
    if act(api.post, "/notifications/read-all") is not None:
        st.rerun()
if not notifications:
    st.info("No notifications.")

for notification in notifications:
    level = notification["level"]
    with st.container(border=True):
        title_column, action_column = st.columns([6, 1])
        with title_column:
            st.badge(level.replace("_", " ").capitalize(), icon=LEVEL_ICONS.get(level), color=LEVEL_COLORS.get(level, "blue"))
            st.markdown(f"**{notification['title']}**")
        if not notification["read"] and action_column.button(
            "Mark read", key=f"read_{notification['id']}", width="stretch"
        ):
            if act(api.post, f"/notifications/{notification['id']}/read") is not None:
                st.rerun()
        if notification["body"]:
            st.markdown(multiline(notification["body"]))
        st.caption(f"{notification['feature']} · {short_time(notification['created_at'])} UTC")
