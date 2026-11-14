"""Regresii de autorizare, politici curente, anulare și limitele agentului F5."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.agent.mock_support import finish
from app.core.contracts.events import ApprovalDecided
from app.core.contracts.models import CalendarDay
from app.features.f5_relisting.pricing import distribute_prices
from app.features.f5_relisting.tests.test_relisting import FakeMarket, calendar, cancel, comparable
from app.ports.llm import ChatResponse
from conftest import app_client, approve, drain, events_of


def test_current_host_discount_is_enforced_before_any_write(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    container = client.app.state.container
    (proposal,) = client.get("/f5/proposals").json()
    before = calendar(client, booking)
    host = container.world.get_host()
    host.rules.max_relist_discount_pct = 0
    container.world.upsert_host(host)
    prices, availability = AsyncMock(), AsyncMock()
    monkeypatch.setattr(container.calendar, "set_price", prices)
    monkeypatch.setattr(container.calendar, "set_availability", availability)

    assert approve(client, proposal["approval_id"])["completed"] is True

    assert client.get("/f5/proposals").json()[0]["status"] == "conflict"
    assert calendar(client, booking) == before
    prices.assert_not_called()
    availability.assert_not_called()
    assert any(n["level"] == "warning" for n in client.get("/notifications").json())
    handler = events_of(client, "ApprovalDecided")[0]["handlers"][0]
    assert handler["status"] == "ok"


def test_internal_decision_cannot_replace_the_persisted_host_approval(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    container = client.app.state.container
    (proposal,) = client.get("/f5/proposals").json()
    before = calendar(client, booking)
    writes = AsyncMock()
    monkeypatch.setattr(container.calendar, "set_price", writes)
    event = ApprovalDecided(
        approval_id=proposal["approval_id"], feature="f5_relisting", kind="relist_price",
        decision="approved", option="approve", payload={"proposal_id": proposal["id"]},
    )

    client.portal.call(container.bus.publish, event)
    assert drain(client)

    assert container.approvals.get(proposal["approval_id"]).status == "pending"
    assert client.get("/f5/proposals").json()[0]["status"] == "awaiting_approval"
    assert calendar(client, booking) == before
    writes.assert_not_called()


@pytest.mark.parametrize("field, value", [
    ("feature", "another_feature"),
    ("kind", "another_kind"),
    ("booking_id", "another_booking"),
    ("property_id", "another_property"),
    ("payload", '{"proposal_id":"another_proposal"}'),
    ("chosen_option", "another_option"),
])
def test_mismatched_persistent_approval_never_authorizes_writes(client, monkeypatch, field, value):
    booking = cancel(client, "Emma Johnson")
    container = client.app.state.container
    (proposal,) = client.get("/f5/proposals").json()
    before = calendar(client, booking)
    # Simulăm o aprobare persistentă pentru alt scop, fără a emite decizia normală.
    container.db.execute(
        "UPDATE approvals SET status = 'approved', chosen_option = 'approve' WHERE id = ?",
        (proposal["approval_id"],),
    )
    container.db.execute(f"UPDATE approvals SET {field} = ? WHERE id = ?", (value, proposal["approval_id"]))
    writes = AsyncMock()
    monkeypatch.setattr(container.calendar, "set_price", writes)
    event = ApprovalDecided(
        approval_id=proposal["approval_id"], feature="f5_relisting", kind="relist_price",
        decision="approved", option="approve", payload={"proposal_id": proposal["id"]},
    )

    client.portal.call(container.bus.publish, event)
    assert drain(client)

    assert calendar(client, booking) == before
    writes.assert_not_called()
    assert client.get("/f5/proposals").json()[0]["status"] != "applied"


def test_earlier_current_host_check_in_expires_the_existing_proposal(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    container = client.app.state.container
    (proposal,) = client.get("/f5/proposals").json()
    before = calendar(client, booking)
    host = container.world.get_host()
    host.rules.check_in_time = "11:00"
    container.world.upsert_host(host)
    monkeypatch.setattr(container.clock, "today", lambda: date(2026, 6, 3))
    monkeypatch.setattr(container.clock, "now", lambda: datetime(2026, 6, 3, 8, 30, tzinfo=UTC))
    writes = AsyncMock()
    monkeypatch.setattr(container.calendar, "set_price", writes)

    approve(client, proposal["approval_id"])

    assert client.get("/f5/proposals").json()[0]["status"] == "expired"
    assert datetime.fromisoformat(client.get("/f5/proposals").json()[0]["expires_at"]).hour == 11
    assert calendar(client, booking) == before
    writes.assert_not_called()
    assert any("expired" in n["title"].lower() for n in client.get("/notifications").json())


def test_outer_handler_timeout_persists_partial_application_failure(settings, monkeypatch):
    settings = settings.model_copy(update={"handler_timeout_s": 0.3, "sim_wait_timeout_s": 2})
    with app_client(settings) as client:
        booking = cancel(client, "Emma Johnson")
        container = client.app.state.container
        (proposal,) = client.get("/f5/proposals").json()
        original = container.calendar.set_price

        async def delayed_write(*args):
            await asyncio.sleep(0.2)
            await original(*args)

        monkeypatch.setattr(container.calendar, "set_price", delayed_write)
        assert approve(client, proposal["approval_id"])["completed"] is True

        current = client.get("/f5/proposals").json()[0]
        assert current["status"] == "failed"
        assert container.approvals.get(proposal["approval_id"]).status == "approved"
        assert calendar(client, booking) == [(True, 65.96), (True, 70), (True, 80.5)]
        assert booking["check_in"] in current["note"]
        assert any(
            n["feature"] == "f5_relisting" and n["level"] == "warning"
            and booking["check_in"] in n["body"]
            for n in client.get("/notifications").json()
        )
        approve(client, proposal["approval_id"])
        assert calendar(client, booking) == [(True, 65.96), (True, 70), (True, 80.5)]


def test_small_positive_calendar_prices_do_not_become_free_with_a_large_base_price():
    days = [
        CalendarDay(property_id="p", day=date(2026, 6, 3) + timedelta(days=i),
                    available=True, price=price)
        for i, price in enumerate([0.01, 0.02, 70])
    ]

    prices = distribute_prices(days, base_price=70, comparable_price=0.01,
                               undercut_pct=99.9, max_discount_pct=100)

    assert all(0.01 <= n.proposed_price <= n.original_price for n in prices)
    assert all(n.proposed_price >= n.floor_price for n in prices)


def test_agent_finishing_before_inspection_notifies_the_host(client, monkeypatch):
    container = client.app.state.container

    async def premature_finish(request):
        return ChatResponse(message=finish("All done."), model="premature-finish")

    monkeypatch.setattr(container.llm, "chat", premature_finish)
    booking = cancel(client, "Emma Johnson")

    assert client.get("/f5/proposals").json() == []
    assert client.get("/approvals").json() == []
    assert calendar(client, booking) == [(True, 70), (True, 70), (True, 80.5)]
    assert any(
        n["feature"] == "f5_relisting" and n["level"] == "warning"
        and n["booking_id"] == booking["id"]
        for n in client.get("/notifications").json()
    )


def test_many_comparables_keep_agent_tool_results_usable(settings):
    market = FakeMarket([comparable(68 + i, ref=f"listing-{i}") for i in range(100)])
    with app_client(settings, market=market) as client:
        booking = cancel(client, "Emma Johnson")

        (proposal,) = client.get("/f5/proposals").json()
        assert proposal["status"] == "awaiting_approval"
        assert proposal["comparable_ref"] == "listing-0"
        assert proposal["comparable_price"] == 68
        assert proposal["approval_id"]
        assert calendar(client, booking) == [(True, 70), (True, 70), (True, 80.5)]
        (run,) = client.get("/agent/runs", params={"feature": "f5_relisting"}).json()
        assert run["status"] == "finished"


def test_final_nightly_read_catches_a_booking_after_the_initial_snapshot(client, monkeypatch):
    booking = cancel(client, "Emma Johnson")
    container = client.app.state.container
    proposal = client.get("/f5/proposals").json()[0]
    original = container.calendar.get_days
    start, end = date.fromisoformat(booking["check_in"]), date.fromisoformat(booking["check_out"])

    async def read(property_id, first, last):
        days = await original(property_id, first, last)
        if first == start and last == end:
            await container.calendar.set_availability(property_id, start, start + timedelta(days=1), False)
        return days

    monkeypatch.setattr(container.calendar, "get_days", read)
    write = AsyncMock()
    monkeypatch.setattr(container.calendar, "set_price", write)
    approve(client, proposal["approval_id"])
    assert client.get("/f5/proposals").json()[0]["status"] == "conflict"
    assert calendar(client, booking) == [(False, 70), (True, 70), (True, 80.5)]
    write.assert_not_called()
