"""Garanțiile nucleului găsite lipsă la auditul de integrare (docs/INTEGRATION_AUDIT_2026-11-14.md).

Fiecare test fixează o problemă din audit: aprobări care rămân fără efect, evenimente de
decizie fără aprobare salvată, aprobări învechite, resetul lumii cu muncă în curs, shutdown-ul
cu sarcini de fundal și lockul comun al calendarului.
"""

from __future__ import annotations

import asyncio

import pytest

from app.container import Container
from app.core.approvals import ApprovalConflict
from app.core.contracts.events import ApprovalDecided, Event
from conftest import app_client, booking_by_guest, make_settings, property_by_name


class Ping(Event):
    pass


def fail_once(container, event_cls):
    """`bus.publish` cade o singură dată pentru `event_cls` (ex. baza e ocupată o clipă)."""
    original, state = container.bus.publish, {"failed": False}

    async def publish(event):
        if isinstance(event, event_cls) and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("transient event persistence failure")
        return await original(event)

    container.bus.publish = publish


# --- C01: decizia nu rămâne salvată fără eveniment ----------------------------------------


async def test_a_decision_whose_event_could_not_be_published_can_be_retried(container):
    calls: list[ApprovalDecided] = []

    async def handler(event: ApprovalDecided) -> None:
        calls.append(event)

    container.approvals.on_decision("test", "demo", handler)
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")
    fail_once(container, ApprovalDecided)

    with pytest.raises(RuntimeError):
        await container.approvals.decide(approval.id, decision="approve")
    assert container.approvals.get(approval.id).status == "pending"  # încă în inbox

    decided, applied = await container.approvals.decide(approval.id, decision="approve")
    await container.bus.join(timeout=5)
    assert applied is True and decided.status == "approved"
    assert len(calls) == 1


# --- P02: un eveniment de decizie fără aprobare salvată nu execută nimic --------------------


async def test_a_decision_event_that_does_not_match_the_stored_approval_is_ignored(container):
    calls: list[ApprovalDecided] = []

    async def handler(event: ApprovalDecided) -> None:
        calls.append(event)

    container.approvals.on_decision("test", "demo", handler)
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")

    forged = ApprovalDecided(
        approval_id=approval.id, feature="test", kind="demo", decision="approved", option="approve"
    )
    await container.bus.publish(forged)
    await container.bus.join(timeout=5)
    assert calls == [] and container.approvals.get(approval.id).status == "pending"

    await container.approvals.decide(approval.id, decision="approve")
    await container.bus.join(timeout=5)
    assert [event.approval_id for event in calls] == [approval.id]


# --- Aprobări retrase ---------------------------------------------------------------------


async def test_a_withdrawn_approval_leaves_the_inbox_and_cannot_be_decided(container):
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")

    assert container.approvals.withdraw(approval.id, reason="No longer true.") is True
    assert container.approvals.withdraw(approval.id) is False  # o singură dată
    assert container.approvals.list() == []
    stored = container.approvals.get(approval.id)
    assert (stored.status, stored.decision_note) == ("withdrawn", "No longer true.")
    with pytest.raises(ApprovalConflict, match="already withdrawn"):
        await container.approvals.decide(approval.id, decision="approve")


async def test_a_decided_approval_cannot_be_withdrawn(container):
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")
    await container.approvals.decide(approval.id, decision="approve")

    assert container.approvals.withdraw(approval.id) is False
    assert container.approvals.get(approval.id).status == "approved"


# --- C06: resetul nu lasă munca lumii vechi să scrie în cea nouă -----------------------------


