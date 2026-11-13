"""Paginile Streamlit rulează fără erori pe un API real (în proces), fără browser.

`ui.api_client.request` e redirecționat către aplicația FastAPI de test, deci paginile
parcurg exact drumul REST din producție.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.config import ROOT_DIR
from conftest import approve, booking_by_guest, property_by_name
from ui import api_client

VIEWS = sorted((ROOT_DIR / "ui" / "views").glob("*.py"))
FEATURE_PAGES = sorted((ROOT_DIR / "app" / "features").glob("*/ui_page.py"))
PAGES = [p for p in [*VIEWS, *FEATURE_PAGES] if p.name != "__init__.py"]


@pytest.fixture
def api(client, monkeypatch):
    """Leagă clientul HTTP al UI-ului de aplicația de test."""

    def request(method, path, *, params=None, json=None):
        query = {key: value for key, value in (params or {}).items() if value is not None}
        response = client.request(method, path, params=query, json=json)
        if response.status_code >= 400:
            raise api_client.ApiError(f"{response.status_code}: {response.json().get('detail')}")
        return response.json()

    monkeypatch.setattr(api_client, "request", request)
    return client


def run_page(path: Path) -> AppTest:
    page = AppTest.from_file(str(path), default_timeout=60).run()
    assert not page.exception, [e.value for e in page.exception]
    return page


def populate(client) -> None:
    """Rulează toate fluxurile, ca fiecare pagină să aibă ce afișa."""
    emma, lukas = booking_by_guest(client, "Emma Johnson"), booking_by_guest(client, "Lukas Mueller")
    wait = {"wait": True}
    client.post("/sim/guest-message", params=wait, json={"booking_id": emma["id"], "text": "Could we check in early?"})
    client.post("/sim/guest-message", params=wait, json={"booking_id": lukas["id"], "text": "The heating is broken."})
    client.post("/sim/review-analysis", params=wait, json={"property_id": None})
    client.post("/sim/cancel-booking", params=wait, json={"booking_id": emma["id"]})
    client.post("/webhooks/mock", json={"event": "reservation.updated"})
    relist = next(a for a in client.get("/approvals").json() if a["feature"] == "f5_relisting")
    approve(client, relist["id"])


@pytest.mark.parametrize("path", PAGES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_page_renders_on_an_empty_and_on_a_busy_world(api, path):
    run_page(path)
    populate(api)
    run_page(path)


def test_main_app_discovers_the_feature_pages(api):
    app = run_page(ROOT_DIR / "ui" / "app.py")
    assert app.title[0].value == "Simulator"  # pagina implicită
    assert "API online" in app.sidebar.caption[0].value
    assert len(FEATURE_PAGES) == 5


def test_simulator_drives_a_flow_and_the_inbox_decides_it(api):
    simulator = run_page(ROOT_DIR / "ui" / "views" / "simulator.py")
    guests = simulator.selectbox(key="chat_booking")
    assert not any("Daniel Kim" in option for option in guests.options)  # sejur încheiat
    guests.select_index(next(i for i, option in enumerate(guests.options) if "Emma Johnson" in option))
    simulator.run()
    simulator.button(key="sample_Request early check-in").click().run()
    assert not simulator.exception

    emma = booking_by_guest(api, "Emma Johnson")
    senders = [m["sender"] for m in api.get(f"/bookings/{emma['id']}/messages").json()]
    assert senders == ["guest", "copilot"]
    (approval,) = api.get("/approvals").json()

    inbox = run_page(ROOT_DIR / "ui" / "views" / "approvals.py")
    assert inbox.subheader[0].value == approval["title"]
    inbox.button(key=f"approve_{approval['id']}").click().run()
    assert not inbox.exception

    assert api.get("/approvals").json() == []
    last = api.get(f"/bookings/{emma['id']}/messages").json()[-1]
    assert "confirmed an early check-in" in last["body"]


def test_staff_report_form_and_two_option_approval(api):
    simulator = run_page(ROOT_DIR / "ui" / "views" / "simulator.py")
    apartment = property_by_name(api, "Calea Victoriei Apartment")
    # Formularul: Maria raportează o problemă la apartamentul ocupat acum.
    properties = simulator.selectbox(key="staff_property")
    properties.select_index(next(i for i, option in enumerate(properties.options) if "Calea Victoriei" in option))
    simulator.button(key="staff_submit").click().run()
    assert not simulator.exception

    (incident,) = api.get("/incidents").json()
    assert incident["property_id"] == apartment["id"]
    (approval,) = api.get("/approvals").json()

    inbox = run_page(ROOT_DIR / "ui" / "views" / "approvals.py")
    inbox.radio(key=f"option_{approval['id']}").set_value("relocate")
    inbox.button(key=f"approve_{approval['id']}").click().run()
    assert not inbox.exception
    assert api.get("/incidents").json()[0]["status"] == "relocated"
