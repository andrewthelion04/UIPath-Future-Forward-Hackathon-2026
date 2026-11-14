"""Adaptoarele reale, testate fără rețea (httpx.MockTransport).

Testele fixează forma cererilor trimise și normalizarea răspunsurilor, pe baza documentației.
Nu înlocuiesc verificarea live: formele marcate „neconfirmat” în adaptoare rămân de validat.
"""

from __future__ import annotations

import gzip
import json
from datetime import date

import httpx
import pytest

from app.adapters.apify.reviews import ApifyReviewsAdapter, normalize_review, render_input
from app.adapters.hospitable.adapter import HospitableAdapter
from app.adapters.inside_airbnb.market import (
    InsideAirbnbMarketAdapter,
    normalize_amenities,
    parse_price,
)
from app.adapters.openrouter.llm import OpenRouterLLM
from app.adapters.ownerrez.adapter import OwnerRezAdapter
from app.container import Container
from app.core.agent.loop import AgentSpec
from app.core.agent.tools import RunContext, Tool, ToolArgs
from app.core.contracts.models import ComparableQuery, Property
from app.features.f5_relisting.relisting import cheapest_comparable
from app.ports.llm import ChatMessage, ChatRequest, LLMError, ToolCall, ToolSpec
from app.ports.pms import CapabilityNotSupported, PmsError, UnmappedEntity
from app.ports.reviews import ReviewsError
from conftest import app_client, drain, events_of, make_settings


class Recorder:
    """Transport HTTP fals: ține minte cererile și răspunde dintr-o funcție."""

    def __init__(self, respond) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.content]


# =====================================================================================
# Hospitable
# =====================================================================================

HOSPITABLE_PROPERTY = {
    "id": "prop-uuid-1",
    "name": "Internal name",
    "public_name": "Vienna City Flat",
    "address": {"city": "Vienna", "display": "Ringstrasse 1, Vienna"},
    "capacity": {"max": 4, "bedrooms": 2},
    "amenities": ["Wifi", "Air conditioning"],
    "currency": "EUR",
}
HOSPITABLE_RESERVATIONS = [
    {
        "id": "res-uuid-1",
        "platform": "airbnb",
        "arrival_date": "2026-07-01T15:00:00+02:00",
        "departure_date": "2026-07-04T11:00:00+02:00",
        "reservation_status": {"current": {"category": "accepted"}},
        "guests": {"total": 3},
        "guest": {"first_name": "Greta", "last_name": "Weber"},
        "properties": [{"id": "prop-uuid-1"}],
    },
    {
        "id": "res-uuid-2",
        "platform": "booking.com",
        "arrival_date": "2026-08-01",
        "departure_date": "2026-08-02",
        "reservation_status": {"current": {"category": "cancelled"}},
        "properties": [{"id": "prop-uuid-1"}],
    },
    {"id": "res-broken", "platform": "airbnb"},  # fără date: sărită, nu oprește sincronizarea
]


