"""F2 la granițele cu restul aplicației (auditul de integrare, I01–I11).

Ce se verifică: relocarea nu se confirmă dacă ținta nu mai e potrivită, liberă sau blocabilă;
două relocări nu ajung în aceeași proprietate; o ofertă nelivrată nu pornește răspunsul
oaspetelui; un raport nou după reparație retrage cererea de închidere; termenele nu se pierd.
"""

from __future__ import annotations

import asyncio
from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.clock import utcnow
from app.core.contracts.events import IssueReported
from app.features.f2_incidents import service as service_module
from app.features.f2_incidents.events import IncidentTimeout
from app.features.f2_incidents.mock_llm import same_problem
from app.features.f2_incidents.rules import covers_free_nights
from app.ports.pms import PmsError
from conftest import app_client, approve, booking_by_guest, drain, property_by_name

AC = "The air conditioning is not working"
LEAK = "Water is leaking under the kitchen sink."


@pytest.fixture(autouse=True)
def fixed_local_time(monkeypatch):
    monkeypatch.setattr(service_module, "local_time_of_day", lambda: time(10, 0))


@pytest.fixture
def waiting(settings, monkeypatch):
    """Oaspetele nu răspunde singur: testul dă răspunsul prin endpoint."""
    monkeypatch.setenv("F2_SIM_GUEST_ANSWER", "off")
    with app_client(settings) as client:
        yield client


def service(client):
    subs = client.app.state.container.bus.subscriptions()
    return next(s.handler.__self__ for s in subs if s.name == "f2_incidents.on_issue_reported")


def report(client, property_name: str, description: str) -> dict:
    prop = property_by_name(client, property_name)
    response = client.post(
        "/incidents/report", params={"wait": True},
        json={"property_id": prop["id"], "description": description, "staff_id": "staff_maria"},
    )  # fmt: skip
    assert response.status_code == 200 and response.json()["completed"] is True
    return next(
        i for i in client.get("/incidents").json()
        if i["property_id"] == prop["id"] and i["reports"][0]["description"] == description
    )  # fmt: skip


def incident(client, incident_id: str) -> dict:
    return client.get(f"/incidents/{incident_id}").json()


def outbound(client, booking: dict) -> list[dict]:
    history = client.get(f"/bookings/{booking['id']}/messages").json()
    return [m for m in history if m["direction"] == "outbound"]


def titles(client) -> list[str]:
    return [n["title"] for n in client.get("/notifications").json()]


def offered(client, description: str = AC) -> tuple[dict, dict]:
    """Incident la Calea Victoriei cu oferta de relocare trimisă lui Lukas."""
    lukas = booking_by_guest(client, "Lukas Mueller")
    opened = report(client, "Calea Victoriei Apartment", description)
    approve(client, opened["approval_id"], "relocate")
    opened = incident(client, opened["id"])
    assert opened["status"] == "awaiting_guest"
    return opened, lukas


def accept(client, incident_id: str) -> None:
    response = client.post(
        f"/incidents/{incident_id}/guest-answer", params={"wait": True}, json={"choice": "accept"}
    )
    assert response.status_code == 200


# --- I05: rezervarea trebuie să fie a proprietății raportate -----------------------------------


def test_report_rejects_a_booking_of_another_property(waiting):
    studio = property_by_name(waiting, "Old Town Studio")
    lukas = booking_by_guest(waiting, "Lukas Mueller")  # cazat la Calea Victoriei

    response = waiting.post(
        "/incidents/report",
        json={"property_id": studio["id"], "booking_id": lukas["id"], "description": AC},
    )
    assert response.status_code == 422
    assert waiting.get("/incidents").json() == []


def test_an_event_naming_a_foreign_booking_never_touches_that_guest(waiting):
    container = waiting.app.state.container
    studio = property_by_name(waiting, "Old Town Studio")
    lukas = booking_by_guest(waiting, "Lukas Mueller")

    event = IssueReported(
        property_id=studio["id"], booking_id=lukas["id"], description=AC, reported_via="staff"
    )
    waiting.portal.call(container.bus.publish, event)
    assert drain(waiting)

    (opened,) = waiting.get("/incidents").json()
    assert opened["property_id"] == studio["id"] and opened["booking_id"] != lukas["id"]


# --- I08 / I11: ce înseamnă o țintă bună ------------------------------------------------------


def test_only_a_complete_calendar_of_that_property_proves_free_nights():
    start, end = date(2026, 6, 1), date(2026, 6, 4)

    def night(offset: int, property_id: str = "p", available: bool = True):
        return SimpleNamespace(property_id=property_id, day=start + timedelta(days=offset), available=available)

    assert covers_free_nights([night(0), night(1), night(2)], "p", start, end)
    assert covers_free_nights([night(2), night(0), night(1)], "p", start, end)  # ordinea nu contează
    assert not covers_free_nights([night(0), night(0), night(0)], "p", start, end)  # dubluri
    assert not covers_free_nights([night(0), night(1)], "p", start, end)  # incomplet
    assert not covers_free_nights([night(0), night(1), night(2, "other")], "p", start, end)
    assert not covers_free_nights([night(0), night(1), night(2, available=False)], "p", start, end)
    assert not covers_free_nights([], "p", start, start)


