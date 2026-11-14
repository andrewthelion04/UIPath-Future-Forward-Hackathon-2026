"""F2 fără simulare: intrările manuale (endpointuri), termenele expirate și fallback-urile LLM."""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from app.core.clock import utcnow
from app.features.f2_incidents import service as service_module
from app.ports.llm import ChatRequest, ChatResponse, LLMError
from conftest import app_client, approve, booking_by_guest, drain, property_by_name


@pytest.fixture(autouse=True)
def fixed_local_time(monkeypatch):
    monkeypatch.setattr(service_module, "local_time_of_day", lambda: time(10, 0))


@pytest.fixture
def manual(settings, monkeypatch):
    """Personalul și oaspetele nu sunt simulați: răspund doar prin endpointuri."""
    monkeypatch.setenv("F2_SIM_ENABLED", "false")
    with app_client(settings) as client:
        yield client


def staff_report(client, description: str, property_name: str = "Calea Victoriei Apartment") -> dict:
    prop = property_by_name(client, property_name)
    response = client.post(
        "/incidents/report",
        params={"wait": True},
        json={"property_id": prop["id"], "description": description, "staff_id": "staff_ioana"},
    )
    assert response.status_code == 200 and response.json()["completed"] is True
    (incident,) = client.get("/incidents").json()
    return incident


def advance_clock(monkeypatch, minutes: float) -> None:
    later = utcnow() + timedelta(minutes=minutes)
    monkeypatch.setattr(service_module, "utc_now", lambda: later)


def test_staff_report_endpoint_and_manual_staff_reply(manual):
    incident = staff_report(manual, "The air conditioning is not working")
    assert incident["status"] == "awaiting_staff" and incident["reporter"] == "Ioana Radu"
    assert [m["direction"] for m in incident["staff_messages"]] == ["to_staff"]
    assert manual.get("/approvals").json() == []

    reply = manual.post(
        f"/incidents/{incident['id']}/staff-reply",
        params={"wait": True},
        json={"text": "I can do it in about 5 hours."},
    )
    assert reply.status_code == 200

    (incident,) = manual.get("/incidents").json()
    assert incident["eta_hours"] == 5 and incident["status"] == "awaiting_decision"
    assert len(manual.get("/approvals").json()) == 1


def test_endpoints_validate_their_input(manual):
    assert manual.post("/incidents/report", json={"property_id": "prop_x", "description": "Broken"}).status_code == 404
    assert manual.post("/incidents/inc_x/staff-reply", json={"text": "hi"}).status_code == 404
    assert manual.post("/incidents/inc_x/guest-answer", json={"choice": "accept"}).status_code == 404
    assert manual.post("/incidents/tasks/T-99/complete", json={}).status_code == 404
    incident = staff_report(manual, "The air conditioning is not working")
    # Oaspetele nu poate răspunde înainte să primească o ofertă.
    answer = manual.post(f"/incidents/{incident['id']}/guest-answer", json={"choice": "accept"})
    assert answer.status_code == 409


def test_staff_silence_times_out_into_a_proposal_without_estimate(manual, monkeypatch):
    staff_report(manual, "The air conditioning is not working")

    advance_clock(monkeypatch, 31)
    manual.get("/incidents")  # verificarea termenelor rulează și la citire
    assert drain(manual)

    (incident,) = manual.get("/incidents").json()
    assert incident["eta_source"] == "timeout" and incident["eta_hours"] is None
    (approval,) = manual.get("/approvals").json()
    assert "did not reply in time" in approval["summary"]
    assert approval["correlation_id"] == incident["correlation_id"]
    assert any("did not reply" in n["title"] for n in manual.get("/notifications").json())


def test_guest_answer_endpoint_and_guest_timeout(manual, monkeypatch):
    lukas = booking_by_guest(manual, "Lukas Mueller")
    incident = staff_report(manual, "The air conditioning is not working")
    manual.post(f"/incidents/{incident['id']}/staff-reply", params={"wait": True},
                json={"text": "Needs a part, about 30 hours."})  # fmt: skip
    (approval,) = manual.get("/approvals").json()
    approve(manual, approval["id"], "relocate")
    (incident,) = manual.get("/incidents").json()
    assert incident["status"] == "awaiting_guest"
    assert len(manual.get(f"/bookings/{lukas['id']}/messages").json()) == 1  # doar oferta

    advance_clock(monkeypatch, 61)
    manual.get("/incidents")
    assert drain(manual)

    (incident,) = manual.get("/incidents").json()
    assert incident["status"] == "remediation_scheduled" and incident["guest_answer"] == "timeout"
    assert len(manual.get(f"/bookings/{lukas['id']}/messages").json()) == 2  # + scuzele aprobate
    assert any("did not answer" in n["title"] for n in manual.get("/notifications").json())


