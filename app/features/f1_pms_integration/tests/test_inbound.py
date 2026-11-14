"""F1: webhook care filtrează, deduplică, mapează și publică. Testat cu payload-uri de exemplu."""

from __future__ import annotations

import asyncio

from app.core.id_mapper import RESERVATION
from app.features.f1_pms_integration.inbound import InboundPipeline
from conftest import app_client, booking_by_guest, drain, events_of, make_settings

EMMA_RESERVATION = "MOCK-RES-1001"  # ID extern, cunoscut doar de PMS și de tabela de corespondențe


def payload(message_id: str = "MSG-1", sender_type: str = "guest", reservation: str = EMMA_RESERVATION) -> dict:
    """Payload de exemplu, în forma trimisă de PMS-ul simulat."""
    return {
        "event": "message.created",
        "message": {
            "id": message_id,
            "reservation_id": reservation,
            "sender_type": sender_type,
            "body": "What time is check-in?",
            "sent_at": "2026-06-01T09:30:00+00:00",
        },
    }


def post_webhook(client, body, provider: str = "mock") -> dict:
    response = client.post(f"/webhooks/{provider}", json=body)
    assert response.status_code == 200  # PMS-ul nu trebuie să retrimită
    drain(client)
    return response.json()


def test_guest_message_is_mapped_and_published(client):
    emma = booking_by_guest(client, "Emma Johnson")

    assert post_webhook(client, payload())["status"] == "published"

    (event,) = events_of(client, "GuestMessageReceived")
    assert event["source"] == "webhook:mock"
    assert event["payload"]["booking_id"] == emma["id"]
    assert event["payload"]["property_id"] == emma["property_id"]
    assert event["payload"]["text"] == "What time is check-in?"
    assert (event["payload"]["pms_provider"], event["payload"]["source_channel"]) == ("mock", "airbnb")
    assert "MOCK-" not in str(event["payload"])  # în eveniment circulă doar ID-uri interne
    messages = client.get(f"/bookings/{emma['id']}/messages").json()
    assert messages[0]["direction"] == "inbound" and messages[0]["id"] == event["payload"]["message_id"]

    (entry,) = client.get("/f1/webhook-log").json()
    assert entry["payload"] == payload() and entry["outcome"] == "published"  # payload-ul brut e păstrat
    assert entry["event_id"] == event["id"]


def test_anti_loop_filter_ignores_messages_not_sent_by_the_guest(client):
    """PMS-ul trimite webhook și pentru mesajele noastre; procesarea lor ar crea o buclă."""
    emma = booking_by_guest(client, "Emma Johnson")

    assert post_webhook(client, payload("MSG-H", sender_type="host"))["status"] == "ignored_not_guest"
    assert post_webhook(client, payload("MSG-X", sender_type=""))["status"] == "ignored_not_guest"

    assert events_of(client, "GuestMessageReceived") == []
    assert client.get(f"/bookings/{emma['id']}/messages").json() == []
    assert client.get("/agent/runs").json() == []  # copilotul nu a pornit


def test_copilot_reply_echoed_by_the_pms_does_not_start_a_loop(client):
    """Cap-coadă: mesaj -> răspuns -> ecoul răspunsului ca webhook `host` -> ignorat."""
    emma = booking_by_guest(client, "Emma Johnson")
    post_webhook(client, payload("MSG-1"))
    reply = client.get(f"/bookings/{emma['id']}/messages").json()[-1]
    assert reply["sender"] == "copilot"

    echo = payload("MSG-ECHO", sender_type="host")
    echo["message"]["body"] = reply["body"]
    assert post_webhook(client, echo)["status"] == "ignored_not_guest"

    assert len(events_of(client, "GuestMessageReceived")) == 1
    assert len(client.get("/agent/runs").json()) == 1


def test_retried_webhook_is_deduplicated_by_message_id(client):
    emma = booking_by_guest(client, "Emma Johnson")

    statuses = [post_webhook(client, payload("MSG-RETRY"))["status"] for _ in range(5)]

    assert statuses == ["published"] + ["duplicate"] * 4
    assert len(events_of(client, "GuestMessageReceived")) == 1
    inbound = [m for m in client.get(f"/bookings/{emma['id']}/messages").json() if m["direction"] == "inbound"]
    assert len(inbound) == 1
    assert post_webhook(client, payload("MSG-OTHER"))["status"] == "published"  # alt mesaj trece


