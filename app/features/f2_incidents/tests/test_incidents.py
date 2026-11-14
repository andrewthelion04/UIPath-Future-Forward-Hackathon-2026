"""F2 cap-coadă pe mock: sesizare -> mentenanța -> propunere -> decizie -> oaspete -> sarcină -> închidere.

Personalul și oaspetele sunt simulați automat (setările implicite `F2_SIM_*`), deci fiecare
request cu `?wait=true` duce fluxul până la următoarea decizie a hostului.
"""

from __future__ import annotations

import re
from datetime import time, timedelta

import pytest

from app.features.f2_incidents import service as service_module
from conftest import approve, booking_by_guest, events_of, property_by_name, reject


@pytest.fixture(autouse=True)
def fixed_local_time(monkeypatch):
    """Ora locală fixă (data e deja fixă: 2026-06-01), ca timpul până la check-in să fie determinist."""
    monkeypatch.setattr(service_module, "local_time_of_day", lambda: time(10, 0))


def report(client, property_name: str, description: str, staff_id: str = "staff_maria", wait=True) -> dict:
    prop = property_by_name(client, property_name)
    result = client.post(
        "/sim/staff-report",
        params={"wait": wait},
        json={"property_id": prop["id"], "description": description, "staff_id": staff_id},
    ).json()
    assert result["status"] == "published"
    if wait:
        assert result["completed"] is True
    return prop


def only_incident(client) -> dict:
    (incident,) = client.get("/incidents").json()
    return incident


def pending(client, kind: str | None = None) -> list[dict]:
    return [a for a in client.get("/approvals").json() if kind is None or a["kind"] == kind]


def messages(client, booking: dict) -> list[str]:
    return [m["body"] for m in client.get(f"/bookings/{booking['id']}/messages").json()]


def quoted(description: str, prefix: str) -> str:
    """Textul exact dintre ghilimele după `prefix` într-o descriere de opțiune."""
    match = re.search(re.escape(prefix) + r' "(.*?)"(?:\n|$)', description, re.DOTALL)
    assert match, f"{prefix!r} not in {description!r}"
    return match.group(1)


# --- Propunerea -------------------------------------------------------------------------


def test_report_asks_maintenance_and_proposes_with_the_estimate(client):
    apartment = report(client, "Calea Victoriei Apartment", "The air conditioning is not working")
    lukas = booking_by_guest(client, "Lukas Mueller")  # cazat acum acolo

    incident = only_incident(client)
    assert incident["status"] == "awaiting_decision"
    assert (incident["property_id"], incident["booking_id"]) == (apartment["id"], lukas["id"])
    assert (incident["reported_via"], incident["reporter"]) == ("staff", "Maria Ionescu")
    # Agentul a întrebat mentenanța, iar personalul simulat a răspuns.
    chat = incident["staff_messages"]
    assert [m["direction"] for m in chat] == ["to_staff", "from_staff"]
    assert chat[0]["staff_name"] == "Andrei Stan" and chat[1]["simulated"] is True
    assert (incident["eta_hours"], incident["eta_source"]) == (30, "staff")
    assert incident["in_house"] is True and incident["recommendation"] == "relocate"

    (approval,) = pending(client)
    assert (approval["feature"], approval["kind"]) == ("f2_incidents", "incident_resolution")
    assert incident["approval_id"] == approval["id"]
    # Recomandarea e prima; ambele opțiuni sunt oferite.
    assert [o["key"] for o in approval["options"]] == ["relocate", "remediate"]
    assert "Herastrau Family Flat" in approval["options"][0]["label"]
    summary = approval["summary"]
    assert "Maintenance (Andrei Stan) replied" in summary and "30 h" in summary
    assert "the guest is in the property now" in summary
    assert "Recommendation: Offer relocation" in summary
    relocate = approval["options"][0]["description"]
    for prefix in ("Guest will receive:", "If the guest accepts:", "If the guest stays:"):
        assert quoted(relocate, prefix).startswith(("Hi Lukas", "Great, Lukas", "Of course, Lukas"))
    assert messages(client, lukas) == []  # nimic către oaspete fără aprobare


