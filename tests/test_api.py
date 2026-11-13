"""API-ul REST comun: endpointurile din CLAUDE.md §3 există și răspund cu modelele comune."""

from __future__ import annotations

from conftest import booking_by_guest, property_by_name


def test_health_reports_providers_and_features(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["providers"] == {"pms": "mock", "llm": "mock", "reviews": "mock", "market": "mock"}
    assert len(health["features"]) == 5 and all(f["loaded"] for f in health["features"])


def test_common_endpoints_exist(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {
        "/properties",
        "/bookings",
        "/notifications",
        "/approvals",
        "/approvals/{approval_id}/decision",
        "/incidents",
        "/sim/guest-message",
        "/integrations/{pms_provider}/sync",
        "/sim/staff-report",
        "/sim/cancel-booking",
        "/sim/review-analysis",
        "/sim/reset",
        "/webhooks/{provider}",
        "/events",
        "/agent/runs",
    } <= paths


def test_world_endpoints_use_internal_ids_only(client):
    properties = client.get("/properties").json()
    bookings = client.get("/bookings").json()
    assert len(properties) == 4 and len(bookings) == 7
    assert all(p["id"].startswith("prop_") for p in properties)
    assert all(b["id"].startswith("bk_") and b["property_id"].startswith("prop_") for b in bookings)
    assert "MOCK-" not in client.get("/properties").text + client.get("/bookings").text

    emma = booking_by_guest(client, "Emma Johnson")
    assert (emma["pms_provider"], emma["source_channel"]) == ("mock", "airbnb")
    assert client.get(f"/bookings/{emma['id']}").json() == emma
    assert client.get(f"/bookings/{emma['id']}/messages").json() == []
    only = client.get("/bookings", params={"property_id": emma["property_id"], "status": "confirmed"}).json()
    assert {b["guest_name"] for b in only} == {"Emma Johnson", "Marie Dubois"}
    assert client.get("/host").json()["rules"]["check_in_time"] == "15:00"
    assert len(client.get("/staff").json()) == 3


def test_unknown_ids_return_404(client):
    assert client.get("/bookings/bk_missing").status_code == 404
    assert client.get("/bookings/bk_missing/messages").status_code == 404
    assert client.get("/properties/prop_missing/calendar").status_code == 404
    assert client.get("/agent/runs/run_missing").status_code == 404
    assert client.post("/notifications/ntf_missing/read").status_code == 404
    assert client.post("/sim/staff-report", json={"property_id": "prop_missing", "description": "x y z"}).status_code == 404
    assert client.post("/sim/cancel-booking", json={"booking_id": "bk_missing"}).status_code == 404
    assert client.post("/sim/guest-message", json={"booking_id": "bk_missing", "text": "hi"}).status_code == 404
    assert client.post("/integrations/guesty/sync").status_code == 404


def test_calendar_shows_booked_nights(client):
    studio = property_by_name(client, "Old Town Studio")
    days = client.get(f"/properties/{studio['id']}/calendar", params={"days": 6}).json()
    assert [d["available"] for d in days] == [True, True, False, False, False, True]
    assert days[0]["price"] == 70.0 and days[4]["price"] == 80.5  # vineri: tarif de weekend


def test_sync_endpoint_is_repeatable(client):
    connections = client.get("/integrations").json()
    assert [(c["provider"], c["selected"]) for c in connections] == [("mock", True)]
    assert connections[0]["capabilities"]["send_messages"] is True

    first = client.post("/integrations/mock/sync").json()
    second = client.post("/integrations/mock/sync").json()
    assert first == second
    assert (first["properties_created"], first["bookings_created"]) == (0, 0)
    assert len(client.get("/properties").json()) == 4


def test_manual_mapping_for_debugging(client):
    studio = property_by_name(client, "Old Town Studio")
    body = {
        "connection": "airbnb",
        "entity_type": "listing",
        "external_id": "https://www.airbnb.com/rooms/123",
        "internal_id": studio["id"],
    }
    assert client.put("/integrations/mappings", json=body).status_code == 200
    assert client.put("/integrations/mappings", json=body).status_code == 200  # idempotent
    assert client.put("/integrations/mappings", json={**body, "internal_id": "prop_other"}).status_code == 409
    mappings = client.get("/integrations/mappings", params={"connection": "airbnb"}).json()
    assert [m["internal_id"] for m in mappings] == [studio["id"]]


def test_notifications_can_be_marked_read(client):
    studio = property_by_name(client, "Old Town Studio")
    client.post(
        "/sim/staff-report",
        params={"wait": True},
        json={"property_id": studio["id"], "description": "Broken lamp"},
    )
    unread = client.get("/notifications", params={"unread_only": True}).json()
    assert unread and unread[0]["read"] is False

    marked = client.post(f"/notifications/{unread[0]['id']}/read").json()
    assert marked["read"] is True
    assert client.post("/notifications/read-all").json() == {"marked": len(unread) - 1}
    assert client.get("/notifications", params={"unread_only": True}).json() == []


def test_reset_clears_everything_and_reseeds(client):
    emma = booking_by_guest(client, "Emma Johnson")
    client.post("/sim/guest-message", params={"wait": True}, json={"booking_id": emma["id"], "text": "Hello"})
    assert client.get("/events").json() and client.get("/agent/runs").json()

    reset = client.post("/sim/reset").json()

    assert reset["status"] == "reset" and reset["properties"] == 4
    assert client.get("/events").json() == []
    assert client.get("/agent/runs").json() == []
    assert client.get("/f1/webhook-log").json() == []
    assert booking_by_guest(client, "Emma Johnson")["id"] == emma["id"]  # ID-uri interne stabile


def test_extension_and_local_ui_origins_are_allowed_by_cors(client):
    """Extensia Chrome apelează API-ul din panoul lateral; alte origini sunt refuzate."""
    origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    preflight = client.options(
        "/approvals/apr_any/decision",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin

    allowed = client.get("/properties", headers={"Origin": "http://localhost:8501"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:8501"
    refused = client.get("/properties", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in refused.headers
