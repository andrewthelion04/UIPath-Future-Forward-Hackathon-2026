"""F1 când publicarea evenimentului cade o dată: mesajul nu se pierde și nu se dublează
(auditul de integrare, C02–C04)."""

from __future__ import annotations

from app.core.contracts.events import GuestMessageReceived
from app.core.contracts.models import PmsProvider
from app.core.id_mapper import RESERVATION, IdMapper
from app.features.f1_pms_integration.tests.test_inbound import payload
from conftest import booking_by_guest, drain, events_of


def fail_once(container) -> None:
    original, state = container.bus.publish, {"failed": False}

    async def publish(event):
        if isinstance(event, GuestMessageReceived) and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("transient event persistence failure")
        return await original(event)

    container.bus.publish = publish


def inbound(client, booking: dict) -> list[dict]:
    history = client.get(f"/bookings/{booking['id']}/messages").json()
    return [m for m in history if m["direction"] == "inbound"]


def test_a_webhook_retry_recovers_a_message_whose_event_was_not_published(client):
    emma = booking_by_guest(client, "Emma Johnson")
    fail_once(client.app.state.container)

    first = client.post("/webhooks/mock", json=payload("MSG-RETRY")).json()
    assert first["status"] == "error"
    assert inbound(client, emma) == []  # nimic pe jumătate în conversație

    retry = client.post("/webhooks/mock", json=payload("MSG-RETRY")).json()
    assert retry["status"] == "published" and drain(client)
    assert len(events_of(client, "GuestMessageReceived")) == 1
    assert len(inbound(client, emma)) == 1
    # După livrare, o nouă retrimitere e din nou un duplicat.
    assert client.post("/webhooks/mock", json=payload("MSG-RETRY")).json()["status"] == "duplicate"


def test_a_failed_replay_does_not_duplicate_the_conversation_record(client):
    container = client.app.state.container
    emma = booking_by_guest(client, "Emma Johnson")
    assert client.post("/webhooks/mock", json=payload("MSG-LATE", reservation="LATE-1")).json()["status"] == "unmapped"
    IdMapper.link(container.ids, "mock", RESERVATION, "LATE-1", emma["id"])  # fără reprocesarea automată
    fail_once(container)

    first = client.post("/f1/reprocess-unmapped", params={"wait": True}).json()
    second = client.post("/f1/reprocess-unmapped", params={"wait": True}).json()

    assert (first["reprocessed"], first["remaining"]) == (0, 1)
    assert (second["reprocessed"], second["remaining"]) == (1, 0)
    assert len(inbound(client, emma)) == 1
    assert len(events_of(client, "GuestMessageReceived")) == 1


def test_a_message_mapped_to_a_booking_of_another_connection_is_not_processed(client):
    """O mapare greșită (făcută pe lângă API) nu trimite răspunsul prin alt PMS."""
    container = client.app.state.container
    emma = booking_by_guest(client, "Emma Johnson")
    booking = container.world.get_booking(emma["id"])
    container.world.upsert_booking(booking.model_copy(update={"pms_provider": PmsProvider.HOSPITABLE}))

    result = client.post("/webhooks/mock", json=payload("MSG-FOREIGN")).json()

    assert result["status"] == "unmapped" and drain(client)
    assert events_of(client, "GuestMessageReceived") == []
    assert inbound(client, emma) == []
