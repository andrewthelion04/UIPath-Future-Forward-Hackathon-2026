"""Ordinea conversațiilor F3, fără rețea și fără modificarea serviciilor comune."""

from __future__ import annotations

import asyncio

import pytest

from app.core.agent.loop import AgentResult
from app.core.channel_gateway import ChannelError
from app.core.contracts.events import ApprovalDecided, GuestMessageReceived
from app.features.f3_guest_copilot.copilot import GuestCopilot


FEATURE = "f3_guest_copilot"


def guest_event(booking, message: str) -> GuestMessageReceived:
    return GuestMessageReceived(
        message_id=f"msg_{message}",
        booking_id=booking.id,
        property_id=booking.property_id,
        text=message,
        pms_provider=booking.pms_provider,
        source_channel=booking.source_channel,
    )


def answered(state) -> AgentResult:
    state["replies_sent"] = 1
    return AgentResult(run_id="run_test", status="finished", result="Answered")


async def test_same_booking_messages_run_consecutively_and_refresh_context(container, monkeypatch):
    c = container
    ctx = c._make_feature_context(FEATURE)
    copilot = GuestCopilot(ctx)
    booking = next(b for b in c.world.list_bookings() if b.guest_name == "Emma Johnson")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(spec, *, objective, state, **kwargs):
        text = state["guest_text"]
        calls.append(("start", text, objective, dict(state)))
        if text == "first":
            entered.set()
            await release.wait()
        calls.append(("finish", text))
        return answered(state)

    monkeypatch.setattr(c.agent, "run", run)
    first = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "first")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "second")))
    # Dăm celui de-al doilea handler ocazia să intre în coadă înaintea actualizării.
    await asyncio.sleep(0)
    c.world.upsert_booking(booking.model_copy(update={"guest_name": "Emma Updated"}))
    release.set()
    await asyncio.gather(first, second)

    assert [(row[0], row[1]) for row in calls] == [
        ("start", "first"), ("finish", "first"),
        ("start", "second"), ("finish", "second"),
    ]
    assert "Emma Johnson" in calls[0][2] and "Emma Updated" in calls[2][2]
    assert calls[0][3]["message_id"] == "msg_first"
    assert calls[2][3]["message_id"] == "msg_second"


async def test_different_booking_messages_can_run_in_parallel(container, monkeypatch):
    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    first, second = c.world.list_bookings()[:2]
    both_entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = 0
    seen = []

    async def run(spec, *, state, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        seen.append((state["booking_id"], state["message_id"]))
        if active == 2:
            both_entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return answered(state)

    monkeypatch.setattr(c.agent, "run", run)
    tasks = [
        asyncio.create_task(copilot.on_guest_message(guest_event(first, "first"))),
        asyncio.create_task(copilot.on_guest_message(guest_event(second, "second"))),
    ]
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert maximum == 2
    assert set(seen) == {(first.id, "msg_first"), (second.id, "msg_second")}


async def test_queue_timeout_notifies_host_and_later_messages_can_run(container, monkeypatch):
    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE), queue_timeout_s=0.02)
    booking = c.world.list_bookings()[0]
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def run(spec, *, state, **kwargs):
        seen.append(state["guest_text"])
        if state["guest_text"] == "first":
            entered.set()
            await release.wait()
        return answered(state)

    monkeypatch.setattr(c.agent, "run", run)
    first = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "first")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        await copilot.on_guest_message(guest_event(booking, "expired"))
    finally:
        release.set()
        await first
    await copilot.on_guest_message(guest_event(booking, "later"))
    assert seen == ["first", "later"]
    notifications = c.notifications.list()
    assert any("conversation queue timed out" in item.body for item in notifications)
    assert any(item.booking_id == booking.id and "reply manually" in item.title for item in notifications)


async def test_cancelled_run_releases_conversation_and_notifies_host(container, monkeypatch):
    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    booking = c.world.list_bookings()[0]
    entered = asyncio.Event()
    seen = []

    async def run(spec, *, state, **kwargs):
        seen.append(state["guest_text"])
        if state["guest_text"] == "cancelled":
            entered.set()
            await asyncio.Event().wait()
        return answered(state)

    monkeypatch.setattr(c.agent, "run", run)
    running = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "cancelled")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await copilot.on_guest_message(guest_event(booking, "next"))
    assert seen == ["cancelled", "next"]
    assert any("processing was cancelled or timed out" in item.body for item in c.notifications.list())


async def test_cancelling_waiter_does_not_release_another_handlers_lock(container, monkeypatch):
    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    booking = c.world.list_bookings()[0]
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def run(spec, *, state, **kwargs):
        seen.append(state["guest_text"])
        if state["guest_text"] == "first":
            entered.set()
            await release.wait()
        return answered(state)

    monkeypatch.setattr(c.agent, "run", run)
    running = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "first")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    waiting = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "waiting")))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    later = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "later")))
    await asyncio.sleep(0)
    assert seen == ["first"]
    release.set()
    await asyncio.gather(running, later)
    assert seen == ["first", "later"]


