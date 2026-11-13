"""F2: sesizare -> incident -> aprobare cu două opțiuni -> acțiune după decizie."""

from __future__ import annotations

from app.core.contracts.models import Property
from app.features.f2_incidents.service import is_compatible
from conftest import approve, booking_by_guest, events_of, property_by_name, reject


def report(client, property_name: str, description: str = "Water leak under the sink") -> dict:
    prop = property_by_name(client, property_name)
    result = client.post(
        "/sim/staff-report",
        params={"wait": True},
        json={"property_id": prop["id"], "description": description, "staff_id": "staff_maria"},
    ).json()
    assert result["status"] == "published" and result["completed"] is True
    return prop


def test_staff_report_opens_an_incident_and_asks_the_host_to_choose(client):
    apartment = report(client, "Calea Victoriei Apartment")
    lukas = booking_by_guest(client, "Lukas Mueller")  # oaspetele cazat acum acolo

    (incident,) = client.get("/incidents").json()
    assert incident["status"] == "awaiting_decision"
    assert (incident["property_id"], incident["booking_id"]) == (apartment["id"], lukas["id"])
    assert (incident["reported_via"], incident["reporter"]) == ("staff", "Maria Ionescu")

    (approval,) = client.get("/approvals").json()
    assert (approval["feature"], approval["kind"]) == ("f2_incidents", "incident_resolution")
    assert [o["key"] for o in approval["options"]] == ["remediate", "relocate"]
    assert "Herastrau Family Flat" in approval["options"][1]["label"]  # singura liberă și compatibilă
    assert incident["approval_id"] == approval["id"]
    assert client.get(f"/bookings/{lukas['id']}/messages").json() == []  # nimic fără aprobare


def test_relocation_after_approval(client):
    report(client, "Calea Victoriei Apartment")
    lukas = booking_by_guest(client, "Lukas Mueller")
    target = property_by_name(client, "Herastrau Family Flat")
    (approval,) = client.get("/approvals").json()

    assert approve(client, approval["id"], "relocate")["completed"] is True

    (incident,) = client.get("/incidents").json()
    assert incident["status"] == "relocated" and incident["relocation_property_id"] == target["id"]
    # Proprietatea țintă e blocată pe restul sejurului (azi -> check-out), apoi e din nou liberă.
    days = client.get(f"/properties/{target['id']}/calendar", params={"days": 4}).json()
    assert [d["available"] for d in days] == [False, False, False, True]
    (message,) = client.get(f"/bookings/{lukas['id']}/messages").json()
    assert message["sender"] == "host" and "Herastrau Family Flat" in message["body"]
    assert any("relocated to Herastrau" in n["title"] for n in client.get("/notifications").json())
    assert events_of(client, "IncidentResolved")[0]["payload"]["resolution"] == "relocation"


def test_remediation_after_approval(client):
    report(client, "Calea Victoriei Apartment")
    lukas = booking_by_guest(client, "Lukas Mueller")
    target = property_by_name(client, "Herastrau Family Flat")
    (approval,) = client.get("/approvals").json()

    approve(client, approval["id"], "remediate")

    (incident,) = client.get("/incidents").json()
    assert incident["status"] == "remediation_scheduled"
    assert "Andrei Stan (maintenance)" in incident["resolution"]
    (message,) = client.get(f"/bookings/{lukas['id']}/messages").json()
    assert "Our team is on it" in message["body"]
    days = client.get(f"/properties/{target['id']}/calendar", params={"days": 3}).json()
    assert all(d["available"] for d in days)  # nicio blocare fără relocare


def test_rejected_incident_is_dismissed_without_any_action(client):
    report(client, "Calea Victoriei Apartment")
    lukas = booking_by_guest(client, "Lukas Mueller")
    (approval,) = client.get("/approvals").json()

    reject(client, approval["id"])

    assert client.get("/incidents").json()[0]["status"] == "dismissed"
    assert client.get(f"/bookings/{lukas['id']}/messages").json() == []


def test_relocation_is_not_offered_without_a_compatible_free_property(client):
    report(client, "Brasov Mountain View Loft")  # singura proprietate din Brașov

    (approval,) = client.get("/approvals").json()
    assert [o["key"] for o in approval["options"]] == ["remediate"]
    assert "relocation is not offered" in approval["summary"]
    assert client.get("/incidents").json()[0]["relocation_property_id"] is None


def test_action_runs_once_even_if_the_host_decides_twice(client):
    report(client, "Calea Victoriei Apartment")
    lukas = booking_by_guest(client, "Lukas Mueller")
    (approval,) = client.get("/approvals").json()

    first = approve(client, approval["id"], "relocate")
    second = approve(client, approval["id"], "relocate")

    assert (first["applied"], second["applied"]) == (True, False)
    assert len(client.get(f"/bookings/{lukas['id']}/messages").json()) == 1
    assert len(events_of(client, "IncidentResolved")) == 1


def test_compatibility_rule():
    original = Property(id="a", name="A", city="Bucharest", bedrooms=1, max_guests=2)
    same_city = Property(id="b", name="B", city="bucharest ", bedrooms=2, max_guests=4)

    assert is_compatible(same_city, original, guest_count=2)
    assert not is_compatible(original, original, guest_count=2)  # aceeași proprietate
    assert not is_compatible(same_city.model_copy(update={"city": "Brasov"}), original, 2)
    assert not is_compatible(same_city.model_copy(update={"max_guests": 1}), original, 2)
    assert not is_compatible(same_city.model_copy(update={"bedrooms": 0}), original, 2)
