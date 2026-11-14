"""ChannelGateway: mesajul pleacă prin conexiunea PMS a rezervării."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.core.channel_gateway import ChannelError
from app.core.contracts.models import Booking, PmsProvider, Property
from app.ports.pms import PmsCapabilities, PmsError, SentMessage


class RecordingPms:
    provider = "hospitable"
    capabilities = PmsCapabilities(send_messages=True)

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send_message(self, booking_id: str, body: str) -> SentMessage:
        if self.fail:
            raise PmsError("HTTP 503 from PMS")
        self.sent.append((booking_id, body))
        return SentMessage(external_message_id="ext-1")

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def add_hospitable_booking(container) -> Booking:
    container.world.upsert_property(
        Property(id="prop_real", name="Real Flat", pms_provider=PmsProvider.HOSPITABLE)
    )
    booking = Booking(
        id="bk_real",
        property_id="prop_real",
        guest_name="Real Guest",
        check_in=date(2026, 7, 1),
        check_out=date(2026, 7, 3),
        pms_provider=PmsProvider.HOSPITABLE,
    )
    container.world.upsert_booking(booking)
    return booking


def mock_booking(container) -> Booking:
    return next(b for b in container.world.list_bookings() if b.guest_name == "Emma Johnson")


async def test_message_is_sent_logged_and_announced(container):
    booking = mock_booking(container)

    message = await container.channel.send_to_guest(booking.id, "Welcome!", feature="test")

    assert (message.direction, message.status, message.sender) == ("outbound", "sent", "copilot")
    assert [m.body for m in container.conversations.history(booking.id)] == ["Welcome!"]
    event = next(e for e in container.bus.list_events() if e["type"] == "GuestMessageSent")
    assert event["payload"]["booking_id"] == booking.id and event["source"] == "feature:test"
    (notification,) = container.notifications.list()
    assert notification.level == "info"
    assert notification.title == "New automated answer by Agent in chat with Emma Johnson"
    assert notification.booking_id == booking.id
    assert notification.property_id == booking.property_id
    assert "Welcome!" not in notification.body


async def test_host_written_reply_does_not_create_an_automated_answer_notification(container):
    booking = mock_booking(container)
    await container.channel.send_to_guest(booking.id, "The Wi-Fi password is demo.", sender="host")
    assert container.notifications.list() == []


async def test_message_is_routed_through_the_bookings_own_connection(container):
    real = RecordingPms()
    container.pms.register(real)
    real_booking, simulated = add_hospitable_booking(container), mock_booking(container)

    await container.channel.send_to_guest(real_booking.id, "via hospitable")
    await container.channel.send_to_guest(simulated.id, "via mock")

    assert real.sent == [("bk_real", "via hospitable")]  # doar mesajul rezervării lui


async def test_send_failure_is_recorded_and_the_host_is_notified(container):
    container.pms.register(RecordingPms(fail=True))
    booking = add_hospitable_booking(container)

    with pytest.raises(ChannelError, match="503"):
        await container.channel.send_to_guest(booking.id, "lost message", feature="test")

    assert [m.status for m in container.conversations.history(booking.id)] == ["failed"]
    notification = container.notifications.list()[0]
    assert notification.level == "error" and "lost message" in notification.body
    assert not any(n.level == "info" for n in container.notifications.list())
    assert not [e for e in container.bus.list_events() if e["type"] == "GuestMessageSent"]


async def test_booking_on_a_disabled_connection_cannot_be_messaged(container):
    booking = add_hospitable_booking(container)  # conexiunea hospitable nu e înregistrată
    with pytest.raises(ChannelError, match="not enabled"):
        await container.channel.send_to_guest(booking.id, "hello")
    with pytest.raises(ChannelError, match="unknown booking"):
        await container.channel.send_to_guest("bk_missing", "hello")