def hospitable_api(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path == "/v2/properties":
        return httpx.Response(200, json={"data": [HOSPITABLE_PROPERTY], "meta": {"last_page": 1}})
    if request.method == "GET" and request.url.path == "/v2/reservations":
        return httpx.Response(200, json={"data": HOSPITABLE_RESERVATIONS, "meta": {"last_page": 1}})
    if request.method == "POST" and request.url.path.endswith("/messages"):
        return httpx.Response(201, json={"data": {"id": "msg-out-1"}})
    return httpx.Response(404, json={"message": "not found"})


def hospitable(container, recorder: Recorder) -> HospitableAdapter:
    return HospitableAdapter(
        base_url="https://public.api.hospitable.com/v2",
        token="hsp-token",
        ids=container.ids,
        transport=recorder.transport,
    )


async def test_hospitable_lists_and_normalizes_properties_and_reservations(container):
    api = Recorder(hospitable_api)
    adapter = hospitable(container, api)

    (prop,) = await adapter.list_properties()
    reservations = await adapter.list_reservations()

    assert (prop.external_id, prop.name, prop.city) == ("prop-uuid-1", "Vienna City Flat", "Vienna")
    assert (prop.bedrooms, prop.max_guests, prop.amenities) == (2, 4, ["wifi", "air_conditioning"])
    first, second = reservations  # a treia e invalidă și sărită
    assert (first.guest_name, first.guest_count) == ("Greta Weber", 3)
    assert (first.check_in, first.check_out) == (date(2026, 7, 1), date(2026, 7, 4))
    assert (first.status, first.source_channel) == ("confirmed", "airbnb")
    assert (second.status, second.source_channel) == ("cancelled", "booking_com")

    assert all(r.headers["authorization"] == "Bearer hsp-token" for r in api.requests)
    reservations_call = next(r for r in api.requests if r.url.path == "/v2/reservations")
    assert reservations_call.url.params.get_list("properties[]") == ["prop-uuid-1"]
    await adapter.aclose()


async def test_hospitable_sends_messages_to_the_mapped_reservation(container):
    api = Recorder(hospitable_api)
    adapter = hospitable(container, api)
    container.ids.link("hospitable", "reservation", "res-uuid-1", "bk_internal")

    sent = await adapter.send_message("bk_internal", "Welcome!")

    (request,) = api.requests
    assert (request.method, request.url.path) == ("POST", "/v2/reservations/res-uuid-1/messages")
    assert json.loads(request.content) == {"body": "Welcome!"}
    assert sent.external_message_id == "msg-out-1"
    with pytest.raises(UnmappedEntity):
        await adapter.send_message("bk_unknown", "Hello")
    await adapter.aclose()


async def test_hospitable_errors_and_unconfirmed_calendar(container):
    adapter = hospitable(container, Recorder(lambda request: httpx.Response(401, text="bad token")))
    with pytest.raises(PmsError, match="HTTP 401"):
        await adapter.list_properties()
    with pytest.raises(CapabilityNotSupported):
        await adapter.set_price("prop_x", date(2026, 7, 1), date(2026, 7, 2), 90)
    assert adapter.capabilities.write_prices is False and adapter.capabilities.send_messages is True
    await adapter.aclose()


def test_hospitable_webhook_parsing_is_permissive(container):
    adapter = hospitable(container, Recorder(hospitable_api))
    enveloped = {
        "id": "evt-1",
        "action": "message.created",
        "created": "2026-06-01T10:00:00Z",
        "data": {
            "id": "msg-1",
            "reservation_id": "res-uuid-1",
            "sender_type": "guest",
            "body": "Is early check-in possible?",
            "sender": {"first_name": "Greta"},
        },
    }
    parsed = adapter.parse_webhook(enveloped)
    assert (parsed.external_message_id, parsed.external_reservation_id) == ("msg-1", "res-uuid-1")
    assert (parsed.sender_type, parsed.body) == ("guest", "Is early check-in possible?")

    # Altă formă plauzibilă: fără plic, cu rezervarea imbricată și rolul pe `sender`.
    flat = {"reservation": {"id": "res-uuid-1"}, "sender": {"type": "HOST"}, "body": "Sure!"}
    parsed_flat = adapter.parse_webhook(flat)
    assert (parsed_flat.sender_type, parsed_flat.external_reservation_id) == ("host", "res-uuid-1")
    # Fără ID de mesaj, cheia de deduplicare e stabilă pentru același conținut.
    assert parsed_flat.external_message_id.startswith("sha1:")
    assert adapter.parse_webhook(flat).external_message_id == parsed_flat.external_message_id

    assert adapter.parse_webhook({"action": "reservation.created", "data": {"id": "x"}}) is None
    assert adapter.parse_webhook({"unexpected": True}) is None


def test_hospitable_sample_webhook_goes_end_to_end(settings):
    """Payload de exemplu de la un PMS real -> eveniment -> răspuns trimis prin același PMS."""
    api = Recorder(hospitable_api)
    with app_client(settings) as client:
        container = client.app.state.container
        container.pms.register(hospitable(container, api))
        report = client.post("/integrations/hospitable/sync").json()
        assert (report["properties_created"], report["bookings_created"]) == (1, 2)
        greta = next(b for b in client.get("/bookings").json() if b["guest_name"] == "Greta Weber")
        assert (greta["pms_provider"], greta["source_channel"]) == ("hospitable", "airbnb")

        webhook = {
            "action": "message.created",
            "data": {"id": "msg-in-1", "reservation_id": "res-uuid-1", "sender_type": "guest",
                     "body": "What time is check-in?"},
        }  # fmt: skip
        assert client.post("/webhooks/hospitable", json=webhook).json()["status"] == "published"
        assert drain(client)

        (event,) = events_of(client, "GuestMessageReceived")
        assert event["payload"]["booking_id"] == greta["id"] and "uuid" not in str(event["payload"])
        # Răspunsul copilotului a plecat prin adaptorul Hospitable, nu prin cel mock.
        outbound = [r for r in api.requests if r.method == "POST"]
        assert [r.url.path for r in outbound] == ["/v2/reservations/res-uuid-1/messages"]
        reply = json.loads(outbound[0].content)["body"]
        assert "15:00" in reply and "2026-07-01" in reply

        # Hospitable retrimite mesajul nostru ca webhook: filtrul anti-buclă îl oprește.
        echo = {"action": "message.created",
                "data": {"id": "msg-out-1", "reservation_id": "res-uuid-1", "sender_type": "host", "body": reply}}  # fmt: skip
        assert client.post("/webhooks/hospitable", json=echo).json()["status"] == "ignored_not_guest"
        assert client.post("/webhooks/hospitable", json=webhook).json()["status"] == "duplicate"
        assert drain(client) and len(events_of(client, "GuestMessageReceived")) == 1


# =====================================================================================
# OwnerRez
# =====================================================================================


def ownerrez_api(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path == "/v2/properties":
        if params.get("cursor") == "p2":
            return httpx.Response(200, json={"items": [{"id": 2, "name": "Cabin", "max_guests": 6}]})
        return httpx.Response(
            200,
            json={
                "items": [{"id": 1, "name": "Beach House", "bedrooms": 3, "max_guests": 8,
                           "address": {"street1": "1 Ocean Dr", "city": "Miami"}, "currency_code": "USD"}],
                "next_page_url": "https://apistage.ownerrez.com/v2/properties?cursor=p2&limit=100",
            },
        )  # fmt: skip
    if path == "/v2/bookings":
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": 501, "property_id": 1, "type": "booking", "status": "active",
                     "arrival": "2026-07-10", "departure": "2026-07-14", "adults": 2, "children": 1,
                     "listing_site": "Airbnb", "total_amount": 1200.5, "currency_code": "USD",
                     "guest": {"first_name": "John", "last_name": "Smith"}},
                    {"id": 502, "property_id": 1, "type": "block", "arrival": "2026-08-01", "departure": "2026-08-03"},
                    {"id": 503, "property_id": 2, "type": "booking", "status": "canceled",
                     "arrival": "2026-09-01", "departure": "2026-09-05", "listing_site": "VRBO", "guest_id": 77},
                ]
            },
        )  # fmt: skip
    if path == "/v2/bookings/501":
        return httpx.Response(200, json={"id": 501, "thread_ids": [9001]})
    if path == "/v2/bookings/503":
        return httpx.Response(200, json={"id": 503, "thread_ids": []})
    if path in ("/v2/messages", "/v2/spotrates"):
        return httpx.Response(200, json={})
    return httpx.Response(404, json={"messages": ["not found"], "code": "not_found"})


