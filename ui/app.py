"""StayPilot AI — simulator și depanare (Streamlit).

Pornire:  python manage.py ui      (API-ul trebuie să ruleze: python manage.py api)

Paginile feature-urilor se descoperă automat din `app/features/*/ui_page.py`, deci un
feature își adaugă pagina fără să editeze acest fișier. Totul trece prin API-ul REST.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from ui import api_client as api  # noqa: E402
from ui.live_updates import live_status  # noqa: E402

VIEWS = ROOT / "ui" / "views"
FEATURES = ROOT / "app" / "features"

st.set_page_config(page_title="StayPilot AI", page_icon=":material/travel_explore:", layout="wide")


def count(path: str, **params: object) -> int | None:
    try:
        return len(api.get(path, **params))
    except api.ApiError:
        return None


def with_count(title: str, number: int | None) -> str:
    return f"{title} ({number})" if number else title


def feature_pages() -> list:
    pages = []
    for path in sorted(FEATURES.glob("*/ui_page.py")):
        folder = path.parent.name  # ex. f2_incidents
        code, _, name = folder.partition("_")
        pages.append(
            st.Page(
                str(path),
                title=f"{code.upper()} · {name.replace('_', ' ').title()}",
                url_path=folder,
                icon=":material/extension:",
            )
        )
    return pages


pending = count("/approvals")
answer_count = count("/answers")
pending = (pending or 0) + (answer_count or 0)
unread = count("/notifications", unread_only=True)

navigation = st.navigation(
    {
        "Simulate": [
            st.Page(str(VIEWS / "simulator.py"), title="Simulator", icon=":material/play_circle:", default=True),
        ],
        "Host": [
            st.Page(str(VIEWS / "approvals.py"), title=with_count("Approvals / Answers", pending), url_path="approvals", icon=":material/fact_check:"),
            st.Page(str(VIEWS / "notifications.py"), title=with_count("Notifications", unread), url_path="notifications", icon=":material/notifications:"),
        ],
        "Inspect": [
            st.Page(str(VIEWS / "agent_dashboard.py"), title="Agent dashboard", url_path="agent-dashboard", icon=":material/monitoring:"),
            st.Page(str(VIEWS / "agent_timeline.py"), title="Agent timeline", icon=":material/timeline:"),
            st.Page(str(VIEWS / "events.py"), title="Events", icon=":material/bolt:"),
            st.Page(str(VIEWS / "world.py"), title="World data", icon=":material/database:"),
        ],
        "Features": feature_pages(),
    }
)  # fmt: skip

with st.sidebar:
    try:
        health = api.get("/health")
    except api.ApiError as exc:
        st.error(str(exc))
    else:
        providers = health["providers"]
        st.caption(
            f"API online · today {health['today']}  \n"
            f"PMS `{providers['pms']}` · LLM `{providers['llm']}`  \n"
            f"Reviews `{providers['reviews']}` · Market `{providers['market']}`"
        )
        for feature in health["features"]:
            if not feature["loaded"]:
                st.warning(f"{feature['name']} is not loaded: {feature['error']}")
    st.button("Refresh", icon=":material/refresh:", width="stretch")
    live_status()

navigation.run()
