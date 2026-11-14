"""Editări persistente, întrebări pentru host și calendarul lunar."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.core.contracts.models import PmsProvider, BookingStatus
from app.ports.pms import PmsCapabilities, PmsError
from conftest import booking_by_guest, property_by_name
from ui.month_calendar import month_html


def ask(client):
    booking = booking_by_guest(client, "Emma Johnson")
    response = client.post("/sim/guest-message?wait=true", json={
        "booking_id":booking["id"], "text":"What is the Wi-Fi password?"})
    assert response.status_code == 200
    return booking, client.get("/answers").json()[0]


def test_question_can_be_answered_exactly_once_and_reply_is_in_conversation(client):
    booking, answer = ask(client)
    assert answer["question"] == "What is the Wi-Fi password?"
    assert answer["guest_name"] == "Emma Johnson"
    assert not any("manual follow-up" in n["title"] for n in client.get("/notifications").json())
    text = "The Wi-Fi password is Demo-Only-2026!"
    for _ in range(2):
        response = client.post(f"/answers/{answer['id']}/reply", json={"text":text})
        assert response.status_code == 200
        assert response.json()["status"] == "sent"
    assert client.get("/answers").json() == []
    messages = client.get(f"/bookings/{booking['id']}/messages").json()
    assert len([m for m in messages if m["body"] == text]) == 1
    assert messages[-1]["sender"] == "host"
    assert client.post(f"/answers/{answer['id']}/reply", json={"text":"Different answer"}).status_code == 409


def test_reply_routes_to_ownerrez_and_concurrent_submissions_do_not_duplicate(client):
    c = client.app.state.container
    booking = c.world.get_booking(booking_by_guest(client, "Emma Johnson")["id"])
    c.world.upsert_booking(booking.model_copy(update={"pms_provider":PmsProvider.OWNERREZ}))
    sent = []

    async def send(booking_id, text):
        await asyncio.sleep(0.01)
        sent.append((booking_id,text))

    c.pms.register(SimpleNamespace(provider="ownerrez", capabilities=PmsCapabilities(send_messages=True), send_message=send))
    answer = c.host_answers.request(booking.id, "ownerrez-guest-message", "Wi-Fi password?")

    async def together():
        return await asyncio.gather(*(c.host_answers.send(answer["id"], "Host-authorized text") for _ in range(2)))

    results = client.portal.call(together)
    assert all(r["status"] == "sent" for r in results)
    assert sent == [(booking.id,"Host-authorized text")]


def test_delivery_failure_remains_visible_and_is_not_blindly_retried(client, monkeypatch):
    booking, answer = ask(client)
    c = client.app.state.container
    calls = []

    async def fail(*args):
        calls.append(args)
        raise PmsError("Connection lost")

    monkeypatch.setattr(c.pms.get("mock"), "send_message", fail)
    path = f"/answers/{answer['id']}/reply"
    assert client.post(path, json={"text":"Password"}).status_code == 502
    assert client.get("/answers").json()[0]["status"] == "sending"
    assert client.post(path, json={"text":"Password"}).status_code == 409
    assert len(calls) == 1
    assert client.post(f"/answers/{answer['id']}/delivery-check", json={"delivered":False}).status_code == 200
    assert client.get("/answers").json()[0]["status"] == "pending"


@pytest.mark.parametrize("body", [{"text":" "}, {"text":"x"*4001}, {}, {"text":"ok","booking_id":"other"}])
def test_reply_validation(client, body):
    _, answer = ask(client)
    assert client.post(f"/answers/{answer['id']}/reply", json=body).status_code == 422


def test_cancelled_booking_cannot_receive_a_queued_answer(client):
    booking, answer = ask(client)
    c = client.app.state.container
    b = c.world.get_booking(booking["id"])
    c.world.upsert_booking(b.model_copy(update={"status":BookingStatus.CANCELLED}))
    assert client.post(f"/answers/{answer['id']}/reply", json={"text":"Hello"}).status_code == 409


def test_historical_incident_answer_duplicates_are_suppressed_without_hiding_wifi(client):
    c = client.app.state.container
    booking = booking_by_guest(client, "Emma Johnson")
    client.post("/sim/guest-message?wait=true", json={"booking_id":booking["id"],"text":"The shower is broken."})
    message = next(m for m in client.get(f"/bookings/{booking['id']}/messages").json() if m["sender"] == "guest")
    old = c.host_answers.request(booking["id"], message["id"], message["body"])
    wifi = c.host_answers.request(booking["id"], "independent-wifi", "Wi-Fi password?")
    c.host_answers.suppress_reported_issues()
    assert [a["id"] for a in c.host_answers.list()] == [wifi["id"]]
    assert client.post(f"/answers/{old['id']}/reply", json={"text":"Already handled"}).status_code == 409


def test_local_property_changes_survive_sync_and_can_be_restored(client):
    p = property_by_name(client,"Old Town Studio")
    response = client.put(f"/properties/{p['id']}", json={"name":"Host's edited studio", "amenities":["wifi","desk"]})
    assert response.status_code == 200
    assert client.post("/integrations/mock/sync").status_code == 200
    updated = next(x for x in client.get("/properties").json() if x["id"] == p["id"])
    assert updated["name"] == "Host's edited studio"
    assert updated["amenities"] == ["wifi","desk"]
    # Raw PMS data remains intact; clearing overrides reveals it again.
    restored = client.delete(f"/properties/{p['id']}/overrides").json()
    assert restored == p


@pytest.mark.parametrize("body", [{"max_guests":0},{"base_price":-1},{"name":" "},{"name":None},
    {"currency":"euro"},{"pms_provider":"ownerrez"},{"id":"another"},{"listing_url":"javascript:alert(1)"}])
def test_property_edit_rejects_invalid_data_and_identity_changes(client, body):
    p = client.get("/properties").json()[0]
    assert client.put(f"/properties/{p['id']}", json=body).status_code == 422


def test_calendar_spans_weeks_excludes_checkout_and_escapes_guest_names():
    props = [{"id":"p", "name":"<Apartment>"}]
    bookings = [{"id":"b", "property_id":"p", "guest_name":"<script>bad()</script>",
                 "guest_count":2,"status":"confirmed", "check_in":"2026-11-01", "check_out":"2026-11-03"}]
    html = month_html(2026,11,props,bookings,[],date(2026,11,14))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'grid-column:7/span 1' in html  # Sunday Nov 1
    assert 'grid-column:1/span 1' in html  # Monday Nov 2; Tuesday checkout excluded
    assert 'sp-date today' in html
    bookings[0]["status"] = "cancelled"
    assert "bad()" not in month_html(2026,11,props,bookings,[],date(2026,11,14))