async def test_reset_stops_handlers_that_are_still_running(container, monkeypatch):
    started, finished = asyncio.Event(), []

    async def slow(event: Ping) -> None:
        started.set()
        await asyncio.sleep(30)
        finished.append(event)  # ar scrie în lumea nouă

    container.bus.subscribe(Ping, slow, name="test.slow", timeout_s=60)
    await container.bus.publish(Ping())
    await asyncio.wait_for(started.wait(), 2)

    original_join = container.bus.join
    monkeypatch.setattr(container.bus, "join", lambda timeout=None: original_join(timeout=0.05))
    await container.reset_world()

    assert finished == [] and await original_join(timeout=1) is True
    assert container.world.get_host() is not None  # lumea nouă e întreagă


# --- C07: shutdown-ul așteaptă munca de fundal a feature-urilor ------------------------------


async def test_shutdown_waits_for_feature_background_work(tmp_path):
    container = Container(make_settings(tmp_path))
    await container.startup()
    prop = container.world.list_properties()[0]
    container.ids.link("airbnb", "listing", "FAST-SHUTDOWN", prop.id)  # programează o reprocesare F1
    pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("f1-reprocess:")]
    assert pending

    await container.shutdown()

    results = await asyncio.gather(*pending, return_exceptions=True)
    assert [r for r in results if isinstance(r, BaseException)] == []


async def test_quiesce_hooks_run_on_reset_and_shutdown(tmp_path):
    container = Container(make_settings(tmp_path))
    await container.startup()
    calls: list[str] = []

    async def hook() -> None:
        calls.append("quiesced")

    container._make_feature_context("test").on_quiesce(hook)  # noqa: SLF001
    assert calls == []  # se aplică abia la commit, ca orice înregistrare a unui feature
    context = container._make_feature_context("test")  # noqa: SLF001
    context.on_quiesce(hook)
    context.commit()

    await container.reset_world()
    await container.shutdown()
    assert calls == ["quiesced", "quiesced"]


# --- Lockul comun al calendarului ------------------------------------------------------------


async def test_calendar_lock_is_shared_per_property(container):
    first, second = [p.id for p in container.world.list_properties()[:2]]

    assert container.calendar.lock(first) is container.calendar.lock(first)
    assert container.calendar.lock(first) is not container.calendar.lock(second)
    async with container.calendar.lock(first):
        assert container.calendar.lock(first).locked()
        assert not container.calendar.lock(second).locked()


# --- I05 / C04: intrările comune verifică relațiile, nu doar existența -----------------------


def test_staff_report_rejects_a_booking_of_another_property(client):
    studio = property_by_name(client, "Old Town Studio")
    lukas = booking_by_guest(client, "Lukas Mueller")  # cazat la Calea Victoriei

    response = client.post(
        "/sim/staff-report",
        json={"property_id": studio["id"], "booking_id": lukas["id"], "description": "Broken lamp"},
    )
    assert response.status_code == 422 and "is not a stay at" in response.json()["detail"]
    assert client.get("/events").json() == []


def test_a_booking_cannot_be_mapped_on_another_pms_connection(tmp_path):
    class OtherPms:
        provider = "hospitable"

        def __init__(self, capabilities) -> None:
            self.capabilities = capabilities

    with app_client(make_settings(tmp_path)) as client:
        container = client.app.state.container
        container.pms.register(OtherPms(container.pms.get("mock").capabilities))
        emma = booking_by_guest(client, "Emma Johnson")  # rezervare a conexiunii mock

        response = client.put("/integrations/mappings", json={
            "connection": "hospitable", "entity_type": "reservation",
            "external_id": "FOREIGN-1", "internal_id": emma["id"],
        })  # fmt: skip
        assert response.status_code == 409 and "belongs to the 'mock' connection" in response.json()["detail"]
        assert container.ids.to_internal("hospitable", "reservation", "FOREIGN-1") is None
        # Entitățile care nu sunt ale unui PMS (ex. anunțul Airbnb pentru recenzii) rămân libere.
        listing = client.put("/integrations/mappings", json={
            "connection": "airbnb", "entity_type": "listing",
            "external_id": "https://www.airbnb.com/rooms/1", "internal_id": emma["property_id"],
        })  # fmt: skip
        assert listing.status_code == 200