def ownerrez(container, recorder: Recorder, **auth) -> OwnerRezAdapter:
    auth = auth or {"access_token": "at_oauth"}
    return OwnerRezAdapter(
        base_url="https://apistage.ownerrez.com",
        ids=container.ids,
        user_agent="StayPilot Test",
        transport=recorder.transport,
        **auth,
    )


async def test_ownerrez_lists_properties_and_bookings_with_cursor_pagination(container):
    api = Recorder(ownerrez_api)
    adapter = ownerrez(container, api)

    properties = await adapter.list_properties()
    api.requests.clear()
    reservations = await adapter.list_reservations()

    assert [(p.external_id, p.name, p.max_guests) for p in properties] == [
        ("1", "Beach House", 8), ("2", "Cabin", 6),
    ]  # fmt: skip
    assert (properties[0].city, properties[0].currency) == ("Miami", "USD")
    first, second = reservations  # blocarea (type=block) nu e rezervare
    assert (first.external_id, first.property_external_id, first.guest_name) == ("501", "1", "John Smith")
    assert (first.guest_count, first.status, first.source_channel) == (3, "confirmed", "airbnb")
    assert first.total_price == 1200.5
    assert (second.status, second.source_channel, second.guest_name) == ("cancelled", "vrbo", "Guest 77")

    bookings_call = next(r for r in api.requests if r.url.path == "/v2/bookings")
    assert bookings_call.url.params["property_ids"] == "1,2"  # parametru obligatoriu
    assert all(r.headers["authorization"] == "Bearer at_oauth" for r in api.requests)
    assert all(r.headers["user-agent"] == "StayPilot Test" for r in api.requests)  # obligatoriu
    await adapter.aclose()


async def test_ownerrez_messaging_goes_through_the_bookings_thread(container):
    api = Recorder(ownerrez_api)
    adapter = ownerrez(container, api)
    container.ids.link("ownerrez", "reservation", "501", "bk_john")
    container.ids.link("ownerrez", "reservation", "503", "bk_no_thread")

    await adapter.send_message("bk_john", "Welcome!")

    lookup, post = api.requests
    assert (lookup.method, lookup.url.path) == ("GET", "/v2/bookings/501")
    assert (post.method, post.url.path) == ("POST", "/v2/messages")
    assert json.loads(post.content) == {"thread_id": 9001, "body": "Welcome!"}
    with pytest.raises(PmsError, match="no message thread"):
        await adapter.send_message("bk_no_thread", "Hello")
    await adapter.aclose()


async def test_ownerrez_personal_token_uses_basic_auth_and_cannot_message(container):
    api = Recorder(ownerrez_api)
    adapter = ownerrez(container, api, username="me@example.com", personal_token="pt_123")

    await adapter.list_properties()

    assert api.requests[0].headers["authorization"].startswith("Basic ")
    assert adapter.capabilities.send_messages is False  # API-ul răspunde 402 fără OAuth
    with pytest.raises(CapabilityNotSupported):
        await adapter.send_message("bk_any", "Hello")
    with pytest.raises(ValueError):
        OwnerRezAdapter(base_url="https://apistage.ownerrez.com", ids=container.ids)
    await adapter.aclose()


async def test_ownerrez_price_updates_use_spot_rates(container):
    api = Recorder(ownerrez_api)
    adapter = ownerrez(container, api)
    container.ids.link("ownerrez", "property", "1", "prop_beach")

    await adapter.set_price("prop_beach", date(2026, 7, 10), date(2026, 7, 12), 199.5)

    (request,) = api.requests
    assert (request.method, request.url.path) == ("PATCH", "/v2/spotrates")
    assert json.loads(request.content) == [
        {"property_id": 1, "date": "2026-07-10", "amount": 199.5},
        {"property_id": 1, "date": "2026-07-11", "amount": 199.5},
    ]
    with pytest.raises(CapabilityNotSupported):
        await adapter.set_availability("prop_beach", date(2026, 7, 10), date(2026, 7, 12), True)
    await adapter.aclose()


def test_ownerrez_webhook_parsing(container):
    adapter = ownerrez(container, Recorder(ownerrez_api))
    payload = {
        "id": "7e5c-guid",
        "user_id": 42,
        "action": "entity_create",
        "entity_type": "thread_message",
        "entity_id": 3001,
        "entity": {"body": "Can we bring a dog?", "from_role": "Guest", "date_utc": "2026-06-01T10:00:00Z",
                   "thread": {"booking_id": 501}},
    }  # fmt: skip
    parsed = adapter.parse_webhook(payload)
    assert (parsed.external_message_id, parsed.external_reservation_id) == ("3001", "501")
    assert (parsed.sender_type, parsed.body) == ("guest", "Can we bring a dog?")

    host_message = {**payload, "entity": {**payload["entity"], "from_role": "Owner"}}
    assert adapter.parse_webhook(host_message).sender_type == "owner"  # F1 îl va ignora
    assert adapter.parse_webhook({**payload, "entity_type": "booking"}) is None
    assert adapter.parse_webhook({**payload, "action": "entity_update"}) is None
    assert adapter.parse_webhook({**payload, "entity": {**payload["entity"], "is_draft": True}}) is None


