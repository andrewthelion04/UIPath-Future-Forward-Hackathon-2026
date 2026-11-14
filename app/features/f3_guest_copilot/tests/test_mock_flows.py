"""Scenarii F3 pe politica mock: efecte observabile, fără rețea."""

from __future__ import annotations

import pytest

from app.core.contracts.events import IssueReported
from conftest import approve, booking_by_guest, events_of, reject


def say(client, text: str, guest: str = "Emma Johnson") -> dict:
    booking = booking_by_guest(client, guest)
    response = client.post(
        "/sim/guest-message", params={"wait": True},
        json={"booking_id": booking["id"], "text": text},
    )
    assert response.status_code == 200
    assert response.json()["completed"] is True
    return booking


def replies(client, booking: dict) -> list[dict]:
    return [message for message in client.get(f"/bookings/{booking['id']}/messages").json()
            if message["sender"] == "copilot" and message["status"] == "sent"]


def guest_requests(client) -> list[dict]:
    return [approval for approval in client.get("/approvals", params={"status": "all"}).json()
            if approval["feature"] == "f3_guest_copilot" and approval["kind"] == "guest_request"]


def test_reformulated_pending_request_reuses_the_same_approval(client):
    booking = say(client, "Could we check in early, around noon?")
    (first,) = guest_requests(client)
    say(client, "We would like to arrive earlier at 12:00, please.")
    (same,) = guest_requests(client)
    assert first["id"] == same["id"]
    assert same["status"] == "pending"
    assert len(events_of(client, "ApprovalRequested")) == 1
    assert len([n for n in client.get("/notifications").json() if n["level"] == "action_required"]) == 1
    assert "already with the host" in replies(client, booking)[-1]["body"]


def test_changed_requested_time_creates_a_distinct_approval(client):
    say(client, "Could we check in early at 12:00?")
    say(client, "Could we check in early at 13:00 instead?")
    approvals = guest_requests(client)
    assert len(approvals) == 2
    assert len({a["dedupe_key"] for a in approvals}) == 2
    assert {a["payload"]["request"]["requested_time"][:5] for a in approvals} == {"12:00", "13:00"}


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_repeated_decided_request_reuses_recorded_outcome(client, decision):
    booking = say(client, "Could we check in early at noon?")
    (approval,) = guest_requests(client)
    (approve if decision == "approved" else reject)(client, approval["id"])
    say(client, "Could we arrive early at 12:00?")
    assert len(guest_requests(client)) == 1
    assert len(events_of(client, "ApprovalRequested")) == 1
    last_reply = replies(client, booking)[-1]["body"]
    if decision == "approved":
        assert last_reply == approval["payload"]["proposed_reply"]
    else:
        assert "previously declined" in last_reply
        assert "confirmed" not in last_reply


def test_extra_guest_within_capacity_needs_host_approval(client):
    booking = say(client, "Could we bring one additional guest?", guest="Sofia Rossi")
    (approval,) = guest_requests(client)
    assert approval["payload"]["request"]["request_type"] == "extra_guests"
    assert approval["payload"]["request"]["guest_count"] == 5
    assert "passed your request" in replies(client, booking)[-1]["body"]
    approve(client, approval["id"])
    assert "total of 5 guests" in replies(client, booking)[-1]["body"]


def test_extra_guests_beyond_capacity_are_never_promised(client):
    booking = say(client, "Could we bring one extra guest?")
    assert guest_requests(client) == []
    assert "could not submit" in replies(client, booking)[-1]["body"]
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_extra_guest_request_without_a_count_asks_for_clarification(client):
    booking = say(client, "Can we bring extra guests?")
    assert guest_requests(client) == []
    assert "How many people would stay in total?" in replies(client, booking)[-1]["body"]


def test_bringing_a_pet_requests_an_exception_but_policy_question_does_not(client):
    booking = say(client, "Are pets allowed?")
    assert guest_requests(client) == []
    assert "not allowed under the standard house rules" in replies(client, booking)[-1]["body"]
    say(client, "Could we bring our dog?")
    (approval,) = guest_requests(client)
    assert approval["payload"]["request"]["request_type"] == "pets"
    assert approval["payload"]["request"]["pet_count"] == 1
    approve(client, approval["id"])
    assert "approved 1 pet" in replies(client, booking)[-1]["body"]


def test_pets_already_allowed_are_answered_from_the_rule(client):
    c = client.app.state.container
    host = c.world.get_host()
    host.rules.pets_allowed = True
    c.world.upsert_host(host)
    booking = say(client, "Can we bring our dog?")
    assert guest_requests(client) == []
    assert "host rules permit pets" in replies(client, booking)[-1]["body"]


@pytest.mark.parametrize("text, forbidden", [
    ("Where can we park?", ("public parking", "parking spot is included", "free parking")),
    ("What is the Wi-Fi password?", ("welcome guide",)),
])
def test_missing_details_are_not_invented_and_host_is_notified(client, text, forbidden):
    booking = say(client, text)
    body = replies(client, booking)[-1]["body"]
    assert "do not have verified" in body or "do not have a verified" in body
    assert all(fragment not in body.lower() for fragment in forbidden)
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_access_instructions_use_only_host_notes(client):
    booking = say(client, "How do we get in? Is there a lockbox?")
    body = replies(client, booking)[-1]["body"]
    assert "Self check-in with a lockbox; the code is sent on the morning of arrival." in body
    assert not any(char.isdigit() for char in body)