async def stored_decision(c, booking) -> ApprovalDecided:
    approval = await c.approvals.request(
        feature=FEATURE, kind="guest_request", title="Guest request",
        booking_id=booking.id, property_id=booking.property_id,
        payload={"booking_id": booking.id, "proposed_reply": "Reviewed reply"},
    )
    # Pregătim o decizie persistentă fără publicarea ei către instanța F3 din container.
    c.db.execute(
        "UPDATE approvals SET status = 'approved', chosen_option = 'approve' WHERE id = ?",
        (approval.id,),
    )
    return ApprovalDecided(
        approval_id=approval.id, feature=FEATURE, kind="guest_request",
        decision="approved", option="approve", payload=approval.payload,
    )


async def test_host_decision_waits_for_message_and_is_delivered_once(container, monkeypatch):
    from app.features.f3_guest_copilot import requests

    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    booking = c.world.list_bookings()[0]
    event = await stored_decision(c, booking)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def run(spec, *, state, **kwargs):
        effects.append("message started")
        entered.set()
        await release.wait()
        effects.append("message finished")
        return answered(state)

    async def decision_reply(ctx, approval_id, booking_id, decision):
        effects.append("decision checked")
        return "Reviewed reply"

    original_send = c.channel.send_to_guest

    async def send(*args, **kwargs):
        effects.append("decision sent")
        return await original_send(*args, **kwargs)

    monkeypatch.setattr(c.agent, "run", run)
    monkeypatch.setattr(requests, "decision_reply", decision_reply)
    monkeypatch.setattr(c.channel, "send_to_guest", send)
    message = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "message")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    decision = asyncio.create_task(copilot.on_guest_request_decision(event))
    await asyncio.sleep(0)
    assert effects == ["message started"]
    release.set()
    await asyncio.gather(message, decision)
    await copilot.on_guest_request_decision(event)
    assert effects == ["message started", "message finished", "decision checked", "decision sent"]
    assert [message.body for message in c.conversations.history(booking.id)] == ["Reviewed reply"]


async def test_guest_message_waits_while_host_decision_is_sending(container, monkeypatch):
    from app.features.f3_guest_copilot import requests

    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    booking = c.world.list_bookings()[0]
    event = await stored_decision(c, booking)
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def decision_reply(ctx, approval_id, booking_id, decision):
        effects.append("decision started")
        entered.set()
        await release.wait()
        effects.append("decision finished")
        return "Reviewed reply"

    async def run(spec, *, state, **kwargs):
        effects.append("message started")
        return answered(state)

    monkeypatch.setattr(requests, "decision_reply", decision_reply)
    monkeypatch.setattr(c.agent, "run", run)
    decision = asyncio.create_task(copilot.on_guest_request_decision(event))
    await asyncio.wait_for(entered.wait(), timeout=2)
    message = asyncio.create_task(copilot.on_guest_message(guest_event(booking, "message")))
    await asyncio.sleep(0)
    assert effects == ["decision started"]
    release.set()
    await asyncio.gather(decision, message)
    assert effects == ["decision started", "decision finished", "message started"]


async def test_forged_decision_payload_cannot_redirect_or_send_a_message(container):
    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    first, second = c.world.list_bookings()[:2]
    event = await stored_decision(c, first)
    forged = event.model_copy(update={"payload": {"booking_id": second.id, "proposed_reply": "Forged"}})
    await copilot.on_guest_request_decision(forged)
    assert c.conversations.history(first.id) == [] and c.conversations.history(second.id) == []
    assert any("does not match" in item.body for item in c.notifications.list())


async def test_failed_decision_delivery_can_be_retried_without_duplicate_success(container, monkeypatch):
    from app.features.f3_guest_copilot import requests

    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    booking = c.world.list_bookings()[0]
    event = await stored_decision(c, booking)
    attempts = 0
    original_send = c.channel.send_to_guest

    async def decision_reply(ctx, approval_id, booking_id, decision):
        return "Reviewed reply"

    async def send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ChannelError("temporary channel failure")
        return await original_send(*args, **kwargs)

    monkeypatch.setattr(requests, "decision_reply", decision_reply)
    monkeypatch.setattr(c.channel, "send_to_guest", send)
    await copilot.on_guest_request_decision(event)
    assert c.conversations.history(booking.id) == []
    await copilot.on_guest_request_decision(event)
    await copilot.on_guest_request_decision(event)
    assert attempts == 2
    assert [message.body for message in c.conversations.history(booking.id)] == ["Reviewed reply"]


async def test_domain_revalidation_failure_prevents_decision_message(container, monkeypatch):
    from app.features.f3_guest_copilot import requests

    c = container
    copilot = GuestCopilot(c._make_feature_context(FEATURE))
    booking = c.world.list_bookings()[0]
    event = await stored_decision(c, booking)

    async def decision_reply(ctx, approval_id, booking_id, decision):
        raise ValueError("booking conditions changed")

    monkeypatch.setattr(requests, "decision_reply", decision_reply)
    await copilot.on_guest_request_decision(event)
    assert c.conversations.history(booking.id) == []
    assert any("booking conditions changed" in item.body for item in c.notifications.list())