@pytest.mark.parametrize("entity_type", ["booking", "reservation"])
@pytest.mark.parametrize("action", ["entity_create", "entity_update", "entity_delete"])
async def test_ownerrez_recognizes_only_supported_reservation_webhooks(container, entity_type, action):
    adapter = ownerrez(container, Recorder(ownerrez_api))
    personal = ownerrez(container, Recorder(ownerrez_api), username="host", personal_token="pt_test")
    try:
        webhook = {"entity_type": entity_type, "action": action}
        assert adapter.is_reservation_webhook(webhook)
        assert adapter.parse_webhook(webhook) is None
        assert not personal.is_reservation_webhook(webhook)
        assert not adapter.is_reservation_webhook({**webhook, "action": "unexpected"})
        assert not adapter.is_reservation_webhook({**webhook, "entity_type": "thread_message"})
        assert not adapter.is_reservation_webhook({})
    finally:
        await adapter.aclose()
        await personal.aclose()


@pytest.mark.parametrize("key", ["action", "event", "type"])
@pytest.mark.parametrize("event", ["reservation.created", "reservation.updated", "reservation.cancelled", "booking.cancelled"])
async def test_hospitable_recognizes_reservation_webhook_names(container, key, event):
    adapter = hospitable(container, Recorder(hospitable_api))
    try:
        assert adapter.is_reservation_webhook({key: event})
        assert not adapter.is_reservation_webhook({key: "message.created"})
        assert not adapter.is_reservation_webhook({key: None})
        assert not adapter.is_reservation_webhook({key: {"type": event}})
        assert not adapter.is_reservation_webhook({key: "reservation"})
        assert not adapter.is_reservation_webhook({})
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_combines_bookings_blocks_and_sparse_paginated_spot_rates(container):
    container.ids.link("ownerrez", "property", "1", "prop_beach")
    entries = [
        {"property_id": 1, "type": "booking", "arrival": "2026-07-08", "departure": "2026-07-11", "status": "active"},
        {"property_id": 1, "type": "block", "arrival": "2026-07-12", "departure": "2026-07-14"},
        {"property_id": 1, "type": "booking", "arrival": "2026-07-11", "departure": "2026-07-12", "status": "canceled"},
        {"property_id": 1, "type": "block", "arrival": "2026-07-11", "departure": "2026-07-12", "status": "cancelled"},
        {"property_id": 2, "type": "booking", "arrival": "2026-07-10", "departure": "2026-07-15"},
        {"property_id": 1, "type": "booking", "arrival": "2026-07-09", "departure": "2026-07-10"},
        {"property_id": 1, "type": "booking", "arrival": "2026-07-15", "departure": "2026-07-16"},
    ]

    def respond(request):
        if request.url.path == "/v2/properties":
            return httpx.Response(200, json={"items": [{"id": 1, "base_price": 100, "currency_code": "USD"}]})
        if request.url.path == "/v2/bookings":
            if request.url.params.get("cursor") == "next":
                return httpx.Response(200, json={"items": entries[1:]})
            return httpx.Response(200, json={"items": entries[:1], "next_page_url": "https://apistage.ownerrez.com/v2/bookings?cursor=next"})
        if request.url.path == "/v2/spotrates":
            if request.url.params.get("cursor") == "next":
                return httpx.Response(200, json={"items": [{"property_id": 1, "date": "2026-07-14", "amount": 0}]})
            return httpx.Response(200, json={"items": [
                {"property_id": 1, "date": "2026-07-10", "amount": "149.5"},
                {"property_id": 1, "date": "2026-07-11", "amount": -50},
                {"property_id": 1, "date": "2026-07-12", "amount": "NaN"},
                {"property_id": 1, "date": "2026-07-13", "amount": "Infinity"},
                {"property_id": 2, "date": "2026-07-11", "amount": 999},
                {"property_id": 1, "date": "2026-07-15", "amount": 888},
                {"property_id": 1, "date": "bad-date", "amount": 777},
            ], "next_page_url": "https://apistage.ownerrez.com/v2/spotrates?cursor=next"})
        raise AssertionError(f"Unexpected request: {request.url}")

    api = Recorder(respond)
    adapter = ownerrez(container, api)
    try:
        days = await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 15))
        assert adapter.capabilities.read_calendar is True
        assert [day.day for day in days] == [date(2026, 7, n) for n in range(10, 15)]
        assert [day.available for day in days] == [False, True, False, False, True]
        assert [day.price for day in days] == [149.5, 100, 100, 100, 0]
        assert {(day.property_id, day.currency) for day in days} == {("prop_beach", "USD")}
        for path in ("/v2/bookings", "/v2/spotrates"):
            first_request = next(request for request in api.requests if request.url.path == path)
            assert first_request.url.params["property_ids"] == "1"
            assert first_request.url.params["from"] == "2026-07-10"
            assert first_request.url.params["to"] == "2026-07-14"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("base_price,expected", [(87.5, 87.5), (None, 0), ("invalid", 0), (-1, 0), ("NaN", 0)])