def test_a_property_with_an_open_incident_is_not_offered_for_relocation(waiting):
    report(waiting, "Herastrau Family Flat", "There is mold in the bathroom")

    opened = report(waiting, "Calea Victoriei Apartment", AC)

    # Herăstrău ar fi prima alegere; cu un incident deschis acolo, se oferă următoarea.
    herastrau = property_by_name(waiting, "Herastrau Family Flat")
    second_choice = property_by_name(waiting, "Aviatiei Family Apartment")
    assert opened["relocation_property_id"] == second_choice["id"] != herastrau["id"]
    (approval,) = [a for a in waiting.get("/approvals").json() if a["id"] == opened["approval_id"]]
    assert "Herastrau" not in " ".join(o["label"] + o["description"] for o in approval["options"])


def test_a_property_that_cannot_be_blocked_is_not_offered_for_relocation(waiting):
    container = waiting.app.state.container
    capabilities = container.pms.get("mock").capabilities
    with patch.object(container.pms.get("mock"), "capabilities",
                      capabilities.model_copy(update={"write_availability": False, "move_reservations": False})):  # fmt: skip
        opened = report(waiting, "Calea Victoriei Apartment", AC)

    assert opened["relocation_property_id"] is None


# --- I02 / I03 / I04: relocarea nu se confirmă pe jumătate ---------------------------------------


def test_accepting_does_not_relocate_into_a_target_that_no_longer_fits(waiting):
    opened, lukas = offered(waiting)
    container = waiting.app.state.container
    target = container.world.get_property(opened["relocation_property_id"])
    container.world.upsert_property(target.model_copy(update={"max_guests": 1}))

    accept(waiting, opened["id"])

    now = incident(waiting, opened["id"])
    assert (now["status"], now["proposal_round"]) == ("awaiting_decision", 2)
    assert len(outbound(waiting, lukas)) == 1  # doar oferta, nicio confirmare
    assert any("is no longer free" in title for title in titles(waiting))


def test_a_failed_calendar_block_does_not_confirm_the_relocation(waiting):
    opened, lukas = offered(waiting)
    container = waiting.app.state.container

    async def fail(*_args):
        raise PmsError("calendar write failed")

    with patch.object(container.calendar, "set_availability", fail):
        accept(waiting, opened["id"])

    now = incident(waiting, opened["id"])
    assert (now["status"], now["proposal_round"]) == ("awaiting_decision", 2)
    assert len(outbound(waiting, lukas)) == 1
    assert any("could not be blocked" in title for title in titles(waiting))
    assert not any("is being relocated" in title for title in titles(waiting))


def test_an_undelivered_offer_does_not_wait_for_a_guest_answer(client):
    """Cu oaspetele simulat activ: oferta nelivrată nu are voie să fie „acceptată”."""
    lukas = booking_by_guest(client, "Lukas Mueller")
    opened = report(client, "Calea Victoriei Apartment", AC)
    connection = client.app.state.container.pms.get("mock")

    async def fail(*_args):
        raise PmsError("transport down")

    with patch.object(connection, "send_message", fail):
        approve(client, opened["approval_id"], "relocate")

    now = incident(client, opened["id"])
    assert (now["status"], now["proposal_round"]) == ("awaiting_decision", 2)
    assert now["guest_answer"] is None
    assert [m["status"] for m in outbound(client, lukas)] == ["failed"]
    assert not any(title.startswith("Relocation offered") for title in titles(client))
    assert any("could not be delivered" in title for title in titles(client))
    # Propunerea nouă e din nou în inbox, cu aceleași opțiuni.
    (again,) = [a for a in client.get("/approvals").json() if a["id"] == now["approval_id"]]
    assert again["id"] != opened["approval_id"] and {o["key"] for o in again["options"]} == {"relocate", "remediate"}


# --- I01: două relocări nu ajung în aceeași proprietate -----------------------------------------