def test_message_on_an_unmapped_reservation_is_logged_and_the_host_notified(client):
    result = post_webhook(client, payload("MSG-U", reservation="MOCK-RES-UNKNOWN"))

    assert result["status"] == "unmapped"
    assert events_of(client, "GuestMessageReceived") == []  # nu intră în fluxul de business
    (notification,) = client.get("/notifications").json()
    assert notification["level"] == "warning" and "unmapped reservation" in notification["title"]
    assert client.get("/f1/webhook-log").json()[0]["outcome"] == "unmapped"


def test_other_payloads_are_ignored_but_always_answered_with_200(client):
    assert post_webhook(client, {"event": "reservation.updated"})["status"] == "ignored_not_message"
    assert post_webhook(client, ["not", "an", "object"])["status"] == "ignored_not_message"
    assert post_webhook(client, payload(), provider="guesty")["status"] == "error"

    garbage = client.post("/webhooks/mock", content=b"\xff not json", headers={"content-type": "application/json"})
    assert garbage.status_code == 200 and garbage.json()["status"] == "ignored_not_message"

    assert events_of(client, "GuestMessageReceived") == []
    assert len(client.get("/f1/webhook-log").json()) == 4  # totul e salvat brut


def test_simulator_and_webhook_produce_the_same_event(client):
    emma = booking_by_guest(client, "Emma Johnson")
    post_webhook(client, payload("MSG-W"))
    simulated = client.post(
        "/sim/guest-message",
        params={"wait": True},
        json={"booking_id": emma["id"], "text": "What time is check-in?"},
    ).json()
    assert simulated["outcome"] == "published"

    from_sim, from_webhook = events_of(client, "GuestMessageReceived")
    assert (from_sim["source"], from_webhook["source"]) == ("simulator", "webhook:mock")
    assert from_sim["type"] == from_webhook["type"]
    comparable = lambda e: {k: v for k, v in e["payload"].items() if k != "message_id"}  # noqa: E731
    assert comparable(from_sim) == comparable(from_webhook)


def test_webhook_token_is_enforced_when_configured(tmp_path):
    settings = make_settings(tmp_path, webhook_token="s3cret")
    with app_client(settings) as client:
        assert client.post("/webhooks/mock", json=payload()).status_code == 403
        assert client.post("/webhooks/mock", params={"token": "wrong"}, json=payload()).status_code == 403
        accepted = client.post("/webhooks/mock", params={"token": "s3cret"}, json=payload())
        assert accepted.status_code == 200 and accepted.json()["status"] == "published"


def test_successful_sync_replays_unmapped_once_and_delivers_to_copilot(client):
    c = client.app.state.container
    emma = booking_by_guest(client, "Emma Johnson")
    c.ids.unlink("mock", RESERVATION, EMMA_RESERVATION)
    body = payload("MSG-LATE-SYNC")
    assert post_webhook(client, body)["status"] == "unmapped"
    assert post_webhook(client, body)["status"] == "duplicate"

    response = client.post("/integrations/mock/sync")
    assert response.status_code == 200 and drain(client)
    (event,) = events_of(client, "GuestMessageReceived")
    assert event["payload"]["booking_id"] == emma["id"]
    assert event["source"] == "webhook:mock" and "MOCK-" not in str(event["payload"])
    assert len(client.get("/agent/runs").json()) == 1
    messages = client.get(f"/bookings/{emma['id']}/messages").json()
    assert [m["direction"] for m in messages] == ["inbound", "outbound"]
    published = next(row for row in client.get("/f1/webhook-log").json() if row["outcome"] == "published")
    assert published["detail"] == "reprocessed after sync" and published["event_id"] == event["id"]

    assert client.post("/integrations/mock/sync").status_code == 200
    result = client.post("/f1/reprocess-unmapped", params={"provider": "mock", "wait": True}).json()
    assert result == {"provider": "mock", "reprocessed": 0, "remaining": 0, "completed": True}
    assert post_webhook(client, body)["status"] == "duplicate"
    assert len(events_of(client, "GuestMessageReceived")) == 1


