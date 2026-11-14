"""Garanțiile fazei 1, pe porturi simulate și fără rețea."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.contracts.events import ApprovalDecided, BookingCancelled
from app.core.contracts.models import CalendarDay
from app.features.f5_relisting.pricing import (
    cap_price, distribute_prices, floor_price, validate_calendar,
)
from app.features.f5_relisting.relisting import F5Settings, Relisting
from app.features.f5_relisting.tests.test_relisting import FakeMarket, calendar, cancel, comparable
from app.ports.pms import CapabilityNotSupported
from conftest import app_client, approve, drain, events_of


def night(price=70, offset=0, **updates):
    data = dict(property_id="p", day=date(2026, 6, 3) + timedelta(days=offset),
                available=True, price=price, currency="EUR")
    return CalendarDay(**(data | updates))


@pytest.mark.parametrize("market, expected", [
    (68, [65.96, 75.85]), (40, [52.5, 60.38]), (500, [70, 80.5]),
])
def test_proportional_prices_have_a_floor_and_cap_for_every_night(market, expected):
    days = [night(), night(80.5, 1)]
    before = [d.model_dump() for d in days]
    result = distribute_prices(days, 70, market, 3, 25)
    assert [n.proposed_price for n in result] == expected
    assert all(n.floor_price <= n.proposed_price <= n.original_price for n in result)
    assert result[1].proposed_price > result[0].proposed_price
    assert [d.model_dump() for d in days] == before
    assert cap_price(120, 70) == 70
    assert floor_price(80.5, 25) == 60.38


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_regular_prices_are_rejected(value):
    with pytest.raises(ValueError):
        floor_price(value, 25)


@pytest.mark.parametrize("discount", [-1, 101, float("nan"), float("inf")])
def test_invalid_host_discount_is_rejected(discount):
    with pytest.raises(ValueError):
        floor_price(70, discount)


@pytest.mark.parametrize("values", [
    {"undercut_pct": -1}, {"undercut_pct": 100}, {"market_timeout_s": 0},
    {"calendar_timeout_s": float("nan")}, {"last_minute_window_hours": 0},
    {"last_minute_window_hours": 8761},
])
def test_invalid_feature_settings_fail_fast(values):
    with pytest.raises(ValidationError):
        F5Settings(_env_file=None, **values)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "currency", "property", "zero", "fraction"])
def test_calendar_snapshot_is_complete_and_valid(mutation):
    days = [night(), night(80.5, 1)]
    if mutation == "missing":
        days.pop()
    elif mutation == "duplicate":
        days[1] = days[0]
    else:
        field, value = {"currency": ("currency", "USD"), "property": ("property_id", "other"),
                        "zero": ("price", 0), "fraction": ("price", 70.001)}[mutation]
        days[1] = days[1].model_copy(update={field: value})
    with pytest.raises(ValueError):
        validate_calendar(days, "p", date(2026, 6, 3), date(2026, 6, 5), "EUR")


def test_invalid_market_prices_never_become_the_cheapest(settings):
    with app_client(settings, market=FakeMarket([
        comparable(0), comparable(-1), comparable(float("nan")), comparable(float("inf")), comparable(68),
    ])) as client:
        cancel(client, "Emma Johnson")
        assert client.get("/f5/proposals").json()[0]["comparable_price"] == 68


def test_expensive_market_never_increases_a_calendar_price(settings):
    with app_client(settings, market=FakeMarket([comparable(500)])) as client:
        booking = cancel(client, "Emma Johnson")
        before = calendar(client, booking)
        proposal = client.get("/f5/proposals").json()[0]
        approve(client, proposal["approval_id"])
        assert calendar(client, booking) == before
        assert [n["proposed_price"] for n in proposal["nightly_prices"]] == [70, 70, 80.5]


@pytest.mark.parametrize("change", ["booked", "price"])
def test_calendar_conflicts_allow_no_writes(client, monkeypatch, change):
    booking = cancel(client, "Emma Johnson")
    gateway = client.app.state.container.calendar
    start = date.fromisoformat(booking["check_in"]) + timedelta(days=1)
    if change == "booked":
        client.portal.call(gateway.set_availability, booking["property_id"], start, start + timedelta(days=1), False)
    else:
        client.portal.call(gateway.set_price, booking["property_id"], start, start + timedelta(days=1), 60)
    before = calendar(client, booking)
    prices, availability = AsyncMock(), AsyncMock()
    monkeypatch.setattr(gateway, "set_price", prices)
    monkeypatch.setattr(gateway, "set_availability", availability)
    approve(client, client.get("/f5/proposals").json()[0]["approval_id"])
    assert client.get("/f5/proposals").json()[0]["status"] == "conflict"
    assert calendar(client, booking) == before
    prices.assert_not_called()
    availability.assert_not_called()
    assert client.get("/notifications").json()[0]["level"] == "warning"


@pytest.mark.parametrize("failure", ["exception", "timeout", "missing", "currency"])
def test_calendar_verification_fails_closed(client, monkeypatch, failure):
    cancel(client, "Emma Johnson")
    gateway = client.app.state.container.calendar
    original = gateway.get_days

    async def broken(*args):
        if failure == "exception":
            raise CapabilityNotSupported("read unavailable")
        if failure == "timeout":
            raise TimeoutError("read timed out")
        days = await original(*args)
        return days[:-1] if failure == "missing" else [d.model_copy(update={"currency": "USD"}) for d in days]

    monkeypatch.setattr(gateway, "get_days", broken)
    writes = AsyncMock()
    monkeypatch.setattr(gateway, "set_price", writes)
    approve(client, client.get("/f5/proposals").json()[0]["approval_id"])
    assert client.get("/f5/proposals").json()[0]["status"] == "failed"
    writes.assert_not_called()
    assert "No changes were made" in client.get("/notifications").json()[0]["body"]
    assert events_of(client, "ApprovalDecided")[0]["handlers"][0]["status"] == "ok"


@pytest.mark.parametrize("at_check_in", [False, True])
def test_expiration_uses_host_check_in_time_and_is_idempotent(client, monkeypatch, at_check_in):
    booking = cancel(client, "Emma Johnson")
    c = client.app.state.container
    proposal = client.get("/f5/proposals").json()[0]
    check_in = date.fromisoformat(booking["check_in"])
    monkeypatch.setattr(c.clock, "today", lambda: check_in)
    # București este UTC+3 în iunie; expiră exact la 15:00 local.
    instant = datetime(2026, 6, 3, 12 if at_check_in else 11, 0 if at_check_in else 59, tzinfo=UTC)
    monkeypatch.setattr(c.clock, "now", lambda: instant)
    expected = "expired" if at_check_in else "awaiting_approval"
    assert client.get("/f5/proposals").json()[0]["status"] == expected
    count = len(client.get("/notifications").json())
    assert client.get("/f5/proposals").json()[0]["status"] == expected
    assert len(client.get("/notifications").json()) == count
    before = calendar(client, booking)
    approve(client, proposal["approval_id"])
    if at_check_in:
        assert calendar(client, booking) == before
        assert client.get("/f5/proposals").json()[0]["status"] == "expired"
    else:
        assert calendar(client, booking) == [(True, 65.96), (True, 65.96), (True, 75.85)]


def test_late_decision_expires_without_a_prior_get(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    proposal = client.get("/f5/proposals").json()[0]
    monkeypatch.setattr(client.app.state.container.clock, "today", lambda: date(2026, 6, 4))
    before = calendar(client, booking)
    approve(client, proposal["approval_id"])
    assert calendar(client, booking) == before
    assert client.get("/f5/proposals").json()[0]["status"] == "expired"


def test_duplicate_cancellations_and_decisions_have_one_effect(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    c = client.app.state.container
    event = BookingCancelled(**{k: booking[k] for k in (
        "property_id", "check_in", "check_out", "pms_provider", "source_channel"
    )}, booking_id=booking["id"])

    async def repeat():
        await asyncio.gather(c.bus.publish(event), c.bus.publish(event.model_copy(update={"event_id": event.event_id + "-retry"})))
        await c.bus.join(10)

    client.portal.call(repeat)
    (proposal,) = client.get("/f5/proposals").json()
    assert len(client.get("/approvals").json()) == 1
    write = AsyncMock(wraps=c.calendar.set_price)
    monkeypatch.setattr(c.calendar, "set_price", write)
    approve(client, proposal["approval_id"])
    approve(client, proposal["approval_id"])
    decision = ApprovalDecided(approval_id=proposal["approval_id"], feature="f5_relisting",
                              kind="relist_price", decision="approved", payload={"proposal_id": proposal["id"]})
    client.portal.call(c.bus.publish, decision)
    assert drain(client)
    assert write.await_count == 3
    assert client.get("/f5/proposals").json()[0]["status"] == "applied"


def test_write_failure_reports_partial_progress_and_does_not_retry(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    c = client.app.state.container
    original = c.calendar.set_price
    calls = []

    async def write(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("PMS offline")
        await original(*args)

    monkeypatch.setattr(c.calendar, "set_price", write)
    proposal = client.get("/f5/proposals").json()[0]
    approve(client, proposal["approval_id"])
    approve(client, proposal["approval_id"])
    assert len(calls) == 2
    assert calendar(client, booking) == [(True, 65.96), (True, 70), (True, 80.5)]
    assert client.get("/f5/proposals").json()[0]["status"] == "failed"
    body = client.get("/notifications").json()[0]["body"]
    assert "Confirmed updated nights: 2026-06-03" in body and "PMS offline" in body


def test_feature_migrates_its_own_legacy_table_without_losing_data(client):
    c = client.app.state.container
    cancel(client, "Emma Johnson")
    c.db.execute("ALTER TABLE f5_relist_proposals DROP COLUMN nightly_prices")
    c.db.execute("ALTER TABLE f5_relist_proposals DROP COLUMN expires_at")
    service = Relisting(c._make_feature_context("f5_relisting"))
    (proposal,) = service.list()
    assert proposal.nightly_prices == []
    assert len(c.world.list_bookings()) == 7
    approve(client, proposal.approval_id)
    assert service.get(proposal.id).status == "failed"


@pytest.mark.parametrize("limit", [0, -1, 201])
def test_proposal_endpoint_rejects_invalid_limits(client, limit):
    assert client.get("/f5/proposals", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("stage", ["proposal", "approval"])
def test_real_calendar_timeout_is_caught_locally(settings, monkeypatch, stage):
    monkeypatch.setenv("F5_CALENDAR_TIMEOUT_S", "0.02")
    with app_client(settings) as client:
        c = client.app.state.container
        if stage == "approval":
            cancel(client, "Emma Johnson")
        async def hang(*args):
            await asyncio.sleep(60)
        monkeypatch.setattr(c.calendar, "get_days", hang)
        writes = AsyncMock()
        monkeypatch.setattr(c.calendar, "set_price", writes)
        if stage == "proposal":
            cancel(client, "Emma Johnson")
        else:
            approve(client, client.get("/f5/proposals").json()[0]["approval_id"])
        assert client.get("/f5/proposals").json()[0]["status"] == "failed"
        writes.assert_not_called()
        assert "Calendar timed out after 0.02s" in client.get("/notifications").json()[0]["body"]


def test_expiration_during_calendar_read_prevents_writes(client, monkeypatch):
    cancel(client, "Emma Johnson")
    c = client.app.state.container
    original = c.calendar.get_days
    async def read(*args):
        days = await original(*args)
        monkeypatch.setattr(c.clock, "today", lambda: date(2026, 6, 4))
        return days
    monkeypatch.setattr(c.calendar, "get_days", read)
    write = AsyncMock()
    monkeypatch.setattr(c.calendar, "set_price", write)
    approve(client, client.get("/f5/proposals").json()[0]["approval_id"])
    assert client.get("/f5/proposals").json()[0]["status"] == "expired"
    write.assert_not_called()


def test_rounding_cannot_produce_a_free_night():
    prices = distribute_prices([night(0.01)], 0.01, 0.01, 99.9, 100)
    assert prices[0].proposed_price == 0.01
