"""F5 la granițele cu restul aplicației (auditul de integrare, P01–P05 și D04).

Regulile hostului se aplică la momentul scrierii, un handler oprit la jumătate își consemnează
progresul, o noapte nu devine gratuită, iar scrierea prețurilor nu se intercalează cu alt
feature care scrie același calendar.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.core.contracts.events import ApprovalDecided
from app.core.contracts.models import CalendarDay
from app.features.f5_relisting.pricing import distribute_prices
from app.features.f5_relisting.tests.test_relisting import calendar, cancel
from conftest import app_client, approve, drain, make_settings

REGULAR = [(True, 70.0), (True, 70.0), (True, 80.5)]


def proposal_of(client) -> dict:
    (proposal,) = client.get("/f5/proposals").json()
    return proposal


def set_rule(client, **rules) -> None:
    world = client.app.state.container.world
    host = world.get_host()
    for name, value in rules.items():
        setattr(host.rules, name, value)
    world.upsert_host(host)


# --- P01: limita hostului de la momentul aprobării ---------------------------------------------


def test_a_lower_discount_limit_set_after_the_proposal_blocks_the_write(client):
    emma = cancel(client, "Emma Johnson")
    proposal = proposal_of(client)
    set_rule(client, max_relist_discount_pct=0)

    approve(client, proposal["approval_id"])

    assert calendar(client, emma) == REGULAR
    after = proposal_of(client)
    assert after["status"] == "conflict" and "maximum relisting discount is now 0%" in after["note"]


def test_a_higher_discount_limit_does_not_block_the_approved_prices(client):
    emma = cancel(client, "Emma Johnson")
    set_rule(client, max_relist_discount_pct=60)

    approve(client, proposal_of(client)["approval_id"])

    assert calendar(client, emma) == [(True, 65.96), (True, 65.96), (True, 75.85)]


# --- P02: fără aprobare salvată, evenimentul nu scrie nimic ---------------------------------------


def test_a_decision_event_without_a_stored_decision_changes_nothing(client):
    emma = cancel(client, "Emma Johnson")
    proposal = proposal_of(client)
    container = client.app.state.container
    forged = ApprovalDecided(
        approval_id=proposal["approval_id"], feature="f5_relisting", kind="relist_price",
        decision="approved", option="approve", payload={"proposal_id": proposal["id"]},
    )  # fmt: skip

    client.portal.call(container.bus.publish, forged)
    assert drain(client)

    assert calendar(client, emma) == REGULAR
    assert proposal_of(client)["status"] == "awaiting_approval"
    assert container.approvals.get(proposal["approval_id"]).status == "pending"


# --- P03: handlerul oprit la jumătate își consemnează progresul -----------------------------------


def test_an_interrupted_price_update_is_recorded_with_its_progress(tmp_path):
    with app_client(make_settings(tmp_path, handler_timeout_s=1, sim_wait_timeout_s=10)) as client:
        emma = cancel(client, "Emma Johnson")
        proposal = proposal_of(client)
        container = client.app.state.container
        original = container.calendar.set_price

        calls = 0

        async def stuck_on_the_second_night(*args):
            nonlocal calls
            calls += 1
            if calls > 1:
                await asyncio.sleep(30)  # depășește timeoutul handlerului
            await original(*args)

        container.calendar.set_price = stuck_on_the_second_night
        approve(client, proposal["approval_id"])

        after = proposal_of(client)
        assert after["status"] == "failed"
        assert "interrupted" in after["note"] and "Confirmed updated nights: 2026-06-03." in after["note"]
        assert calendar(client, emma) == [(True, 65.96), (True, 70.0), (True, 80.5)]
        titles = [n["title"] for n in client.get("/notifications").json()]
        assert "Relisting of Old Town Studio could not be applied" in titles


# --- P04: ora de check-in mutată mai devreme scurtează termenul -----------------------------------


def test_an_earlier_check_in_time_expires_the_proposal_sooner(client, monkeypatch):
    emma = cancel(client, "Emma Johnson")
    proposal = proposal_of(client)  # expiră la 15:00, ora locală
    container = client.app.state.container
    set_rule(client, check_in_time="11:00")
    monkeypatch.setattr(container.clock, "today", lambda: date(2026, 6, 3))
    # 08:30 UTC = 11:30 la București: după noul check-in, înaintea celui salvat.
    monkeypatch.setattr(container.clock, "now", lambda: datetime(2026, 6, 3, 8, 30, tzinfo=UTC))

    assert proposal_of(client)["status"] == "expired"
    # Aprobarea a ieșit din inbox: nu mai poate schimba nimic.
    assert client.get("/approvals").json() == []
    late = client.post(f"/approvals/{proposal['approval_id']}/decision", json={"decision": "approve"})
    assert late.status_code == 409
    assert calendar(client, emma) == REGULAR


def test_a_later_check_in_time_does_not_extend_the_proposal(client, monkeypatch):
    cancel(client, "Emma Johnson")
    container = client.app.state.container
    set_rule(client, check_in_time="20:00")
    monkeypatch.setattr(container.clock, "today", lambda: date(2026, 6, 3))
    monkeypatch.setattr(container.clock, "now", lambda: datetime(2026, 6, 3, 12, 30, tzinfo=UTC))  # 15:30 local

    assert proposal_of(client)["status"] == "expired"


# --- P05: o noapte cu preț pozitiv nu devine gratuită ----------------------------------------------


def test_a_positive_night_never_becomes_free():
    night = CalendarDay(property_id="p", day=date(2026, 6, 3), available=True, price=0.01, currency="EUR")

    (result,) = distribute_prices([night], 70, 0.01, 99.9, 100)

    assert result.proposed_price == 0.01 and result.floor_price == 0


# --- D04: verificarea și scrierea stau sub lockul comun al calendarului ---------------------------


def test_prices_are_written_under_the_shared_calendar_lock(client):
    emma = cancel(client, "Emma Johnson")
    proposal = proposal_of(client)
    container = client.app.state.container
    lock = container.calendar.lock(emma["property_id"])
    original_get, original_set = container.calendar.get_days, container.calendar.set_price
    held: list[bool] = []

    async def get_days(*args):
        held.append(lock.locked())
        return await original_get(*args)

    async def set_price(*args):
        held.append(lock.locked())
        await original_set(*args)

    container.calendar.get_days, container.calendar.set_price = get_days, set_price
    approve(client, proposal["approval_id"])

    assert len(held) == 7 and all(held)  # snapshot complet + trei recitiri pe noapte + trei scrieri
    assert not lock.locked() and proposal_of(client)["status"] == "applied"


def test_a_night_blocked_by_another_flow_before_the_write_is_not_discounted(client):
    """Alt feature (ex. o relocare F2) ține lockul și blochează noaptea: F5 vede conflictul."""
    emma = cancel(client, "Emma Johnson")
    proposal = proposal_of(client)
    container = client.app.state.container
    start = date.fromisoformat(emma["check_in"])

    async def race() -> bool:
        async with container.calendar.lock(emma["property_id"]):
            await container.approvals.decide(proposal["approval_id"], decision="approve")
            await asyncio.sleep(0.05)  # handlerul F5 așteaptă lockul
            await container.calendar.set_availability(
                emma["property_id"], start, start + timedelta(days=1), False
            )
        return await container.bus.join(timeout=10)

    assert client.portal.call(race)
    assert proposal_of(client)["status"] == "conflict"
    assert calendar(client, emma) == [(False, 70.0), (True, 70.0), (True, 80.5)]