def test_a_quick_fix_is_recommended_in_place(client):
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")

    incident = only_incident(client)
    assert incident["eta_hours"] == 2 and incident["recommendation"] == "remediate"
    (approval,) = pending(client)
    assert [o["key"] for o in approval["options"]] == ["remediate", "relocate"]
    assert "can be done in time" in approval["summary"]


@pytest.mark.parametrize(
    ("local_time", "recommended"),
    [(time(10, 0), "remediate"), (time(20, 0), "relocate")],
)
def test_recommendation_depends_on_the_time_left_before_check_in(client, monkeypatch, local_time, recommended):
    """Emma sosește pe 3 iunie la 15:00; mucegaiul cere ~48 h plus 2 h de marjă."""
    monkeypatch.setattr(service_module, "local_time_of_day", lambda: local_time)
    report(client, "Old Town Studio", "There is mold in the bathroom")

    incident = only_incident(client)
    assert incident["in_house"] is False and incident["eta_hours"] == 48
    assert incident["recommendation"] == recommended
    (approval,) = pending(client)
    assert approval["options"][0]["key"] == recommended


def test_no_guest_message_when_the_fix_is_done_before_arrival(client):
    emma = booking_by_guest(client, "Emma Johnson")
    report(client, "Old Town Studio", "The air conditioning is not working")  # ~30 h, sosire în 53 h

    (approval,) = pending(client)
    remediate = next(o for o in approval["options"] if o["key"] == "remediate")
    assert "No message to the guest" in remediate["description"]
    approve(client, approval["id"], "remediate")
    assert messages(client, emma) == []


def test_relocation_is_not_offered_without_a_compatible_free_property(client):
    # Toate celelalte proprietăți sunt ocupate pe durata sejurului lui Lukas.
    container = client.app.state.container
    today = container.clock.today()
    for prop in client.get("/properties").json():
        if prop["name"] != "Calea Victoriei Apartment":
            client.portal.call(
                container.calendar.set_availability, prop["id"], today, today + timedelta(days=3), False
            )

    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")

    (approval,) = pending(client)
    assert [o["key"] for o in approval["options"]] == ["remediate"]
    assert "relocation is not offered" in approval["summary"]
    assert only_incident(client)["relocation_property_id"] is None


# --- Decizia, oaspetele, sarcina, închiderea ----------------------------------------------


def test_relocation_accepted_by_the_guest_then_closed_by_the_host(client):
    report(client, "Calea Victoriei Apartment", "The air conditioning is not working")
    lukas = booking_by_guest(client, "Lukas Mueller")
    target = property_by_name(client, "Herastrau Family Flat")
    (approval,) = pending(client)
    relocate = approval["options"][0]["description"]

    assert approve(client, approval["id"], "relocate")["completed"] is True

    incident = only_incident(client)
    assert incident["status"] == "relocated" and incident["guest_answer"] == "accept"
    moved = client.get(f"/bookings/{lukas['id']}").json()
    assert moved["property_id"] == target["id"]
    assert client.post("/integrations/mock/sync").status_code == 200
    assert client.get(f"/bookings/{lukas['id']}").json()["property_id"] == target["id"]
    # Oaspetele a primit exact textele din aprobare: oferta, apoi confirmarea.
    assert messages(client, lukas) == [
        quoted(relocate, "Guest will receive:"),
        quoted(relocate, "If the guest accepts:"),
    ]
    days = client.get(f"/properties/{target['id']}/calendar", params={"days": 4}).json()
    assert [d["available"] for d in days] == [False, False, False, True]
    # Sarcina de reparație a fost creată și terminată de personalul simulat.
    (task,) = incident["tasks"]
    assert task["status"] == "done" and task["staff_name"] == "Andrei Stan" and task["completion_note"]
    (closure,) = pending(client, "incident_closure")
    assert "accepted the move" in closure["summary"]
    assert closure["options"][0]["description"] == "No message to the guest."
    assert events_of(client, "IncidentResolved") == []  # încă nu e închis

    approve(client, closure["id"])

    incident = only_incident(client)
    assert incident["status"] == "resolved"
    assert len(messages(client, lukas)) == 2  # oaspetele mutat nu primește „s-a rezolvat”
    (resolved,) = events_of(client, "IncidentResolved")
    assert resolved["payload"]["resolution"] == "relocation"
    assert events_of(client, "IncidentDecided")[0]["payload"]["option"] == "relocate"


