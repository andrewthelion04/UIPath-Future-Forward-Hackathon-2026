"""Politicile F3 sunt verificate direct în unelte, independent de buna voință a LLM-ului."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.agent.tools import RunContext
from app.core.channel_gateway import ChannelError
from app.core.contracts.models import BookingStatus, CalendarDay
from app.features.f3_guest_copilot.requests import (
    DECLINE_REPLY,
    GuestRequest,
    canonical_request,
    decision_reply,
    validate_approval,
)
from app.features.f3_guest_copilot.tools import build_tools
from app.ports.pms import PmsError


FEATURE = "f3_guest_copilot"


@dataclass
class Policy:
    c: object
    ctx: object
    tools: dict
    booking: object

    def run(self, booking=None, **state) -> RunContext:
        target = booking or self.booking
        return RunContext("run_policy", FEATURE, {
            "booking_id": target.id, "property_id": target.property_id, **state,
        })

    async def call(self, name, *, run=None, **arguments):
        tool = self.tools[name]
        return await tool.handler(tool.args_model(reason="Verify the policy", **arguments), run or self.run())

    async def approval(self, request=None, **arguments):
        return await self.call(
            "request_host_approval", request=request or {"request_type": "early_check_in"}, **arguments,
        )

    def decide(self, approval_id, status="approved"):
        self.c.db.execute(
            "UPDATE approvals SET status = ?, chosen_option = ? WHERE id = ?",
            (status, "approve" if status == "approved" else None, approval_id),
        )


@pytest.fixture
def policy(container):
    ctx = container._make_feature_context(FEATURE)
    booking = next(item for item in container.world.list_bookings() if item.guest_name == "Emma Johnson")
    return Policy(container, ctx, {tool.name: tool for tool in build_tools(ctx)}, booking)


@pytest.mark.parametrize("kind, supplied, expected", [
    ("early_check_in", "12:00", "12:00:00"),
    ("early_check_in", "14:59", "14:59:00"),
    ("late_check_out", "11:01", "11:01:00"),
    ("late_check_out", "13:00", "13:00:00"),
])
def test_time_requests_use_booking_dates_and_host_limits(policy, kind, supplied, expected):
    canonical = canonical_request(policy.ctx, policy.booking.id, GuestRequest(
        request_type=kind, requested_time=supplied,
    ))
    target = policy.booking.check_out if kind == "late_check_out" else policy.booking.check_in
    assert canonical.requested_date == target
    assert canonical.canonical_data()["requested_time"] == expected


@pytest.mark.parametrize("request_data, error", [
    ({"request_type": "early_check_in", "requested_time": "11:59"}, "outside"),
    ({"request_type": "early_check_in", "requested_time": "15:00"}, "outside"),
    ({"request_type": "late_check_out", "requested_time": "11:00"}, "outside"),
    ({"request_type": "late_check_out", "requested_time": "13:01"}, "outside"),
    ({"request_type": "early_check_in", "requested_time": "12:00:01"}, "minute precision"),
    ({"request_type": "early_check_in", "requested_time": "12:00:00+02:00"}, "local time"),
    ({"request_type": "early_check_in", "requested_date": "2026-06-04"}, "date does not match"),
    ({"request_type": "late_check_out", "requested_date": "2026-06-03"}, "date does not match"),
    ({"request_type": "early_check_in", "guest_count": 3}, "counts do not belong"),
    ({"request_type": "late_check_out", "pet_count": 1}, "counts do not belong"),
    ({"request_type": "extra_guests", "guest_count": 3, "requested_time": "12:00"}, "time does not belong"),
    ({"request_type": "extra_guests", "guest_count": 3, "pet_count": 1}, "without pet_count"),
    ({"request_type": "extra_guests"}, "total guest_count"),
    ({"request_type": "extra_guests", "guest_count": 2}, "must exceed"),
    ({"request_type": "extra_guests", "guest_count": 3}, "exceeds the property capacity"),
    ({"request_type": "pets", "guest_count": 1}, "does not belong"),
])
async def test_invalid_requests_cannot_create_approvals(policy, request_data, error):
    with pytest.raises(ValueError, match=error):
        await policy.approval(request_data)
    assert policy.c.approvals.list() == []


@pytest.mark.parametrize("kind, field", [
    ("early_check_in", "early_check_in_earliest"),
    ("late_check_out", "late_check_out_latest"),
])
async def test_none_time_limit_disables_the_exception(policy, kind, field):
    host = policy.c.world.get_host()
    host.rules = host.rules.model_copy(update={field: None})
    policy.c.world.upsert_host(host)
    with pytest.raises(ValueError, match="disabled"):
        await policy.approval({"request_type": kind})
    assert policy.c.approvals.list() == []


@pytest.mark.parametrize("request_data", [
    {"request_type": "discount"},
    {"request_type": "pets", "pet_count": 0},
    {"request_type": "pets", "pet_count": True},
    {"request_type": "extra_guests", "guest_count": "3"},
    {"request_type": "early_check_in", "requested_time": "25:00"},
    {"request_type": "pets", "approved": True},
])
def test_request_schema_rejects_invalid_types_and_unknown_permissions(request_data):
    with pytest.raises(ValidationError):
        GuestRequest.model_validate(request_data)


async def test_extra_guests_within_capacity_require_an_exact_approval(policy):
    sofia = next(item for item in policy.c.world.list_bookings() if item.guest_name == "Sofia Rossi")
    request = {"request_type": "extra_guests", "guest_count": 5}
    result = await policy.call("request_host_approval", run=policy.run(sofia), request=request)
    approval = policy.c.approvals.get(result["approval_id"])
    assert result["status"] == "pending" and approval.booking_id == sofia.id
    assert approval.payload["request"]["guest_count"] == 5
    assert "5 guests" in approval.payload["proposed_reply"]
    assert policy.c.world.get_booking(sofia.id).guest_count == 4


async def test_pets_are_negotiable_only_through_explicit_host_approval(policy):
    assert policy.c.world.host_rules().pets_allowed is False
    result = await policy.approval({"request_type": "pets"})
    assert result["status"] == "pending" and result["request"]["pet_count"] == 1
    assert policy.c.conversations.history(policy.booking.id) == []
    policy.decide(result["approval_id"])
    approved = await validate_approval(policy.ctx, result["approval_id"], policy.booking.id)
    assert "1 pet(s)" in approved.payload["proposed_reply"]


@pytest.mark.parametrize("request_type", ["early_check_in", "pets", "extra_guests"])
async def test_past_arrival_or_finished_stay_cannot_get_new_permission(policy, request_type):
    target_name = "Lukas Mueller" if request_type == "early_check_in" else "Daniel Kim"
    booking = next(item for item in policy.c.world.list_bookings() if item.guest_name == target_name)
    parameters = {"request_type": request_type}
    if request_type == "extra_guests":
        parameters["guest_count"] = 3
    with pytest.raises(ValueError, match="already passed|stay has ended"):
        await policy.call("request_host_approval", run=policy.run(booking), request=parameters)
    assert policy.c.approvals.list() == []


@pytest.mark.parametrize("status", [BookingStatus.CANCELLED, BookingStatus.PENDING])
async def test_unconfirmed_bookings_cannot_create_exception_approvals(policy, status):
    policy.c.world.upsert_booking(policy.booking.model_copy(update={"status": status}))
    with pytest.raises(ValueError, match="confirmed booking"):
        await policy.approval()
    assert policy.c.approvals.list() == []


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
async def test_semantically_identical_requests_reuse_id_despite_wording_or_time_format(policy, status):
    first = await policy.approval(
        {"request_type": "early_check_in", "requested_time": "12:00"},
        request_summary="Could we arrive at noon?", proposed_reply="An untrusted proposed promise",
    )
    policy.decide(first["approval_id"], status)
    second = await policy.approval(
        {"request_type": "early_check_in", "requested_date": policy.booking.check_in.isoformat(),
         "requested_time": "12:00:00"},
        request_summary="We would like an early check-in at midday.", proposed_reply="A different untrusted text",
    )
    assert second["approval_id"] == first["approval_id"]
    assert second["reused"] is True and second["status"] == status
    assert "untrusted" not in second["proposed_reply"]
    assert len(policy.c.approvals.list(status="all")) == 1
    events = [event for event in policy.c.bus.list_events() if event["type"] == "ApprovalRequested"]
    assert len(events) == 1


async def test_changed_time_or_request_type_creates_a_distinct_approval(policy):
    first = await policy.approval({"request_type": "early_check_in", "requested_time": "12:00"})
    changed = await policy.approval({"request_type": "early_check_in", "requested_time": "12:30"})
    other = await policy.approval({"request_type": "late_check_out", "requested_time": "12:00"})
    assert len({first["approval_id"], changed["approval_id"], other["approval_id"]}) == 3


@pytest.mark.parametrize("text", [
    "Your early check-in is confirmed for 12:00.",
    "You can arrive early at noon.",
    "Your dog is welcome.",
    "An extra guest is approved.",
    "We guarantee a refund.",
    "The host will confirm later, but your early check-in is confirmed for 12:00.",
    "Your early check-in is not confirmed, but you can arrive at 12:00.",
    "Early check-in is not confirmed, but arriving at 12:00 works.",
    "We cannot accommodate a late check-out, but arrive early at 12:00.",
    "Puteți veni devreme la 12:00.",
    "Check-inul devreme este confirmat pentru ora 12:00.",
    "Da, veniți mai devreme la 12:00.",
    "You can check in at 11:00.",
    "Check-out is confirmed for 15:00.",
])
async def test_unapproved_promises_are_blocked_before_channel_delivery(policy, text):
    run = policy.run()
    with pytest.raises(ValueError):
        await policy.call("send_guest_reply", run=run, text=text)
    assert run.state.get("replies_sent", 0) == 0
    assert policy.c.conversations.history(policy.booking.id) == []


@pytest.mark.parametrize("text", [
    "Your early check-in has not yet been confirmed. The host will confirm.",
    "I have passed your request to the host and will confirm when I hear back.",
    "Unfortunately pets are not allowed under the current house rules.",
    "Check-in is from 15:00 and check-out is by 11:00.",
])
async def test_pending_declined_and_standard_rule_messages_can_be_sent(policy, text):
    result = await policy.call("send_guest_reply", text=text)
    assert result["sent"] is True
    assert policy.c.conversations.history(policy.booking.id)[-1].body == text


async def test_exact_typed_approved_confirmation_can_be_sent(policy):
    result = await policy.approval()
    policy.decide(result["approval_id"])
    run = policy.run()
    sent = await policy.call(
        "send_guest_reply", run=run, text=result["proposed_reply"],
        approval_id=result["approval_id"], request=result["request"],
    )
    assert sent["sent"] is True and run.state["replies_sent"] == 1
    assert policy.c.conversations.history(policy.booking.id)[-1].body == result["proposed_reply"]


@pytest.mark.parametrize("mutation", ["pending", "rejected", "wrong_booking", "wrong_type", "wrong_time", "wrong_text", "no_request", "no_id"])
async def test_only_the_exact_approved_scope_and_reviewed_text_can_be_sent(policy, mutation):
    result = await policy.approval()
    policy.decide(result["approval_id"])
    request = dict(result["request"])
    text = result["proposed_reply"]
    run = policy.run()
    arguments = {"approval_id": result["approval_id"], "request": request}
    if mutation in ("pending", "rejected"):
        policy.decide(result["approval_id"], mutation)
    elif mutation == "wrong_booking":
        other = next(item for item in policy.c.world.list_bookings() if item.guest_name == "Sofia Rossi")
        run = policy.run(other)
    elif mutation == "wrong_type":
        arguments["request"] = {"request_type": "pets"}
    elif mutation == "wrong_time":
        arguments["request"] = {"request_type": "early_check_in", "requested_time": "12:30"}
    elif mutation == "wrong_text":
        text += " You may also bring your dog."
    elif mutation == "no_request":
        arguments.pop("request")
    else:
        arguments.pop("approval_id")
    with pytest.raises(ValueError):
        await policy.call("send_guest_reply", run=run, text=text, **arguments)
    assert run.state.get("replies_sent", 0) == 0
    assert not any(message.direction == "outbound" for message in policy.c.conversations.history(policy.booking.id))


@pytest.mark.parametrize("column, value", [
    ("feature", "f2_incidents"),
    ("kind", "other_kind"),
    ("property_id", "prop_other"),
    ("dedupe_key", "different-scope"),
    ("chosen_option", "not_approve"),
])
async def test_unrelated_or_altered_persisted_approval_cannot_authorize_a_reply(policy, column, value):
    result = await policy.approval()
    policy.decide(result["approval_id"])
    # Numele coloanei vine exclusiv din lista fixă a parametrizării testului.
    policy.c.db.execute(f"UPDATE approvals SET {column} = ? WHERE id = ?", (value, result["approval_id"]))
    with pytest.raises(ValueError):
        await policy.call(
            "send_guest_reply", text=result["proposed_reply"],
            approval_id=result["approval_id"], request=result["request"],
        )
    assert policy.c.conversations.history(policy.booking.id) == []


@pytest.mark.parametrize("change", ["cancelled", "pending", "check_in", "check_out", "property", "guest_count", "time_rule", "guest_name"])
async def test_changed_booking_or_host_time_conditions_invalidate_existing_approval(policy, change):
    result = await policy.approval()
    policy.decide(result["approval_id"])
    booking = policy.booking
    if change in ("cancelled", "pending"):
        booking = booking.model_copy(update={"status": BookingStatus(change)})
    elif change in ("check_in", "check_out"):
        booking = booking.model_copy(update={change: getattr(booking, change) + timedelta(days=1)})
    elif change == "property":
        other = next(item for item in policy.c.world.list_properties() if item.id != booking.property_id)
        booking = booking.model_copy(update={"property_id": other.id})
    elif change == "guest_count":
        booking = booking.model_copy(update={"guest_count": 1})
    elif change == "guest_name":
        booking = booking.model_copy(update={"guest_name": "Another Guest"})
    elif change == "time_rule":
        host = policy.c.world.get_host()
        host.rules.early_check_in_earliest = "11:30"
        policy.c.world.upsert_host(host)
    policy.c.world.upsert_booking(booking)
    with pytest.raises(ValueError):
        await validate_approval(policy.ctx, result["approval_id"], policy.booking.id)
    assert policy.c.conversations.history(policy.booking.id) == []


async def test_capacity_change_invalidates_an_extra_guest_approval(policy):
    sofia = next(item for item in policy.c.world.list_bookings() if item.guest_name == "Sofia Rossi")
    result = await policy.call("request_host_approval", run=policy.run(sofia), request={"request_type": "extra_guests", "guest_count": 5})
    policy.decide(result["approval_id"])
    prop = policy.c.world.get_property(sofia.property_id)
    policy.c.world.upsert_property(prop.model_copy(update={"max_guests": 4}))
    with pytest.raises(ValueError, match="capacity"):
        await validate_approval(policy.ctx, result["approval_id"], sofia.id)


async def test_pets_rule_change_invalidates_the_old_reviewed_scope(policy):
    result = await policy.approval({"request_type": "pets"})
    policy.decide(result["approval_id"])
    host = policy.c.world.get_host()
    host.rules.pets_allowed = True
    policy.c.world.upsert_host(host)
    with pytest.raises(ValueError, match="conditions have changed"):
        await validate_approval(policy.ctx, result["approval_id"], policy.booking.id)


@pytest.mark.parametrize("mode", ["empty", "booked", "wrong_property", "wrong_date", "duplicate", "error"])
async def test_missing_occupied_or_failed_calendar_prevents_confirmation(policy, monkeypatch, mode):
    result = await policy.approval()
    policy.decide(result["approval_id"])
    night = policy.booking.check_in - timedelta(days=1)

    async def calendar(property_id, start, end):
        if mode == "error":
            raise PmsError("calendar unavailable")
        if mode == "empty":
            return []
        day = CalendarDay(
            property_id="prop_other" if mode == "wrong_property" else property_id,
            day=night + timedelta(days=1) if mode == "wrong_date" else night,
            available=mode != "booked", price=70,
        )
        return [day, day] if mode == "duplicate" else [day]

    monkeypatch.setattr(policy.c.calendar, "get_days", calendar)
    with pytest.raises(ValueError, match="occupied or unknown"):
        await validate_approval(policy.ctx, result["approval_id"], policy.booking.id)
    with pytest.raises(ValueError):
        await policy.call("send_guest_reply", text=result["proposed_reply"], approval_id=result["approval_id"], request=result["request"])
    assert policy.c.conversations.history(policy.booking.id) == []


async def test_rejection_does_not_require_an_available_calendar(policy, monkeypatch):
    result = await policy.approval()
    policy.decide(result["approval_id"], "rejected")

    async def calendar(*args):
        raise AssertionError("a rejection should not query the calendar")

    monkeypatch.setattr(policy.c.calendar, "get_days", calendar)
    assert await decision_reply(policy.ctx, result["approval_id"], policy.booking.id, "rejected") == DECLINE_REPLY


async def test_missing_calendar_is_explicit_in_the_hosts_pending_request(policy, monkeypatch):
    async def calendar(*args):
        raise PmsError("calendar unavailable")

    monkeypatch.setattr(policy.c.calendar, "get_days", calendar)
    result = await policy.approval()
    approval = policy.c.approvals.get(result["approval_id"])
    assert result["status"] == "pending"
    assert approval.payload["calendar_evidence"]["state"] == "unknown"
    assert "unknown" in approval.summary and "free" not in approval.summary
    assert policy.c.conversations.history(policy.booking.id) == []


@pytest.mark.parametrize("mode", ["empty", "duplicate", "wrong_property"])
async def test_calendar_tool_does_not_present_partial_data_as_available(policy, monkeypatch, mode):
    night = policy.booking.check_in - timedelta(days=1)

    async def calendar(property_id, start, end):
        day = CalendarDay(
            property_id="prop_other" if mode == "wrong_property" else property_id,
            day=start, available=True, price=70,
        )
        return [] if mode == "empty" else ([day, day] if mode == "duplicate" else [day])

    monkeypatch.setattr(policy.c.calendar, "get_days", calendar)
    with pytest.raises(ValueError, match="incomplete"):
        await policy.call("check_calendar", start_date=night, end_date=night + timedelta(days=1))


async def test_booking_context_filters_approvals_before_global_limits(policy):
    relevant = await policy.approval()
    decided = await policy.approval({"request_type": "pets"})
    policy.decide(decided["approval_id"], "rejected")
    other = next(item for item in policy.c.world.list_bookings() if item.id != policy.booking.id)
    for index in range(205):
        await policy.c.approvals.request(
            feature="f2_incidents" if index % 3 == 0 else FEATURE,
            kind="other_kind" if index % 3 == 1 else "guest_request",
            title=f"Unrelated approval {index}",
            booking_id=other.id if index % 3 == 2 else policy.booking.id,
        )
    context = await policy.call("get_booking_context")
    assert {item["approval_id"] for item in context["pending_approvals"]} == {relevant["approval_id"]}
    assert {item["approval_id"] for item in context["guest_requests"]} == {relevant["approval_id"], decided["approval_id"]}
    assert context["booking"]["guest_name"] == "Emma Johnson"


async def test_conversation_omits_queued_future_inbound_and_keeps_previous_reply(policy):
    def record(body, direction="inbound"):
        return policy.c.conversations.record(
            booking_id=policy.booking.id, direction=direction,
            sender="guest" if direction == "inbound" else "copilot",
            body=body, pms_provider=policy.booking.pms_provider,
            status="received" if direction == "inbound" else "sent",
        )

    record("Earlier guest question")
    current = record("Current guest question")
    record("Queued future guest question")
    record("Reply to the earlier question", direction="outbound")
    result = await policy.call("get_conversation", run=policy.run(message_id=current.id))
    assert [item["text"] for item in result] == [
        "Earlier guest question", "Current guest question", "Reply to the earlier question",
    ]


async def test_channel_failure_notifies_host_without_consuming_reply_limit(policy, monkeypatch):
    connection = policy.c.pms.get("mock")
    original = connection.send_message
    run = policy.run()

    async def fail(*args):
        raise PmsError("PMS message transport unavailable")

    monkeypatch.setattr(connection, "send_message", fail)
    with pytest.raises(ChannelError):
        await policy.call("send_guest_reply", run=run, text="Thank you for your message.")
    assert run.state.get("replies_sent", 0) == 0
    assert any("could not be sent" in item.title for item in policy.c.notifications.list())
    assert [message.status for message in policy.c.conversations.history(policy.booking.id)] == ["failed"]
    assert not any(event["type"] == "GuestMessageSent" for event in policy.c.bus.list_events())
    monkeypatch.setattr(connection, "send_message", original)
    await policy.call("send_guest_reply", run=run, text="Thank you for your message.")
    assert run.state["replies_sent"] == 1
    assert [message.status for message in policy.c.conversations.history(policy.booking.id)] == ["failed", "sent"]


async def test_missing_facts_can_request_host_follow_up_after_a_safe_reply(policy):
    await policy.call(
        "send_guest_reply", text="The requested details are unavailable. The host will follow up.",
        host_follow_up=True,
    )
    (notification,) = policy.c.notifications.list()
    assert notification.title == "Guest request needs manual follow-up"
    assert notification.booking_id == policy.booking.id
    assert notification.property_id == policy.booking.property_id


def change_conditions_during_calendar_io(policy, change):
    if change == "cancelled":
        policy.c.world.upsert_booking(policy.booking.model_copy(update={"status": BookingStatus.CANCELLED}))
    else:
        host = policy.c.world.get_host()
        host.rules.early_check_in_earliest = "11:30"
        policy.c.world.upsert_host(host)


@pytest.mark.parametrize("change", ["cancelled", "rules"])
async def test_approval_is_revalidated_after_calendar_io_before_confirmation(policy, monkeypatch, change):
    result = await policy.approval()
    policy.decide(result["approval_id"])
    calendar_called = False

    async def calendar(property_id, start, end):
        nonlocal calendar_called
        calendar_called = True
        # Simulează un sync sau o schimbare de reguli în timpul citirii externe.
        change_conditions_during_calendar_io(policy, change)
        return [CalendarDay(property_id=property_id, day=start, available=True, price=70)]

    monkeypatch.setattr(policy.c.calendar, "get_days", calendar)
    with pytest.raises(ValueError):
        await validate_approval(policy.ctx, result["approval_id"], policy.booking.id)
    assert calendar_called is True
    assert policy.c.conversations.history(policy.booking.id) == []


@pytest.mark.parametrize("change", ["cancelled", "rules"])
async def test_conditions_changing_during_calendar_io_do_not_create_a_stale_approval(policy, monkeypatch, change):
    calendar_called = False

    async def calendar(property_id, start, end):
        nonlocal calendar_called
        calendar_called = True
        change_conditions_during_calendar_io(policy, change)
        return [CalendarDay(property_id=property_id, day=start, available=True, price=70)]

    monkeypatch.setattr(policy.c.calendar, "get_days", calendar)
    with pytest.raises(ValueError):
        await policy.approval()
    assert calendar_called is True
    assert policy.c.approvals.list(status="all") == []
    assert not any(event["type"] == "ApprovalRequested" for event in policy.c.bus.list_events())


async def test_renamed_guest_can_obtain_a_new_reviewed_approval(policy):
    original = await policy.approval()
    policy.decide(original["approval_id"])
    policy.c.world.upsert_booking(policy.booking.model_copy(update={"guest_name": "Ema Johnson"}))
    with pytest.raises(ValueError):
        await validate_approval(policy.ctx, original["approval_id"], policy.booking.id)
    replacement = await policy.approval()
    assert replacement["approval_id"] != original["approval_id"]
    assert replacement["status"] == "pending" and replacement["reused"] is False
    assert "Good news, Ema!" in replacement["proposed_reply"]
    assert "Good news, Emma!" in original["proposed_reply"]