def test_manual_task_completion_is_idempotent(manual):
    incident = staff_report(manual, "Water is leaking under the kitchen sink.")
    manual.post(f"/incidents/{incident['id']}/staff-reply", params={"wait": True},
                json={"text": "About 2 hours."})  # fmt: skip
    (approval,) = manual.get("/approvals").json()
    approve(manual, approval["id"], "remediate")
    (incident,) = manual.get("/incidents").json()
    (task,) = incident["tasks"]
    assert task["status"] == "open"

    body = {"note": "New seal installed.", "staff_id": "staff_andrei"}
    first = manual.post(f"/incidents/tasks/{task['id']}/complete", params={"wait": True}, json=body)
    second = manual.post(f"/incidents/tasks/{task['id']}/complete", params={"wait": True}, json=body)

    assert first.json()["status"] == second.json()["status"] == "done"
    assert first.json()["completed_by"] == "Andrei Stan"
    closures = [a for a in manual.get("/approvals").json() if a["kind"] == "incident_closure"]
    assert len(closures) == 1 and "New seal installed." in closures[0]["summary"]


def test_relocation_target_taken_meanwhile_leads_to_a_new_proposal(manual):
    lukas = booking_by_guest(manual, "Lukas Mueller")
    target = property_by_name(manual, "Herastrau Family Flat")
    incident = staff_report(manual, "The air conditioning is not working")
    manual.post(f"/incidents/{incident['id']}/staff-reply", params={"wait": True},
                json={"text": "About 30 hours, I need a part."})  # fmt: skip
    (approval,) = manual.get("/approvals").json()
    approve(manual, approval["id"], "relocate")

    container = manual.app.state.container
    start, end = container.clock.today(), container.clock.today() + timedelta(days=3)
    manual.portal.call(container.calendar.set_availability, target["id"], start, end, False)
    manual.post(f"/incidents/{incident['id']}/guest-answer", params={"wait": True}, json={"choice": "accept"})

    (incident,) = manual.get("/incidents").json()
    assert incident["status"] == "awaiting_decision" and incident["proposal_round"] == 2
    assert len(manual.get(f"/bookings/{lukas['id']}/messages").json()) == 1  # nicio confirmare
    (new_approval,) = manual.get("/approvals").json()
    assert new_approval["id"] != approval["id"]
    assert [o["key"] for o in new_approval["options"]] == ["remediate"]  # nu mai e loc liber
    assert any("no longer free" in n["title"] for n in manual.get("/notifications").json())


# --- LLM-ul căzut ---------------------------------------------------------------------------


class FailingLLM:
    name = "failing"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        raise LLMError("model unavailable")


def test_every_agent_has_a_fallback_when_the_llm_fails(settings):
    with app_client(settings, llm=FailingLLM()) as client:
        lukas = booking_by_guest(client, "Lukas Mueller")
        apartment = property_by_name(client, "Calea Victoriei Apartment")
        body = {"property_id": apartment["id"], "description": "The air conditioning is not working"}
        client.post("/sim/staff-report", params={"wait": True}, json=body)

        (incident,) = client.get("/incidents").json()
        # Întrebarea-șablon a plecat, personalul simulat a răspuns din script, estimarea e necunoscută.
        assert [m["direction"] for m in incident["staff_messages"]] == ["to_staff", "from_staff"]
        assert incident["eta_source"] == "unknown" and incident["drafts_source"] == "template"
        (approval,) = client.get("/approvals").json()
        assert "templates" in approval["summary"]
        assert "as soon as possible" in approval["options"][0]["description"]

        # A doua sesizare: trierea nu poate rula, deci incident nou + avertisment.
        client.post("/sim/staff-report", params={"wait": True}, json=body)
        assert len(client.get("/incidents").json()) == 2
        assert any("Possible duplicate" in n["title"] for n in client.get("/notifications").json())

        approve(client, approval["id"], "remediate")
        closure = next(a for a in client.get("/approvals").json() if a["kind"] == "incident_closure")
        assert "recorded facts" in closure["summary"]
        approve(client, closure["id"])
        assert "fixed" in client.get(f"/bookings/{lukas['id']}/messages").json()[-1]["body"]