def test_guest_who_stays_gets_the_apology_with_the_estimate(settings, monkeypatch):
    monkeypatch.setenv("F2_SIM_GUEST_ANSWER", "stay")
    from conftest import app_client

    with app_client(settings) as client:
        report(client, "Calea Victoriei Apartment", "The air conditioning is not working")
        lukas = booking_by_guest(client, "Lukas Mueller")
        (approval,) = pending(client)
        relocate = approval["options"][0]["description"]

        approve(client, approval["id"], "relocate")

        incident = only_incident(client)
        assert incident["status"] == "remediation_scheduled" and incident["guest_answer"] == "stay"
        declined = quoted(relocate, "If the guest stays:")
        assert "30 hours" in declined
        assert messages(client, lukas) == [quoted(relocate, "Guest will receive:"), declined]
        # Oaspetele a rămas: la închidere primește și mesajul „s-a rezolvat”, aprobat de host.
        (closure,) = pending(client, "incident_closure")
        fixed = quoted(closure["options"][0]["description"], "Guest will receive:")
        approve(client, closure["id"])
        assert messages(client, lukas)[-1] == fixed
        assert only_incident(client)["status"] == "resolved"
        assert events_of(client, "IncidentResolved")[0]["payload"]["resolution"] == "remediation"


def test_remediation_sends_the_approved_apology_and_closes_with_a_fixed_message(client):
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")
    lukas = booking_by_guest(client, "Lukas Mueller")
    (approval,) = pending(client)
    remediate = next(o for o in approval["options"] if o["key"] == "remediate")

    approve(client, approval["id"], "remediate")

    assert messages(client, lukas) == [quoted(remediate["description"], "Guest will receive:")]
    incident = only_incident(client)
    assert incident["status"] == "remediation_scheduled"
    assert "Andrei Stan (maintenance)" in incident["resolution"]
    target = property_by_name(client, "Herastrau Family Flat")
    days = client.get(f"/properties/{target['id']}/calendar", params={"days": 3}).json()
    assert all(d["available"] for d in days)  # nicio blocare fără relocare

    (closure,) = pending(client, "incident_closure")
    approve(client, closure["id"])
    assert len(messages(client, lukas)) == 2 and "fixed" in messages(client, lukas)[-1]
    assert only_incident(client)["status"] == "resolved"


def test_rejected_proposal_is_dismissed_without_any_action(client):
    report(client, "Calea Victoriei Apartment", "The air conditioning is not working")
    lukas = booking_by_guest(client, "Lukas Mueller")
    (approval,) = pending(client)

    reject(client, approval["id"])

    incident = only_incident(client)
    assert incident["status"] == "dismissed" and incident["tasks"] == []
    assert messages(client, lukas) == []
    assert events_of(client, "IncidentResolved")[0]["payload"]["resolution"] == "dismissed"


def test_rejected_closure_sends_the_staff_back(client):
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")
    (approval,) = pending(client)
    approve(client, approval["id"], "remediate")
    (closure,) = pending(client, "incident_closure")

    body = {"decision": "reject", "note": "Still dripping under the sink"}
    client.post(f"/approvals/{closure['id']}/decision", params={"wait": True}, json=body)

    incident = only_incident(client)
    assert incident["status"] == "remediation_scheduled"
    assert [t["description"] for t in incident["tasks"]] == [
        "Fix: Water is leaking under the kitchen sink.",
        "Follow-up: Still dripping under the sink",
    ]
    # Personalul simulat a terminat și urmarea: a doua cerere de închidere.
    (second,) = pending(client, "incident_closure")
    assert second["id"] != closure["id"] and second["payload"]["round"] == 2


