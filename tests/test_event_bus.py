"""Garanția de izolare a handlerilor: o eroare sau un timeout nu afectează alt handler."""

from __future__ import annotations

import asyncio

import pytest

from app.container import CORE_SCHEMA
from app.core.contracts.events import Event, GuestMessageReceived
from app.core.db import Database
from app.core.event_bus import EventBus
from conftest import booking_by_guest


class Ping(Event):
    value: int = 0


class Pong(Event):
    value: int = 0


@pytest.fixture
def bus(tmp_path):
    db = Database(tmp_path / "bus.db")
    db.script(CORE_SCHEMA.read_text(encoding="utf-8"))
    yield EventBus(db, default_timeout_s=5)
    db.close()


async def test_failing_and_slow_handlers_do_not_affect_the_others(bus):
    seen: list[int] = []
    failures: list[tuple[str, str]] = []
    bus.failure_hook = lambda sub, event, status, error: failures.append((sub.name, status))

    async def boom(event: Ping) -> None:
        raise RuntimeError("feature exploded")

    async def slow(event: Ping) -> None:
        await asyncio.sleep(30)

    async def healthy(event: Ping) -> None:
        seen.append(event.value)

    bus.subscribe(Ping, boom, name="broken.boom", feature="broken")
    bus.subscribe(Ping, slow, name="slow.slow", feature="slow", timeout_s=0.05)
    bus.subscribe(Ping, healthy, name="healthy.healthy", feature="healthy")

    await bus.publish(Ping(value=7))
    assert await bus.join(timeout=5)

    assert seen == [7]
    statuses = {h["handler"]: h["status"] for h in bus.list_events()[0]["handlers"]}
    assert statuses == {"broken.boom": "error", "slow.slow": "timeout", "healthy.healthy": "ok"}
    assert sorted(failures) == [("broken.boom", "error"), ("slow.slow", "timeout")]


async def test_publish_does_not_wait_for_handlers(bus):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(event: Ping) -> None:
        started.set()
        await release.wait()

    bus.subscribe(Ping, handler)
    await bus.publish(Ping())  # se întoarce imediat, handlerul rulează în background
    await started.wait()
    assert not await bus.join(timeout=0.05)
    release.set()
    assert await bus.join(timeout=5)


async def test_correlation_flows_to_events_published_by_handlers(bus):
    received: list[Pong] = []

    async def on_ping(event: Ping) -> None:
        await bus.publish(Pong(value=event.value + 1))

    async def on_pong(event: Pong) -> None:
        received.append(event)

    bus.subscribe(Ping, on_ping)
    bus.subscribe(Pong, on_pong)
    ping = await bus.publish(Ping(value=1))
    await bus.join(timeout=5)

    assert ping.correlation_id == ping.event_id
    assert received[0].correlation_id == ping.event_id
    assert received[0].causation_id == ping.event_id


async def test_predicate_routes_only_matching_events(bus):
    seen: list[int] = []

    async def handler(event: Ping) -> None:
        seen.append(event.value)

    bus.subscribe(Ping, handler, predicate=lambda event: event.value > 10)
    await bus.publish(Ping(value=1))
    await bus.publish(Ping(value=11))
    await bus.join(timeout=5)
    assert seen == [11]


def test_a_broken_feature_does_not_stop_the_guest_copilot(client):
    """Un handler care cade pe același eveniment nu împiedică F3 să răspundă."""
    container = client.app.state.container

    async def boom(event: GuestMessageReceived) -> None:
        raise RuntimeError("another feature is broken")

    container.bus.subscribe(GuestMessageReceived, boom, name="broken.boom", feature="broken")
    emma = booking_by_guest(client, "Emma Johnson")
    result = client.post(
        "/sim/guest-message",
        params={"wait": True},
        json={"booking_id": emma["id"], "text": "What time is check-in?"},
    ).json()

    assert result["outcome"] == "published" and result["completed"] is True
    messages = client.get(f"/bookings/{emma['id']}/messages").json()
    assert [m["sender"] for m in messages] == ["guest", "copilot"]
    handlers = {h["handler"]: h["status"] for h in client.get("/events").json()[-1]["handlers"]}
    assert handlers["broken.boom"] == "error"
    assert handlers["f3_guest_copilot.on_guest_message"] == "ok"
    errors = [n for n in client.get("/notifications").json() if n["level"] == "error"]
    assert any(n["feature"] == "broken" for n in errors)  # hostul e notificat