async def test_ownerrez_calendar_uses_safe_property_fallback_when_rates_unavailable(container, base_price, expected):
    container.ids.link("ownerrez", "property", "1", "prop_beach")

    def respond(request):
        if request.url.path == "/v2/bookings":
            return httpx.Response(200, json={"items": [], "limit": 100})
        if request.url.path == "/v2/properties":
            return httpx.Response(200, json={"items": [{"id": 1, "base_price": base_price, "currency_code": "USD"}]})
        if request.url.path == "/v2/spotrates":
            return httpx.Response(404, json={"error": "unsupported in this environment"})
        raise AssertionError(request.url)

    adapter = ownerrez(container, Recorder(respond))
    try:
        (day,) = await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
        assert (day.available, day.price, day.currency) == (True, expected, "USD")
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_rejects_a_bookings_response_without_items(container):
    """Un 200 fără lista de rezervări nu are voie să arate nopți libere (audit C05)."""
    container.ids.link("ownerrez", "property", "1", "prop_beach")

    def respond(request):
        if request.url.path == "/v2/bookings":
            return httpx.Response(200, json={"unexpected": "bookings data is absent"})
        return httpx.Response(200, json={"items": []})

    adapter = ownerrez(container, Recorder(respond))
    try:
        with pytest.raises(PmsError, match="no item list"):
            await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_keeps_availability_when_all_optional_prices_fail(container):
    container.ids.link("ownerrez", "property", "1", "prop_beach")

    def respond(request):
        if request.url.path == "/v2/bookings":
            return httpx.Response(200, json={"items": [{"property_id": 1, "type": "block", "arrival": "2026-07-10", "departure": "2026-07-11"}]})
        return httpx.Response(503, json={"error": "unavailable"})

    adapter = ownerrez(container, Recorder(respond))
    try:
        (day,) = await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
        assert (day.available, day.price) == (False, 0)
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_preserves_cached_currency_and_base_price_during_metadata_failure(container):
    container.ids.link("ownerrez", "property", "1", "prop_beach")
    metadata_available = True

    def respond(request):
        if request.url.path == "/v2/properties":
            if metadata_available:
                return httpx.Response(200, json={"items": [{"id": 1, "base_price": 87.5, "currency_code": "USD"}]})
            return httpx.Response(503, text="property metadata temporarily unavailable")
        if request.url.path == "/v2/bookings":
            return httpx.Response(200, json={"items": []})
        if request.url.path == "/v2/spotrates":
            return httpx.Response(200, json={"items": [{"property_id": 1, "date": "2026-07-10", "amount": 149}]})
        raise AssertionError(request.url)

    adapter = ownerrez(container, Recorder(respond))
    try:
        await adapter.list_properties()
        metadata_available = False
        days = await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 12))
        assert [(day.price, day.currency) for day in days] == [(149, "USD"), (87.5, "USD")]
        assert all(day.available for day in days)
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_validates_mapping_range_and_availability_failures(container):
    api = Recorder(lambda request: httpx.Response(503, text="unavailable"))
    adapter = ownerrez(container, api)
    try:
        with pytest.raises(UnmappedEntity):
            await adapter.get_days("unknown", date(2026, 7, 10), date(2026, 7, 11))
        container.ids.link("ownerrez", "property", "1", "prop_beach")
        assert await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 10)) == []
        with pytest.raises(ValueError, match="end"):
            await adapter.get_days("prop_beach", date(2026, 7, 11), date(2026, 7, 10))
        assert api.requests == []
        with pytest.raises(PmsError, match="HTTP 503"):
            await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("entry", [
    {"arrival": "invalid", "departure": "2026-07-11"},
    {"arrival": "2026-07-10"},
    {"arrival": "2026-07-12", "departure": "2026-07-11"},
])
async def test_ownerrez_calendar_never_reports_free_when_occupancy_is_malformed(container, entry):
    container.ids.link("ownerrez", "property", "1", "prop_beach")
    adapter = ownerrez(container, Recorder(lambda request: httpx.Response(200, json={"items": [entry]})))
    try:
        with pytest.raises(PmsError, match="invalid dates"):
            await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("entry_type", ["booking", "block", "owner", "quote_hold", "linked_availability"])
async def test_ownerrez_calendar_holds_every_supported_occupying_type_through_end(container, entry_type):
    container.ids.link("ownerrez", "property", "1", "prop_beach")

    def respond(request):
        if request.url.path == "/v2/bookings":
            return httpx.Response(200, json={"items": [{"property_id": 1, "type": entry_type,
                "arrival": "2026-07-10", "departure": "2026-07-20", "status": "pending"}]})
        return httpx.Response(200, json={})

    adapter = ownerrez(container, Recorder(respond))
    try:
        days = await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 13))
        assert [day.available for day in days] == [False, False, False]
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_rejects_truncated_booking_pagination(container, monkeypatch):
    monkeypatch.setattr("app.adapters.ownerrez.adapter.MAX_PAGES", 2)
    container.ids.link("ownerrez", "property", "1", "prop_beach")
    api = Recorder(lambda request: httpx.Response(200, json={"items": [],
        "next_page_url": "https://apistage.ownerrez.com/v2/bookings?cursor=more"}))
    adapter = ownerrez(container, api)
    try:
        with pytest.raises(PmsError, match="pagination exceeded"):
            await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
        assert len(api.requests) == 2
    finally:
        await adapter.aclose()


