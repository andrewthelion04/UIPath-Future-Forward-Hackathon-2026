"""Mecanismul comun de aprobări: idempotență, validare și rutarea deciziei."""

from __future__ import annotations

import pytest

from app.core.approvals import ApprovalConflict, ApprovalNotFound, InvalidDecision
from app.core.contracts.events import ApprovalDecided
from app.core.contracts.models import ApprovalOption

TWO_OPTIONS = [ApprovalOption(key="a", label="Option A"), ApprovalOption(key="b", label="Option B")]


def decided_events(container) -> list[dict]:
    return [e for e in container.bus.list_events() if e["type"] == "ApprovalDecided"]


async def test_request_creates_a_pending_approval_and_notifies_the_host(container):
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")

    assert approval.status == "pending"
    assert [a.id for a in container.approvals.list()] == [approval.id]
    notification = container.notifications.list()[0]
    assert notification.level == "action_required" and notification.approval_id == approval.id


async def test_repeating_the_same_decision_is_idempotent(container):
    calls: list[ApprovalDecided] = []

    async def handler(event: ApprovalDecided) -> None:
        calls.append(event)

    container.approvals.on_decision("test", "demo", handler)
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")

    first, applied_first = await container.approvals.decide(approval.id, decision="approve")
    second, applied_second = await container.approvals.decide(approval.id, decision="approve")
    await container.bus.join(timeout=5)

    assert (applied_first, applied_second) == (True, False)
    assert first.status == second.status == "approved"
    assert first.decided_at == second.decided_at
    assert len(decided_events(container)) == 1  # decizia s-a publicat o singură dată
    assert len(calls) == 1  # deci și acțiunea s-a executat o singură dată


async def test_a_contrary_decision_is_a_conflict(container):
    approval = await container.approvals.request(feature="test", kind="demo", title="Do it?")
    await container.approvals.decide(approval.id, decision="reject")

    with pytest.raises(ApprovalConflict):
        await container.approvals.decide(approval.id, decision="approve")
    assert container.approvals.get(approval.id).status == "rejected"
    assert len(decided_events(container)) == 1


async def test_choosing_between_options(container):
    approval = await container.approvals.request(
        feature="test", kind="demo", title="Which?", options=TWO_OPTIONS
    )
    with pytest.raises(InvalidDecision):
        await container.approvals.decide(approval.id, decision="approve")  # lipsește opțiunea
    with pytest.raises(InvalidDecision):
        await container.approvals.decide(approval.id, decision="approve", option="zzz")

    decided, applied = await container.approvals.decide(approval.id, decision="approve", option="b")
    assert applied and decided.chosen_option == "b"
    # Altă opțiune după decizie = conflict, nu o a doua decizie.
    with pytest.raises(ApprovalConflict):
        await container.approvals.decide(approval.id, decision="approve", option="a")


async def test_dedupe_key_makes_the_request_idempotent(container):
    first = await container.approvals.request(
        feature="test", kind="demo", title="Once", dedupe_key="test:once"
    )
    second = await container.approvals.request(
        feature="test", kind="demo", title="Once again", dedupe_key="test:once"
    )
    assert first.id == second.id
    assert len(container.approvals.list()) == 1


async def test_the_decision_reaches_only_the_requesting_feature(container):
    received: list[str] = []

    def collector(name: str):
        async def handler(event: ApprovalDecided) -> None:
            received.append(name)

        return handler

    container.approvals.on_decision("feature_a", "kind_x", collector("a:x"))
    container.approvals.on_decision("feature_a", "kind_y", collector("a:y"))
    container.approvals.on_decision("feature_b", "kind_x", collector("b:x"))
    approval = await container.approvals.request(
        feature="feature_a", kind="kind_x", title="?", payload={"ticket": 42}
    )

    await container.approvals.decide(approval.id, decision="approve", note="ok", decided_by="alex")
    await container.bus.join(timeout=5)

    assert received == ["a:x"]
    event = decided_events(container)[0]
    assert event["payload"]["payload"] == {"ticket": 42}  # contextul cererii se întoarce la feature
    assert event["payload"]["decided_by"] == "alex"


async def test_unknown_approval(container):
    with pytest.raises(ApprovalNotFound):
        await container.approvals.decide("apr_missing", decision="approve")


def test_decision_endpoint_status_codes(client):
    container = client.app.state.container
    approval_id = client.portal.call(
        lambda: container.approvals.request(
            feature="test", kind="demo", title="Which?", options=TWO_OPTIONS
        )
    ).id

    assert client.post("/approvals/apr_missing/decision", json={"decision": "approve"}).status_code == 404
    url = f"/approvals/{approval_id}/decision"
    assert client.post(url, json={"decision": "approve"}).status_code == 422
    assert client.post(url, json={"decision": "maybe"}).status_code == 422

    first = client.post(url, json={"decision": "approve", "option": "a"})
    again = client.post(url, json={"decision": "approve", "option": "a"})
    assert first.status_code == again.status_code == 200
    assert first.json()["applied"] is True and again.json()["applied"] is False
    assert client.post(url, json={"decision": "reject"}).status_code == 409
    assert client.get("/approvals").json() == []  # nu mai e în așteptare
    assert client.get("/approvals", params={"status": "all"}).json()[0]["chosen_option"] == "a"