def test_manual_mapping_automatically_replays_without_another_sync(client):
    emma = booking_by_guest(client, "Emma Johnson")
    body = payload("MSG-LATE-LINK", reservation="MOCK-MANUAL")
    assert post_webhook(client, body)["status"] == "unmapped"
    mapping = {"connection": "mock", "entity_type": RESERVATION,
               "external_id": "MOCK-MANUAL", "internal_id": emma["id"]}
    assert client.put("/integrations/mappings", json=mapping).status_code == 200
    assert drain(client)
    (event,) = events_of(client, "GuestMessageReceived")
    assert event["payload"]["booking_id"] == emma["id"]
    assert client.put("/integrations/mappings", json=mapping).status_code == 200
    assert drain(client)
    assert post_webhook(client, body)["status"] == "duplicate"
    assert len(events_of(client, "GuestMessageReceived")) == 1


def restore_reservation_mapping(c, external_id: str, internal_id: str) -> None:
    """Simulează maparea restaurată dintr-un import, fără hookul de mapare manuală."""
    c.db.execute(
        "INSERT INTO id_map (connection, entity_type, external_id, internal_id, created_at) "
        "VALUES ('mock', 'reservation', ?, ?, '2026-06-01T10:00:00Z')",
        (external_id, internal_id),
    )


def test_reprocess_endpoint_preserves_pending_rows_and_publishes_in_receipt_order(client):
    c = client.app.state.container
    emma = booking_by_guest(client, "Emma Johnson")
    c.ids.unlink("mock", RESERVATION, EMMA_RESERVATION)
    for key in ("MSG-FIRST", "MSG-SECOND"):
        assert post_webhook(client, payload(key))["status"] == "unmapped"
    untouched = client.post("/f1/reprocess-unmapped").json()
    assert untouched == {"provider": None, "reprocessed": 0, "remaining": 2}
    restore_reservation_mapping(c, EMMA_RESERVATION, emma["id"])
    assert client.post("/f1/reprocess-unmapped", params={"provider": "hospitable"}).json()["reprocessed"] == 0
    result = client.post("/f1/reprocess-unmapped", params={"wait": True}).json()
    assert result == {"provider": None, "reprocessed": 2, "remaining": 0, "completed": True}
    logs = list(reversed(client.get("/f1/webhook-log").json()))
    assert [row["external_message_id"] for row in logs] == ["MSG-FIRST", "MSG-SECOND"]
    assert [row["event_id"] for row in logs] == [e["id"] for e in reversed(events_of(client, "GuestMessageReceived"))]
    assert c.db.query_one("SELECT COUNT(*) AS n FROM f1_seen_messages")["n"] == 2


def test_mapped_id_without_a_booking_remains_pending(client):
    assert post_webhook(client, payload("MSG-NO-BOOKING", reservation="MISSING"))["status"] == "unmapped"
    response = client.put("/integrations/mappings", json={
        "connection": "mock", "entity_type": RESERVATION,
        "external_id": "MISSING", "internal_id": "bk_not_created",
    })
    assert response.status_code == 200 and drain(client)
    result = client.post("/f1/reprocess-unmapped").json()
    assert result["reprocessed"] == 0 and result["remaining"] == 1
    assert events_of(client, "GuestMessageReceived") == []


async def test_concurrent_replay_publishes_each_pending_message_once(container, monkeypatch):
    c = container
    pipeline = InboundPipeline(c._make_feature_context("f1_pms_integration"))
    booking_id = c.ids.to_internal("mock", RESERVATION, EMMA_RESERVATION)
    c.ids.unlink("mock", RESERVATION, EMMA_RESERVATION)
    assert (await pipeline.process("mock", payload("MSG-CONCURRENT"), source="webhook:mock")).outcome == "unmapped"
    restore_reservation_mapping(c, EMMA_RESERVATION, booking_id)
    original_publish = c.bus.publish

    async def slow_publish(event):
        await asyncio.sleep(0.01)
        return await original_publish(event)

    monkeypatch.setattr(c.bus, "publish", slow_publish)
    reports = await asyncio.gather(*(pipeline.reprocess_unmapped("mock") for _ in range(5)))
    assert sum(report["reprocessed"] for report in reports) == 1
    assert await c.bus.join(timeout=10)
    assert len([event for event in c.bus.list_events() if event["type"] == "GuestMessageReceived"]) == 1
    assert len([message for message in c.conversations.history(booking_id) if message.direction == "inbound"]) == 1