async def test_ownerrez_calendar_wraps_transport_timeouts_as_pms_errors(container):
    container.ids.link("ownerrez", "property", "1", "prop_beach")

    def timeout(request):
        raise httpx.ReadTimeout("calendar read timed out", request=request)

    adapter = ownerrez(container, Recorder(timeout))
    try:
        with pytest.raises(PmsError, match="timed out"):
            await adapter.get_days("prop_beach", date(2026, 7, 10), date(2026, 7, 11))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("available", [True, False])
async def test_ownerrez_availability_remains_explicitly_disabled_without_symmetric_api(container, available):
    api = Recorder(ownerrez_api)
    adapter = ownerrez(container, api)
    try:
        assert adapter.capabilities.write_availability is False
        with pytest.raises(CapabilityNotSupported, match="blocks cannot be cancelled or deleted"):
            await adapter.set_availability("prop_beach", date(2026, 7, 10), date(2026, 7, 11), available)
        assert api.requests == []
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("provider", ["ownerrez", "hospitable"])
def test_real_reservation_cancellation_webhook_syncs_and_publishes_once(settings, provider):
    cancelled = False

    def respond(request):
        response = ownerrez_api(request) if provider == "ownerrez" else hospitable_api(request)
        if request.url.path in ("/v2/bookings", "/v2/reservations"):
            body = response.json()
            if cancelled:
                if provider == "ownerrez":
                    body["items"][0]["status"] = "canceled"
                else:
                    body["data"][0]["reservation_status"]["current"]["category"] = "cancelled"
            return httpx.Response(response.status_code, json=body)
        return response

    with app_client(settings) as client:
        container = client.app.state.container
        api = Recorder(respond)
        adapter = ownerrez(container, api) if provider == "ownerrez" else hospitable(container, api)
        container.pms.register(adapter)
        assert client.post(f"/integrations/{provider}/sync").status_code == 200
        cancelled = True
        webhook = (
            {"entity_type": "booking", "action": "entity_update", "entity_id": 501}
            if provider == "ownerrez" else {"event": "reservation.cancelled", "data": {"id": "res-uuid-1"}}
        )
        for _ in range(3):
            response = client.post(f"/webhooks/{provider}", json=webhook)
            assert response.status_code == 200 and response.json()["status"] == "synced"
        assert drain(client)
        (event,) = events_of(client, "BookingCancelled")
        assert event["source"] == f"sync:{provider}"
        assert event["payload"]["pms_provider"] == provider
        assert "uuid" not in str(event["payload"])
        internal_booking = container.world.get_booking(event["payload"]["booking_id"])
        assert internal_booking.status == "cancelled"


@pytest.mark.parametrize("provider", ["ownerrez", "hospitable"])
def test_real_unmapped_message_is_automatically_replayed_after_sync(settings, provider):
    with app_client(settings) as client:
        container = client.app.state.container
        api = Recorder(ownerrez_api if provider == "ownerrez" else hospitable_api)
        adapter = ownerrez(container, api) if provider == "ownerrez" else hospitable(container, api)
        container.pms.register(adapter)
        webhook = (
            {"entity_type": "thread_message", "action": "entity_create", "entity_id": "real-unmapped-1",
             "entity": {"body": "What time is check-in?", "from_role": "Guest", "thread": {"booking_id": 501}}}
            if provider == "ownerrez" else
            {"action": "message.created", "data": {"id": "real-unmapped-1", "reservation_id": "res-uuid-1",
                                                    "sender_type": "guest", "body": "What time is check-in?"}}
        )
        assert client.post(f"/webhooks/{provider}", json=webhook).json()["status"] == "unmapped"
        assert client.post(f"/integrations/{provider}/sync").status_code == 200
        assert drain(client)
        (event,) = events_of(client, "GuestMessageReceived")
        assert event["source"] == f"webhook:{provider}"
        assert "uuid" not in str(event["payload"])
        original = client.get("/f1/webhook-log").json()[0]
        assert original["outcome"] == "published" and original["event_id"] == event["id"]
        assert original["detail"] == "reprocessed after sync"
        assert client.post(f"/integrations/{provider}/sync").status_code == 200
        assert client.post(f"/webhooks/{provider}", json=webhook).json()["status"] == "duplicate"
        assert drain(client) and len(events_of(client, "GuestMessageReceived")) == 1


# =====================================================================================
# OpenRouter
# =====================================================================================

REASONING = [{"type": "reasoning.text", "text": "Need the booking first.", "signature": "sig", "id": "r1",
              "format": "anthropic-claude-v1", "index": 0}]  # fmt: skip


def completion(message: dict, **usage) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor/strong-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, **usage},
        },
    )