def test_the_decision_is_applied_once_even_if_the_host_decides_twice(client):
    report(client, "Calea Victoriei Apartment", "The air conditioning is not working")
    lukas = booking_by_guest(client, "Lukas Mueller")
    (approval,) = pending(client)

    first = approve(client, approval["id"], "relocate")
    second = approve(client, approval["id"], "relocate")

    assert (first["applied"], second["applied"]) == (True, False)
    assert len(messages(client, lukas)) == 2
    assert len(events_of(client, "IncidentDecided")) == 1
    assert len(only_incident(client)["tasks"]) == 1


def test_the_whole_incident_shares_one_correlation(client):
    report(client, "Calea Victoriei Apartment", "The air conditioning is not working")
    (approval,) = pending(client)
    approve(client, approval["id"], "relocate")
    (closure,) = pending(client, "incident_closure")
    approve(client, closure["id"])

    correlation = only_incident(client)["correlation_id"]
    assert correlation == approval["correlation_id"] == closure["correlation_id"]
    types = {e["type"] for e in client.get("/events", params={"correlation_id": correlation}).json()}
    assert {"IssueReported", "StaffReplied", "EstimateReady", "IncidentDecided",
            "GuestAnsweredRelocation", "IncidentTaskCompleted", "IncidentResolved"} <= types  # fmt: skip


# --- Deduplicare ------------------------------------------------------------------------


def test_two_identical_reports_make_one_incident_and_one_approval(client):
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.", "staff_andrei")

    incident = only_incident(client)
    assert incident["report_count"] == 2 and len(incident["reports"]) == 2
    assert len(pending(client)) == 1
    assert any("Another report" in n["title"] for n in client.get("/notifications").json())


def test_simultaneous_reports_do_not_open_two_incidents(client):
    from conftest import drain

    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.", wait=False)
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.", wait=False)
    assert drain(client)

    assert only_incident(client)["report_count"] == 2
    assert len(pending(client)) == 1


def test_the_same_problem_in_other_words_is_attached(client):
    report(client, "Calea Victoriei Apartment", "AC broken")
    report(client, "Calea Victoriei Apartment", "No cooling in the bedroom, it is very hot", "staff_andrei")

    incident = only_incident(client)
    assert incident["report_count"] == 2
    runs = [r for r in client.get("/agent/runs").json() if r["agent"] == "f2_incidents.triage"]
    assert len(runs) == 1 and runs[0]["status"] == "finished"


def test_different_problems_open_separate_incidents(client):
    report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")
    report(client, "Calea Victoriei Apartment", "The bedroom lamp is broken")

    assert len(client.get("/incidents").json()) == 2


def test_a_more_severe_report_raises_the_severity(client):
    apartment = report(client, "Calea Victoriei Apartment", "Water is leaking under the kitchen sink.")
    client.post(
        "/sim/staff-report",
        params={"wait": True},
        json={"property_id": apartment["id"], "description": "The kitchen sink is leaking badly",
              "severity": "high"},
    )  # fmt: skip
    assert only_incident(client)["severity"] == "high"


# --- Personalul ---------------------------------------------------------------------------


def test_a_vague_staff_reply_gets_one_clarification(client):
    report(client, "Calea Victoriei Apartment", "Something is wrong with the oven")

    incident = only_incident(client)
    assert [m["direction"] for m in incident["staff_messages"]] == [
        "to_staff", "from_staff", "to_staff", "from_staff",
    ]  # fmt: skip
    assert incident["staff_questions"] == 2 and incident["eta_hours"] == 4
    assert len(pending(client)) == 1


def test_no_staff_assigned_still_reaches_the_host(client):
    client.app.state.container.db.execute("DELETE FROM staff")
    report(client, "Calea Victoriei Apartment", "The air conditioning is not working", staff_id=None)

    incident = only_incident(client)
    assert incident["eta_source"] == "no_staff" and incident["staff_messages"] == []
    (approval,) = pending(client)
    assert "No maintenance person is assigned" in approval["summary"]
    titles = [n["title"] for n in client.get("/notifications").json()]
    assert any("No staff assigned" in t for t in titles)

    approve(client, approval["id"], "remediate")
    (task,) = only_incident(client)["tasks"]
    assert task["staff_name"] is None and task["status"] == "open"
    assert any("nobody assigned" in n["title"] for n in client.get("/notifications").json())