async def test_sync_wrapper_serializes_webhook_and_manual_sync(container, monkeypatch):
    c = container
    connection = c.pms.get("mock")
    original_properties = connection.list_properties
    active = maximum = 0

    async def slow_properties():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            return await original_properties()
        finally:
            active -= 1

    monkeypatch.setattr(connection, "list_properties", slow_properties)
    monkeypatch.setattr(connection, "is_reservation_webhook", lambda p: p.get("event") == "reservation.cancelled", raising=False)
    pipeline = InboundPipeline(c._make_feature_context("f1_pms_integration"))
    results = await asyncio.gather(
        pipeline.process("mock", {"event": "reservation.cancelled"}, source="webhook:mock"),
        c.sync.sync("mock"), c.sync.sync("mock", publish_events=False),
    )
    assert results[0].outcome == "synced" and maximum == 1


def test_failed_sync_does_not_replay_partial_mappings(client, monkeypatch):
    c = client.app.state.container
    connection = c.pms.get("mock")
    reservations = client.portal.call(connection.list_reservations)
    emma = next(item for item in reservations if item.external_id == EMMA_RESERVATION)
    c.ids.unlink("mock", RESERVATION, EMMA_RESERVATION)
    assert post_webhook(client, payload("MSG-PARTIAL"))["status"] == "unmapped"

    async def broken_reservations():
        return [emma, object()]

    monkeypatch.setattr(connection, "list_reservations", broken_reservations)
    assert client.post("/integrations/mock/sync").status_code == 502
    assert drain(client)
    assert c.ids.to_internal("mock", RESERVATION, EMMA_RESERVATION) is not None
    assert events_of(client, "GuestMessageReceived") == []
    assert client.get("/f1/webhook-log").json()[0]["outcome"] == "unmapped"


def test_reservation_sync_failure_is_logged_and_still_answered_200(client, monkeypatch):
    connection = client.app.state.container.pms.get("mock")
    monkeypatch.setattr(connection, "is_reservation_webhook", lambda p: True, raising=False)

    async def failed_properties():
        raise RuntimeError("PMS unavailable")

    monkeypatch.setattr(connection, "list_properties", failed_properties)
    result = post_webhook(client, {"event": "reservation.cancelled"})
    assert result["status"] == "error"
    assert client.get("/f1/webhook-log").json()[0]["outcome"] == "error"
    assert any(n["title"] == "PMS webhook processing failed" for n in client.get("/notifications").json())


async def test_mapping_during_replay_schedules_another_pass(container, monkeypatch):
    c = container
    pipeline = InboundPipeline(c._make_feature_context("f1_pms_integration"))
    for key, reservation in (("MSG-EARLY", "LATE-MAP"), ("MSG-SLOW", EMMA_RESERVATION)):
        c.ids.unlink("mock", RESERVATION, reservation)
        await pipeline.process("mock", payload(key, reservation=reservation), source="webhook:mock")
    booking = next(b for b in c.world.list_bookings() if b.guest_name == "Emma Johnson")
    restore_reservation_mapping(c, EMMA_RESERVATION, booking.id)
    entered, resume = asyncio.Event(), asyncio.Event()
    original_publish = c.bus.publish

    async def paused_publish(event):
        if event.event_type() == "GuestMessageReceived":
            entered.set()
            await resume.wait()
        return await original_publish(event)

    monkeypatch.setattr(c.bus, "publish", paused_publish)
    pipeline.schedule_reprocess("mock")
    await asyncio.wait_for(entered.wait(), timeout=2)
    restore_reservation_mapping(c, "LATE-MAP", booking.id)
    pipeline.schedule_reprocess("mock")
    resume.set()
    await pipeline.close()
    assert await c.bus.join(timeout=10)
    assert len([e for e in c.bus.list_events() if e["type"] == "GuestMessageReceived"]) == 2


def test_invalid_f1_settings_leave_shared_services_unwrapped(tmp_path, monkeypatch):
    from app.container import Container
    from app.core.id_mapper import IdMapper
    from app.core.sync_service import SyncService

    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_USERNAME", "username-only")
    monkeypatch.delenv("F1_OWNERREZ_WEBHOOK_PASSWORD", raising=False)
    c = Container(make_settings(tmp_path))
    try:
        f1 = next(feature for feature in c.features if feature.name == "f1_pms_integration")
        assert not f1.loaded
        assert c.sync.sync.__func__ is SyncService.sync
        assert c.ids.link.__func__ is IdMapper.link
    finally:
        asyncio.run(c.shutdown())