def lookup_call(call_id: str = "call_1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "lookup", "arguments": '{"key": "a", "reason": "need it"}'}}  # fmt: skip


def openrouter(recorder: Recorder, **kwargs) -> OpenRouterLLM:
    return OpenRouterLLM(
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
        model="vendor/strong-model",
        http_client=httpx.AsyncClient(transport=recorder.transport),
        **kwargs,
    )


LOOKUP_SPEC = ToolSpec(name="lookup", description="Look up a value.",
                       parameters={"type": "object", "properties": {"key": {"type": "string"}}})  # fmt: skip


async def test_openrouter_request_and_response_mapping():
    api = Recorder(
        lambda request: completion(
            {"content": None, "tool_calls": [lookup_call()], "reasoning_details": REASONING}, cost=0.0042
        )
    )
    llm = openrouter(api)
    history = [
        ChatMessage(role="system", content="Objective."),
        ChatMessage(role="user", content="Go."),
        ChatMessage(role="assistant", reasoning_details=REASONING,
                    tool_calls=[ToolCall(id="call_0", name="lookup", arguments='{"key": "z", "reason": "r"}')]),
        ChatMessage(role="tool", tool_call_id="call_0", name="lookup", content='{"value": 1}'),
    ]  # fmt: skip

    response = await llm.chat(ChatRequest(purpose="test", messages=history, tools=[LOOKUP_SPEC]))

    (request,) = api.requests
    assert request.url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(request.content)
    assert body["model"] == "vendor/strong-model"
    assert body["tools"] == [{"type": "function", "function": LOOKUP_SPEC.model_dump()}]
    assistant, tool = body["messages"][2], body["messages"][3]
    assert assistant["reasoning_details"] == REASONING  # retrimis nemodificat
    assert assistant["tool_calls"][0] == {
        "id": "call_0", "type": "function",
        "function": {"name": "lookup", "arguments": '{"key": "z", "reason": "r"}'},
    }  # fmt: skip
    assert tool == {"role": "tool", "tool_call_id": "call_0", "content": '{"value": 1}'}

    assert response.message.tool_calls == [
        ToolCall(id="call_1", name="lookup", arguments='{"key": "a", "reason": "need it"}')
    ]
    assert response.message.reasoning_details == REASONING
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (120, 30)
    assert response.usage.cost == pytest.approx(0.0042)
    assert response.model == "vendor/strong-model"
    await llm.aclose()


async def test_openrouter_utility_model_and_errors():
    api = Recorder(lambda request: completion({"content": "summary"}))
    llm = openrouter(api, utility_model="vendor/cheap-model")
    messages = [ChatMessage(role="user", content="Summarize.")]
    await llm.chat(ChatRequest(purpose="t", messages=messages, model_role="utility"))
    await llm.chat(ChatRequest(purpose="t", messages=messages))
    assert [json.loads(r.content)["model"] for r in api.requests] == ["vendor/cheap-model", "vendor/strong-model"]
    assert "tools" not in json.loads(api.requests[0].content)
    await llm.aclose()

    failing = openrouter(Recorder(lambda r: httpx.Response(401, json={"error": {"code": 401, "message": "No auth"}})))
    with pytest.raises(LLMError, match="OpenRouter call failed"):
        await failing.chat(ChatRequest(purpose="t", messages=messages))
    await failing.aclose()

    empty = openrouter(Recorder(lambda r: httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": [],
        "error": {"code": 502, "message": "upstream down"}})))  # fmt: skip
    with pytest.raises(LLMError, match="no choices"):
        await empty.chat(ChatRequest(purpose="t", messages=messages))
    await empty.aclose()