def test_two_guests_cannot_be_relocated_into_the_same_property(waiting):
    container = waiting.app.state.container
    emma = booking_by_guest(waiting, "Emma Johnson")
    stay = container.world.get_booking(emma["id"])
    container.world.upsert_booking(
        stay.model_copy(update={"check_in": date(2026, 5, 31), "check_out": date(2026, 6, 4)})
    )
    first = report(waiting, "Calea Victoriei Apartment", AC)
    second = report(waiting, "Old Town Studio", "The heating is not working")
    assert first["relocation_property_id"] == second["relocation_property_id"] is not None
    approve(waiting, first["approval_id"], "relocate")
    approve(waiting, second["approval_id"], "relocate")

    f2 = service(waiting)

    async def both_accept() -> bool:
        await asyncio.gather(
            f2.answer_relocation(first["id"], "accept"), f2.answer_relocation(second["id"], "accept")
        )
        return await container.bus.join(timeout=10)

    assert waiting.portal.call(both_accept)
    states = sorted(incident(waiting, i["id"])["status"] for i in (first, second))
    assert states == ["awaiting_decision", "relocated"]


# --- I06 / I07: închiderea ---------------------------------------------------------------------


def closure_of(client) -> dict:
    (closure,) = [a for a in client.get("/approvals").json() if a["kind"] == "incident_closure"]
    return closure


def test_the_closure_approval_shows_the_exact_guest_message_in_its_summary(client):
    lukas = booking_by_guest(client, "Lukas Mueller")
    opened = report(client, "Calea Victoriei Apartment", LEAK)
    approve(client, opened["approval_id"], "remediate")

    closure = closure_of(client)
    message = incident(client, opened["id"])["closure_message"]
    assert message and f'On approval the guest will receive: "{message}"' in closure["summary"]

    approve(client, closure["id"])
    assert outbound(client, lukas)[-1]["body"] == message


def test_a_new_report_after_the_repair_withdraws_the_closure_request(client):
    lukas = booking_by_guest(client, "Lukas Mueller")
    opened = report(client, "Calea Victoriei Apartment", LEAK)
    approve(client, opened["approval_id"], "remediate")
    stale = closure_of(client)
    sent_before = len(outbound(client, lukas))

    again = client.post("/incidents/report", params={"wait": True}, json={
        "property_id": opened["property_id"],
        "description": "Water is still leaking under the kitchen sink after the repair.",
    })  # fmt: skip
    assert again.status_code == 200

    # Aprobarea veche nu mai poate închide incidentul și nu mai e în inbox.
    old = client.post(f"/approvals/{stale['id']}/decision", json={"decision": "approve"})
    assert old.status_code == 409 and "withdrawn" in old.json()["detail"]
    now = incident(client, opened["id"])
    assert now["status"] == "remediation_scheduled" and now["report_count"] == 2
    assert len(outbound(client, lukas)) == sent_before  # niciun „s-a rezolvat” prematur
    assert any("is not ready to close" in title for title in titles(client))
    follow_up = now["tasks"][-1]
    assert follow_up["description"].startswith("Follow-up: reported again after the repair")
    # Personalul simulat a terminat și sarcina nouă: cererea de închidere e una nouă.
    fresh = closure_of(client)
    assert fresh["id"] != stale["id"] and now["closure_round"] == 2

    approve(client, fresh["id"])
    assert incident(client, opened["id"])["status"] == "resolved"


# --- I09: termenul nu se pierde dacă evenimentul nu a putut fi publicat -------------------------


def test_a_deadline_survives_a_failed_timeout_publication(settings, monkeypatch):
    monkeypatch.setenv("F2_SIM_ENABLED", "false")
    with app_client(settings) as client:
        opened = report(client, "Calea Victoriei Apartment", AC)
        f2, bus = service(client), client.app.state.container.bus
        f2.repo.update(opened["id"], staff_deadline=utcnow() - timedelta(minutes=1))
        original = bus.publish

        async def failing(event):
            if isinstance(event, IncidentTimeout):
                raise RuntimeError("event store unavailable")
            return await original(event)

        with patch.object(bus, "publish", failing), pytest.raises(RuntimeError):
            client.portal.call(f2.check_deadlines)
        assert f2.repo.get(opened["id"]).staff_deadline is not None

        assert client.portal.call(f2.check_deadlines) == 1 and drain(client)
        now = incident(client, opened["id"])
        assert (now["status"], now["eta_source"]) == ("awaiting_decision", "timeout")


# --- I10: aceeași defecțiune în două încăperi înseamnă două incidente ---------------------------


def test_the_same_kind_of_problem_in_two_rooms_stays_separate(waiting):
    assert not same_problem(LEAK, "The bathroom shower is leaking badly.")
    assert same_problem(LEAK, "There is a leak below the sink in the kitchen")
    assert same_problem("AC broken", "no cooling, very hot")  # fără loc precizat: aceeași problemă

    report(waiting, "Calea Victoriei Apartment", LEAK)
    waiting.post("/incidents/report", params={"wait": True}, json={
        "property_id": property_by_name(waiting, "Calea Victoriei Apartment")["id"],
        "description": "The bathroom shower is leaking badly.",
    })  # fmt: skip

    assert sorted(i["report_count"] for i in waiting.get("/incidents").json()) == [1, 1]
