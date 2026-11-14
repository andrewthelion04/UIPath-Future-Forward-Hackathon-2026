"""F2 și conversația cu oaspetele: ce publică despre incident și cum primește răspunsul din chat.

Copilotul (alt feature) citește `guest_topics` și răspunde oaspetelui. Testele verifică aici
ce are voie să afle oaspetele în fiecare stare și că un răspuns la ofertă, venit ca eveniment
comun, continuă fluxul la fel ca endpointul `guest-answer`.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from app.core.clock import utcnow
from app.core.contracts.events import GuestMessageReceived, GuestOfferAnswered
from app.features.f2_incidents import service as service_module
from app.features.f2_incidents import templates
from conftest import app_client, approve, booking_by_guest, drain, property_by_name, reject

AC = "The air conditioning is not working"
LEAK = "Water is leaking under the kitchen sink."


@pytest.fixture(autouse=True)
def fixed_local_time(monkeypatch):
    monkeypatch.setattr(service_module, "local_time_of_day", lambda: time(10, 0))


@pytest.fixture
def waiting(settings, monkeypatch):
    """Oaspetele nu răspunde singur: răspunsul vine din conversație."""
    monkeypatch.setenv("F2_SIM_GUEST_ANSWER", "off")
    with app_client(settings) as client:
        yield client


def report(client, description: str, property_name: str = "Calea Victoriei Apartment") -> dict:
    prop = property_by_name(client, property_name)
    body = {"property_id": prop["id"], "description": description, "staff_id": "staff_maria"}
    assert client.post("/sim/staff-report", params={"wait": True}, json=body).json()["completed"]
    return next(i for i in client.get("/incidents").json() if i["property_id"] == prop["id"])


def incident(client, incident_id: str) -> dict:
    return client.get(f"/incidents/{incident_id}").json()


def topics(client, booking: dict) -> list:
    return client.app.state.container.guest_topics.for_booking(booking["id"])


def answer_from_chat(client, opened: dict, booking: dict, choice: str) -> None:
    """Ce publică copilotul când oaspetele răspunde la ofertă în conversație."""
    container = client.app.state.container
    (topic,) = topics(client, booking)
    assert container.guest_topics.answer_offer(topic.id, choice)
    event = GuestOfferAnswered(
        topic_id=topic.id, booking_id=booking["id"], feature="f2_incidents", choice=choice
    )
    client.portal.call(container.bus.publish, event)
    assert drain(client)


def outbound(client, booking: dict) -> list[str]:
    history = client.get(f"/bookings/{booking['id']}/messages").json()
    return [m["body"] for m in history if m["direction"] == "outbound"]


# --- Ce află oaspetele, pe stări ------------------------------------------------------------


def test_before_the_host_decides_the_guest_view_promises_nothing(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    report(waiting, AC)

    (topic,) = topics(waiting, lukas)
    assert topic.feature == "f2_incidents" and topic.kind == "incident"
    assert topic.facts["problem"] == "the air conditioning is not working"
    assert "assessing" in topic.facts["status"] and "repair_estimate" not in topic.facts
    assert topic.offer is None  # estimarea și relocarea sunt încă doar o propunere către host


def test_an_approved_offer_is_published_with_the_details_and_the_listing_link(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    target = property_by_name(waiting, "Herastrau Family Flat")
    opened = report(waiting, AC)
    approve(waiting, opened["approval_id"], "relocate")

    (topic,) = topics(waiting, lukas)
    offer = topic.offer
    assert offer.status == "pending" and "Herastrau Family Flat" in offer.question
    assert offer.details == {
        "name": target["name"], "address": target["address"], "bedrooms": 2, "sleeps": 5,
        "amenities": target["amenities"], "listing_url": target["listing_url"],
        "extra_cost": "none", "from": "2026-06-01", "until": lukas["check_out"],
    }  # fmt: skip
    assert offer.expires_at is not None
    assert "about 30 hours" in topic.facts["if_the_guest_stays"]
    # Oferta aprobată de host îi arată oaspetelui același link.
    assert target["listing_url"] in outbound(waiting, lukas)[0]


def test_the_template_offer_carries_the_link_only_when_there_is_one():
    with_link = templates.relocation_offer("Lukas Mueller", "A", "AC broken", "B", "Street 1", "https://x/1")
    assert "Here is the place: https://x/1" in with_link
    assert "Here is the place" not in templates.relocation_offer("Lukas Mueller", "A", "AC broken", "B", "Street 1")


def test_accepting_from_the_conversation_relocates_and_keeps_the_new_address_visible(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    target = property_by_name(waiting, "Herastrau Family Flat")
    opened = report(waiting, AC)
    approve(waiting, opened["approval_id"], "relocate")

    answer_from_chat(waiting, opened, lukas, "accept")

    now = incident(waiting, opened["id"])
    assert (now["status"], now["guest_answer"]) == ("relocated", "accept")
    assert now["correlation_id"] == opened["correlation_id"]
    assert len(outbound(waiting, lukas)) == 2  # oferta + confirmarea aprobată
    (topic,) = topics(waiting, lukas)
    assert topic.offer is None and topic.facts["current_accommodation"]["address"] == target["address"]
    assert topic.valid_until.isoformat() == lukas["check_out"]

    # După închiderea incidentului, oaspetele mutat tot la noua adresă stă.
    closure = next(a for a in waiting.get("/approvals").json() if a["kind"] == "incident_closure")
    approve(waiting, closure["id"])
    assert incident(waiting, opened["id"])["status"] == "resolved"
    (topic,) = topics(waiting, lukas)
    assert topic.facts["current_accommodation"]["name"] == "Herastrau Family Flat"


def test_declining_from_the_conversation_keeps_the_guest_and_shares_the_approved_estimate(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    opened = report(waiting, AC)
    approve(waiting, opened["approval_id"], "relocate")

    answer_from_chat(waiting, opened, lukas, "decline")

    now = incident(waiting, opened["id"])
    assert (now["status"], now["guest_answer"]) == ("remediation_scheduled", "stay")
    (topic,) = topics(waiting, lukas)
    assert topic.offer is None and "chose to stay" in topic.facts["relocation"]
    # Personalul simulat a terminat deja: până confirmă hostul, oaspetelui nu i se spune „reparat”.
    assert "host is checking" in topic.facts["status"] and "not tell the guest it is fixed" in topic.facts["note"]


def test_a_scheduled_repair_shares_only_the_estimate_the_host_approved(settings, monkeypatch):
    monkeypatch.setenv("F2_SIM_TASK_DELAY_S", "600")  # reparația rămâne în lucru
    with app_client(settings) as client:
        lukas = booking_by_guest(client, "Lukas Mueller")
        opened = report(client, LEAK)
        approve(client, opened["approval_id"], "remediate")

        (topic,) = topics(client, lukas)
        assert topic.facts["status"] == "The repair is scheduled with the maintenance team."
        assert topic.facts["repair_estimate"] == "about 2 hours"


def test_the_topic_closes_when_the_incident_does(client):
    lukas = booking_by_guest(client, "Lukas Mueller")
    opened = report(client, LEAK)
    approve(client, opened["approval_id"], "remediate")
    closure = next(a for a in client.get("/approvals").json() if a["kind"] == "incident_closure")
    assert len(topics(client, lukas)) == 1

    approve(client, closure["id"])

    assert incident(client, opened["id"])["status"] == "resolved"
    assert topics(client, lukas) == []


def test_a_dismissed_incident_leaves_nothing_to_tell_the_guest(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    opened = report(waiting, AC)
    assert len(topics(waiting, lukas)) == 1

    reject(waiting, opened["approval_id"])

    assert topics(waiting, lukas) == []


def test_an_incident_without_an_affected_stay_publishes_nothing(waiting):
    container = waiting.app.state.container
    report(waiting, LEAK, "Aviatiei Family Apartment")  # nicio rezervare acolo

    rows = container.db.query("SELECT id FROM guest_topics")
    assert rows == []


def test_a_move_that_could_not_be_arranged_is_said_as_such(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    container = waiting.app.state.container
    opened = report(waiting, AC)
    approve(waiting, opened["approval_id"], "relocate")
    target = container.world.get_property(incident(waiting, opened["id"])["relocation_property_id"])
    container.world.upsert_property(target.model_copy(update={"max_guests": 1}))

    answer_from_chat(waiting, opened, lukas, "accept")

    assert incident(waiting, opened["id"])["status"] == "awaiting_decision"
    (topic,) = topics(waiting, lukas)
    assert topic.offer is None and "could not be arranged" in topic.facts["status"]


# --- Răspunsul și activitatea oaspetelui -----------------------------------------------------


def test_an_answer_to_an_offer_that_is_no_longer_open_changes_nothing(waiting):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    container = waiting.app.state.container
    opened = report(waiting, AC)
    approve(waiting, opened["approval_id"], "remediate")  # nu există nicio ofertă
    before = incident(waiting, opened["id"])["status"]

    event = GuestOfferAnswered(
        topic_id=f"f2_incidents:incident:{opened['id']}", booking_id=lukas["id"],
        feature="f2_incidents", choice="accept",
    )  # fmt: skip
    waiting.portal.call(container.bus.publish, event)
    assert drain(waiting)

    assert incident(waiting, opened["id"])["status"] == before
    titles = [n["title"] for n in waiting.get("/notifications").json()]
    assert "A guest answered a relocation offer that is no longer open" in titles


def test_a_guest_who_keeps_writing_is_not_timed_out(waiting, monkeypatch):
    lukas = booking_by_guest(waiting, "Lukas Mueller")
    container = waiting.app.state.container
    opened = report(waiting, AC)
    approve(waiting, opened["approval_id"], "relocate")
    first_deadline = incident(waiting, opened["id"])["guest_deadline"]

    # După 50 de minute oaspetele pune o întrebare: termenul de 60 de minute repornește.
    later = utcnow() + timedelta(minutes=50)
    monkeypatch.setattr(service_module, "utc_now", lambda: later)
    message = GuestMessageReceived(
        message_id="msg_x", booking_id=lukas["id"], property_id=lukas["property_id"],
        text="Does it have parking?", pms_provider="mock", source_channel="booking_com",
    )  # fmt: skip
    service = next(
        s.handler.__self__ for s in container.bus.subscriptions() if s.name == "f2_incidents.on_guest_message"
    )
    waiting.portal.call(service.on_guest_message, message)
    assert incident(waiting, opened["id"])["guest_deadline"] > first_deadline

    even_later = later + timedelta(minutes=20)  # 70 de minute de la ofertă, 20 de la întrebare
    monkeypatch.setattr(service_module, "utc_now", lambda: even_later)
    waiting.get("/incidents")
    assert drain(waiting)
    assert incident(waiting, opened["id"])["status"] == "awaiting_guest"
