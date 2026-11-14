"""Ieșirea către oaspeți: rutează mesajul prin conexiunea PMS a rezervării."""

from __future__ import annotations

import logging
from typing import Literal

from app.core.contracts.events import GuestMessageSent
from app.core.contracts.models import Message
from app.core.conversation_log import ConversationLog
from app.core.event_bus import EventBus
from app.core.notifications import NotificationService
from app.core.world_repo import WorldRepository
from app.ports.pms import PmsRegistry

logger = logging.getLogger("staypilot.channel")


class ChannelError(RuntimeError):
    """Mesajul nu a putut fi trimis oaspetelui."""


class ChannelGateway:
    def __init__(
        self,
        connections: PmsRegistry,
        world: WorldRepository,
        conversations: ConversationLog,
        bus: EventBus,
        notifications: NotificationService,
    ) -> None:
        self._connections = connections
        self._world = world
        self._conversations = conversations
        self._bus = bus
        self._notifications = notifications

    async def send_to_guest(
        self,
        booking_id: str,
        text: str,
        *,
        sender: Literal["host", "copilot"] = "copilot",
        feature: str = "core",
    ) -> Message:
        booking = self._world.get_booking(booking_id)
        if booking is None:
            raise ChannelError(f"unknown booking {booking_id}")
        connection = self._connections.get(booking.pms_provider)
        try:
            if connection is None:
                raise ChannelError(f"PMS connection '{booking.pms_provider}' is not enabled")
            if not connection.capabilities.send_messages:
                raise ChannelError(f"PMS connection '{booking.pms_provider}' cannot send messages")
            await connection.send_message(booking_id, text)
        except Exception as exc:
            logger.warning("Sending to guest failed for %s: %s", booking_id, exc)
            self._conversations.record(
                booking_id=booking_id,
                direction="outbound",
                sender=sender,
                body=text,
                pms_provider=booking.pms_provider,
                status="failed",
            )
            self._notifications.notify(
                feature=feature,
                level="error",
                title=f"Message to {booking.guest_name} could not be sent",
                body=f"Reason: {exc}\n\nUndelivered text:\n{text}",
                booking_id=booking_id,
                property_id=booking.property_id,
            )
            if isinstance(exc, ChannelError):
                raise
            raise ChannelError(str(exc)) from exc

        message = self._conversations.record(
            booking_id=booking_id,
            direction="outbound",
            sender=sender,
            body=text,
            pms_provider=booking.pms_provider,
            status="sent",
        )
        if sender == "copilot":
            prop = self._world.get_property(booking.property_id)
            self._notifications.notify(
                feature=feature,
                level="info",
                title=f"New automated answer by Agent in chat with {booking.guest_name}",
                body=f"{prop.name if prop else 'Property'} · {booking.pms_provider.value}",
                booking_id=booking.id,
                property_id=booking.property_id,
            )
        await self._bus.publish(
            GuestMessageSent(
                message_id=message.id,
                booking_id=booking_id,
                property_id=booking.property_id,
                text=text,
                sender=sender,
                pms_provider=booking.pms_provider,
                source=f"feature:{feature}",
            )
        )
        return message
