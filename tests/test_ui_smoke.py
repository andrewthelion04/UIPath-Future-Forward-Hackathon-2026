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


def test_host_answers_a_guest_from_the_inbox(api):
    booking = booking_by_guest(api, "Emma Johnson")
    api.post("/sim/guest-message?wait=true", json={"booking_id":booking["id"], "text":"What is the Wi-Fi password?"})
    answer = api.get("/answers").json()[0]
    page = run_page(ROOT_DIR / "ui" / "views" / "approvals.py")
    assert page.title[0].value == "Approvals / Answers"
    page.text_area(key=f"reply_{answer['id']}").input("Wi-Fi password: Demo1234")
    next(b for b in page.button if b.label == "Send reply to guest").click().run()
    assert not page.exception
    assert api.get("/answers").json() == []
    assert api.get(f"/bookings/{booking['id']}/messages").json()[-1]["body"] == "Wi-Fi password: Demo1234"


def test_world_calendar_navigates_across_year_boundary(api):
    page = run_page(ROOT_DIR / "ui" / "views" / "world.py")
    from datetime import date
    page.session_state["calendar_month"] = date(2026,12,1)
    page.button(key="calendar_next").click().run()
    assert not page.exception
    assert page.session_state["calendar_month"] == date(2027,1,1)
    page.button(key="calendar_prev").click().run()
    assert page.session_state["calendar_month"] == date(2026,12,1)


def test_property_table_saves_local_edits(api):
    page = run_page(ROOT_DIR / "ui" / "views" / "world.py")
    prop = api.get("/properties").json()[0]
    page.session_state["property_editor"] = {"edited_rows": {0:{"Name":"Edited in the table"}}, "added_rows":[], "deleted_rows":[]}
    next(b for b in page.button if b.label == "Save property changes").click().run()
    assert not page.exception
    updated = next(p for p in api.get("/properties").json() if p["id"] == prop["id"])
    assert updated["name"] == "Edited in the table"


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


def test_single_option_approval_shows_what_the_host_approves(api):
    """Textul exact pentru oaspete stă în descrierea opțiunii; inboxul îl arată și când e una singură."""
    apartment = property_by_name(api, "Calea Victoriei Apartment")
    body = {"property_id": apartment["id"], "description": "Water is leaking under the kitchen sink."}
    api.post("/sim/staff-report", params={"wait": True}, json=body)
    (proposal,) = api.get("/approvals").json()
    approve(api, proposal["id"], "remediate")
    (closure,) = api.get("/approvals").json()
    (option,) = closure["options"]
    assert option["description"].startswith("Guest will receive:")

    inbox = run_page(ROOT_DIR / "ui" / "views" / "approvals.py")

    assert option["description"] in [str(block.value) for block in inbox.markdown]


def test_host_can_record_the_guests_answer_after_a_direct_conversation(api, monkeypatch):
    """Copilotul a predat discuția; hostul notează în pagina F2 ce a hotărât oaspetele."""
    container = api.app.state.container
    service = next(
        s.handler.__self__ for s in container.bus.subscriptions() if s.name == "f2_incidents.on_issue_reported"
    )
    monkeypatch.setattr(service.settings, "sim_guest_answer", "off")
    apartment = property_by_name(api, "Calea Victoriei Apartment")
    body = {"property_id": apartment["id"], "description": "The air conditioning is not working"}
    api.post("/sim/staff-report", params={"wait": True}, json=body)
    (incident,) = api.get("/incidents").json()
    approve(api, incident["approval_id"], "relocate")
    assert api.get("/incidents").json()[0]["status"] == "awaiting_guest"

    page = run_page(ROOT_DIR / "app" / "features" / "f2_incidents" / "ui_page.py")
    page.button(key=f"stay_{incident['id']}").click().run()

    assert not page.exception
    assert api.get("/incidents").json()[0]["status"] == "remediation_scheduled"