async def test_agent_loop_over_openrouter_preserves_reasoning_between_rounds(container):
    """Bucla noastră + adaptorul real: runda a doua retrimite reasoning-ul primei runde."""
    finish_call = {"id": "call_2", "type": "function", "function": {
        "name": "finish", "arguments": '{"result": "a is 42", "evidence": ["value 42"], "reason": "done"}'}}  # fmt: skip
    replies = iter([
        completion({"content": None, "tool_calls": [lookup_call()], "reasoning_details": REASONING}, cost=0.01),
        completion({"content": None, "tool_calls": [finish_call]}, cost=0.02),
    ])  # fmt: skip
    api = Recorder(lambda request: next(replies))
    llm = openrouter(api)

    class LookupArgs(ToolArgs):
        key: str

    async def lookup(args: LookupArgs, run: RunContext) -> dict:
        return {"value": 42}

    container.agent._llm = llm
    spec = AgentSpec(name="test.agent", feature="test", system_prompt="Objective.",
                     tools=[Tool("lookup", "Look up.", LookupArgs, lookup)])  # fmt: skip
    result = await container.agent.run(spec, objective="Find a.")

    assert (result.status, result.result) == ("finished", "a is 42")
    assert result.usage.cost == pytest.approx(0.03)
    second = json.loads(api.requests[1].content)["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[2]["reasoning_details"] == REASONING
    assert second[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"value": 42}'}
    assert [t["function"]["name"] for t in json.loads(api.requests[1].content)["tools"]] == ["lookup", "finish"]
    await llm.aclose()


# =====================================================================================
# Apify
# =====================================================================================

APIFY_ITEMS = [
    {"id": "r1", "rating": 4, "text": "Great flat, slow wifi.", "createdAt": "2026-05-20T10:00:00.000Z",
     "reviewer": {"firstName": "Lena"}},
    {"stars": 5, "comments": "Perfect.", "date": "2026-04-02", "author": "Tom"},
    {"rating": 0, "text": "invalid rating"},
    {"text": "no rating"},
    "not an object",
]  # fmt: skip


def apify(container, recorder: Recorder, template: str = '{"startUrls": [{"url": "{listing_url}"}], "maxReviews": 50}'):
    return ApifyReviewsAdapter(token="apify-token", actor_id="someone~airbnb-reviews", input_template=template,
                               ids=container.ids, timeout_s=30, transport=recorder.transport)  # fmt: skip


async def test_apify_runs_the_configured_actor_and_normalizes_reviews(container):
    api = Recorder(lambda request: httpx.Response(201, json=APIFY_ITEMS))
    adapter = apify(container, api)
    container.ids.link("airbnb", "listing", "https://www.airbnb.com/rooms/123", "prop_demo")

    batch = await adapter.fetch_reviews("prop_demo")

    (request,) = api.requests
    assert request.url.path == "/v2/acts/someone~airbnb-reviews/run-sync-get-dataset-items"
    assert request.url.params["timeout"] == "30"
    assert request.headers["authorization"] == "Bearer apify-token" and "token" not in request.url.params
    assert json.loads(request.content) == {
        "startUrls": [{"url": "https://www.airbnb.com/rooms/123"}], "maxReviews": 50,
    }  # fmt: skip
    assert batch.source == "apify" and len(batch.reviews) == 2  # elementele invalide sunt sărite
    first, second = batch.reviews
    assert (first.rating, first.author, first.reviewed_on) == (4, "Lena", date(2026, 5, 20))
    assert (second.rating, second.text, second.reviewed_on) == (5, "Perfect.", date(2026, 4, 2))
    assert all(r.property_id == "prop_demo" for r in batch.reviews)
    await adapter.aclose()


async def test_apify_failures_become_reviews_errors(container):
    adapter = apify(container, Recorder(lambda request: httpx.Response(402, text="out of credit")))
    with pytest.raises(ReviewsError, match="no listing URL mapped"):
        await adapter.fetch_reviews("prop_unmapped")
    container.ids.link("airbnb", "listing", "https://www.airbnb.com/rooms/1", "prop_demo")
    with pytest.raises(ReviewsError, match="HTTP 402"):
        await adapter.fetch_reviews("prop_demo")
    await adapter.aclose()

    with pytest.raises(ValueError, match="not valid JSON"):
        apify(container, Recorder(lambda r: httpx.Response(200, json=[])), template="{broken")
    assert render_input({"a": ["{listing_url}", 1], "b": {"c": "x{listing_url}"}}, "U") == {
        "a": ["U", 1], "b": {"c": "xU"},
    }  # fmt: skip
    assert normalize_review({"rating": 3, "text": "ok"}, "p").id == normalize_review({"rating": 3, "text": "ok"}, "p").id


# =====================================================================================
# Inside Airbnb
# =====================================================================================

LISTINGS_CSV = (
    "id,listing_url,name,accommodates,bedrooms,amenities,price\n"
    '101,https://www.airbnb.com/rooms/101,Central Studio,2,1,'
    '"[""Wifi"", ""Kitchen"", ""Air conditioning"", ""Washer"", ""Self check-in""]","$68.00"\n'
    '102,https://www.airbnb.com/rooms/102,Bare Room,2,1,"[""Wifi""]",$45.00\n'
    '103,https://www.airbnb.com/rooms/103,Big Loft,6,3,"[""Wifi"", ""Free parking on premises""]","$1,250.00"\n'
    '104,https://www.airbnb.com/rooms/104,No Price,2,1,"[""Wifi""]",\n'
)


def test_inside_airbnb_normalization_helpers():
    assert parse_price("$1,250.00") == 1250.0 and parse_price("") is None
    assert normalize_amenities('["Wifi", "Free parking on premises", "Patio or balcony", "Hot tub"]') == [
        "balcony", "hot_tub", "parking", "wifi",
    ]  # fmt: skip


@pytest.mark.parametrize("compressed", [False, True])
async def test_inside_airbnb_is_selected_from_config_and_feeds_relisting(tmp_path, compressed):
    csv_file = tmp_path / ("listings.csv.gz" if compressed else "listings.csv")
    csv_file.write_bytes(gzip.compress(LISTINGS_CSV.encode()) if compressed else LISTINGS_CSV.encode())
    settings = make_settings(
        tmp_path, market_provider="inside_airbnb", inside_airbnb_listings_csv=csv_file, market_city="Vienna"
    )
    container = Container(settings)  # doar config: nicio linie de feature nu se schimbă
    await container.startup()
    try:
        assert isinstance(container.market, InsideAirbnbMarketAdapter)
        assert "CC BY 4.0" in container.market.attribution  # creditul sursei, afișat în UI
        studio = next(p for p in container.world.list_properties() if p.name == "Old Town Studio")
        query = ComparableQuery(city=studio.city, bedrooms=1, max_guests=2, amenities=studio.amenities,
                                check_in=date(2026, 6, 3), check_out=date(2026, 6, 6))  # fmt: skip

        candidates = await container.market.find_comparables(query)

        assert [(c.ref, c.nightly_price) for c in candidates] == [("102", 41.4), ("101", 62.56)]
        assert all((c.city, c.source) == ("Vienna", "inside_airbnb") for c in candidates)
        best = cheapest_comparable(candidates, studio)
        assert (best.title, best.nightly_price) == ("Central Studio", 62.56)  # preț USD convertit; cel ieftin nu are facilitățile
        assert best.url == "https://www.airbnb.com/rooms/101"

        await container.reset_world()  # resetul lumii nu pierde datele de piață
        assert len(await container.market.find_comparables(query)) == 2
    finally:
        await container.shutdown()


def test_real_pms_is_selected_from_config_alongside_the_mock(tmp_path):
    settings = make_settings(tmp_path, pms_provider="ownerrez", ownerrez_access_token="at_x")
    with app_client(settings) as client:
        connections = {c["provider"]: c for c in client.get("/integrations").json()}
        assert set(connections) == {"mock", "ownerrez"}
        assert connections["ownerrez"]["selected"] is True
        assert connections["ownerrez"]["capabilities"]["write_availability"] is False
        assert len(client.get("/bookings").json()) == 7  # lumea simulată rămâne disponibilă
    assert Property(id="p", name="n").pms_provider == "mock"
