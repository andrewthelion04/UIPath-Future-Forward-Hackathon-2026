"""Bucăți de interfață folosite de mai multe pagini (inclusiv de paginile feature-urilor)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

from ui import api_client as api

LEVEL_ICONS = {
    "info": ":material/info:",
    "success": ":material/check_circle:",
    "warning": ":material/warning:",
    "error": ":material/error:",
    "action_required": ":material/pending_actions:",
}
LEVEL_COLORS = {
    "info": "blue",
    "success": "green",
    "warning": "orange",
    "error": "red",
    "action_required": "violet",
}
STATUS_COLORS = {
    "ok": "green", "finished": "green", "approved": "green", "applied": "green", "sent": "green",
    "published": "green", "confirmed": "green", "relocated": "green",
    "remediation_scheduled": "green",
    "pending": "orange", "awaiting_decision": "orange", "awaiting_approval": "orange",
    "running": "orange", "max_steps": "orange", "duplicate": "gray", "dismissed": "gray",
    "ignored_not_guest": "gray", "ignored_not_message": "gray", "cancelled": "gray",
    "rejected": "red", "error": "red", "timeout": "red", "failed": "red", "unmapped": "red",
    "no_comparable": "red",
}  # fmt: skip


def load(path: str, **params: Any) -> Any:
    """GET; dacă API-ul nu răspunde, arată eroarea și oprește pagina."""
    try:
        return api.get(path, **params)
    except api.ApiError as exc:
        st.error(str(exc))
        st.stop()


def act(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Rulează o acțiune (POST/PUT) cu spinner; la eroare o afișează și întoarce None."""
    try:
        with st.spinner("Working..."):
            return call(*args, **kwargs)
    except api.ApiError as exc:
        st.error(str(exc))
        return None


def flash(message: str, level: str = "success") -> None:
    """Mesaj (`success` sau `warning`) care supraviețuiește unui `st.rerun()`."""
    st.session_state["_flash"] = (level, message)


def flash_outcome(result: dict, done: str, still_running: str) -> None:
    """Confirmare pentru un request cu `?wait=true`.

    `completed=False` înseamnă că API-ul a încetat să aștepte, nu că fluxul s-a terminat:
    munca merge mai departe în fundal, deci nu anunțăm „gata”.
    """
    if result.get("completed") is False:
        flash(still_running, "warning")
    else:
        flash(done)


def show_flash() -> None:
    saved = st.session_state.pop("_flash", None)
    if saved:
        level, message = saved if isinstance(saved, tuple) else ("success", saved)
        (st.warning if level == "warning" else st.success)(message)


def status_badge(status: str | None) -> None:
    if status:
        st.badge(status.replace("_", " "), color=STATUS_COLORS.get(status, "blue"))


def property_names(properties: list[dict]) -> dict[str, str]:
    return {prop["id"]: prop["name"] for prop in properties}


def booking_label(booking: dict, names: dict[str, str]) -> str:
    return (
        f"{booking['guest_name']} · {names.get(booking['property_id'], '?')} · "
        f"{booking['check_in']} → {booking['check_out']} · {booking['source_channel']}"
    )


def short_time(iso: str | None) -> str:
    """`2026-06-01T10:15:30.123+00:00` -> `06-01 10:15:30` (UTC)."""
    return iso[5:19].replace("T", " ") if iso else ""


def multiline(text: str) -> str:
    """Păstrează rândurile unui text simplu când e afișat ca Markdown."""
    return (text or "").replace("\n", "  \n")