def test_missing_access_instructions_request_host_follow_up(client):
    c = client.app.state.container
    host = c.world.get_host()
    host.rules.house_notes = []
    c.world.upsert_host(host)
    booking = say(client, "Please send the door access code.")
    assert "do not have verified access instructions" in replies(client, booking)[-1]["body"]
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_listed_parking_does_not_imply_an_allocated_or_free_space(client):
    booking = say(client, "Is parking free and can we reserve a space?", guest="Sofia Rossi")
    body = replies(client, booking)[-1]["body"]
    assert "Parking is listed among the property's amenities" in body
    assert "do not have verified parking availability, allocation or cost" in body
    assert "free parking" not in body and "is reserved" not in body
    assert guest_requests(client) == []


@pytest.mark.parametrize("text", [
    "Could you give us a discount?",
    "Can we get a refund?",
    "Can you waive the cleaning fee?",
    "Is breakfast included?",
    "What time does the nearby museum open?",
])
def test_unsupported_requests_and_unknown_facts_need_host_follow_up(client, text):
    booking = say(client, text)
    body = replies(client, booking)[-1]["body"]
    assert "do not have verified" in body and "host will follow up" in body
    assert "Check-in at" not in body
    assert guest_requests(client) == []
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_reported_issue_does_not_hide_an_unsupported_refund_request(client):
    booking = say(client, "The shower is broken; can we get a refund?")
    assert len(events_of(client, "IssueReported")) == 1
    body = replies(client, booking)[-1]["body"]
    assert "informed the host" in body and "do not have verified details" in body
    assert guest_requests(client) == []
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_mixed_request_reuses_exact_confirmation_without_dropping_other_intents(client):
    booking = say(client, "Could we check in early at noon?")
    (approval,) = guest_requests(client)
    approve(client, approval["id"])
    count = len(replies(client, booking))
    say(client, "The shower is broken; could we check in early at noon and get a refund?")
    new_replies = replies(client, booking)[count:]
    assert len(new_replies) == 2
    assert "informed the host" in new_replies[0]["body"]
    assert "do not have verified details" in new_replies[0]["body"]
    assert new_replies[1]["body"] == approval["payload"]["proposed_reply"]
    assert len(guest_requests(client)) == 1
    assert len(events_of(client, "IssueReported")) == 1


def test_issue_and_exception_in_one_message_both_have_effects(client):
    booking = say(client, "The shower is broken. Could we also check in early at noon?")
    issue, = events_of(client, "IssueReported")
    assert issue["payload"]["booking_id"] == booking["id"]
    approval, = guest_requests(client)
    assert approval["payload"]["request"]["request_type"] == "early_check_in"
    body = replies(client, booking)[-1]["body"]
    assert "informed the host" in body and "passed your request" in body


def test_two_different_exceptions_do_not_bypass_the_per_run_approval_limit(client):
    booking = say(client, "Could we check in early at noon and bring our dog?")
    (approval,) = guest_requests(client)
    assert approval["payload"]["request"]["request_type"] == "early_check_in"
    assert "other request also needs the host's attention" in replies(client, booking)[-1]["body"]
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_issue_tool_failure_does_not_claim_the_incident_was_forwarded(client, monkeypatch):
    c = client.app.state.container
    original_publish = c.bus.publish

    async def fail_issue(event):
        if isinstance(event, IssueReported):
            raise RuntimeError("issue storage unavailable")
        return await original_publish(event)

    monkeypatch.setattr(c.bus, "publish", fail_issue)
    booking = say(client, "The shower is broken.")
    assert events_of(client, "IssueReported") == []
    body = replies(client, booking)[-1]["body"]
    assert "could not forward" in body and "I have informed" not in body
    assert any("manual follow-up" in n["title"] for n in client.get("/notifications").json())


def test_calendar_failure_is_unknown_and_cannot_be_confirmed_automatically(client, monkeypatch):
    c = client.app.state.container

    async def unavailable(*args, **kwargs):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(c.calendar, "get_days", unavailable)
    booking = say(client, "Could we check in early at noon?")
    approval, = guest_requests(client)
    assert "unknown" in approval["summary"].lower()
    assert "confirmed" not in replies(client, booking)[-1]["body"]
    approve(client, approval["id"])
    assert len(replies(client, booking)) == 1
    assert any("manually" in n["title"] or "manual" in n["body"] for n in client.get("/notifications").json())


def test_disabled_early_check_in_does_not_propose_an_invented_time(client):
    c = client.app.state.container
    host = c.world.get_host()
    host.rules.early_check_in_earliest = None
    c.world.upsert_host(host)
    booking = say(client, "Could we check in early?")
    assert guest_requests(client) == []
    assert "None" not in replies(client, booking)[-1]["body"]
    assert "could not submit" in replies(client, booking)[-1]["body"]
