"""F1: webhook care filtrează, deduplică, mapează și publică. Testat cu payload-uri de exemplu."""

from __future__ import annotations

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
