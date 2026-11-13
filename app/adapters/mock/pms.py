"""PMS-ul simulat: implementează toate porturile unei conexiuni PMS peste lumea mock.

Se comportă ca un PMS real: lucrează cu ID-uri externe proprii (`MOCK-...`), iar ID-urile
interne primite de la aplicație le traduce prin `IdMapper`. Tot el este și „lumea” pe care
acționează simulatorul (`SimulationPort`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.clock import utcnow
from app.core.contracts.models import CalendarDay
from app.core.id_mapper import PROPERTY, RESERVATION, IdMapper
from app.core.ids import new_id
from app.adapters.mock.world import MockWorld
from app.ports.pms import (
    InboundMessage,
    PmsCapabilities,
    PmsProperty,
    PmsReservation,
    SentMessage,
    UnmappedEntity,
)
from app.ports.simulation import SimulationError

PROVIDER = "mock"
MESSAGE_EVENT = "message.created"


class MockPmsAdapter:
    provider = PROVIDER
    capabilities = PmsCapabilities(
        list_properties=True,
        list_reservations=True,
        send_messages=True,
        receive_webhooks=True,
        read_calendar=True,
        write_prices=True,
        write_availability=True,
    )

    def __init__(self, world: MockWorld, ids: IdMapper) -> None:
        self._world = world
        self._ids = ids

    # --- Traducerea ID-urilor ------------------------------------------------------

    def _external(self, entity_type: str, internal_id: str) -> str:
        external_id = self._ids.to_external(PROVIDER, entity_type, internal_id)
        if external_id is None:
            raise UnmappedEntity(f"{entity_type} {internal_id} is not mapped on the mock PMS")
        return external_id

    # --- PropertiesPort / ReservationsPort -----------------------------------------

    async def list_properties(self) -> list[PmsProperty]:
        return [PmsProperty(**data) for data in self._world.properties()]

    async def list_reservations(self) -> list[PmsReservation]:
        return [PmsReservation(**data) for data in self._world.reservations()]

    # --- MessagingPort -------------------------------------------------------------

    async def send_message(self, booking_id: str, body: str) -> SentMessage:
        reservation_id = self._external(RESERVATION, booking_id)
        external_id = new_id("MOCK-MSG")
        self._world.add_message(external_id, reservation_id, "host", body, utcnow().isoformat())
        return SentMessage(external_message_id=external_id)

    def parse_webhook(self, payload: dict[str, Any]) -> InboundMessage | None:
        if payload.get("event") != MESSAGE_EVENT:
            return None
        message = payload.get("message")
        if not isinstance(message, dict) or not message.get("id"):
            return None
        return InboundMessage(
            external_message_id=str(message["id"]),
            external_reservation_id=message.get("reservation_id"),
            sender_type=str(message.get("sender_type", "")),
            body=str(message.get("body", "")),
            sent_at=message.get("sent_at"),
        )

    # --- CalendarPort --------------------------------------------------------------

    async def get_days(self, property_id: str, start: date, end: date) -> list[CalendarDay]:
        external_id = self._external(PROPERTY, property_id)
        currency = next(
            (p["currency"] for p in self._world.properties() if p["external_id"] == external_id),
            "EUR",
        )
        return [
            CalendarDay(
                property_id=property_id,
                day=row["day"],
                available=bool(row["available"]),
                price=row["price"],
                currency=currency,
            )
            for row in self._world.calendar_days(external_id, start, end)
        ]

    async def set_price(self, property_id: str, start: date, end: date, price: float) -> None:
        self._world.set_price(self._external(PROPERTY, property_id), start, end, price)

    async def set_availability(
        self, property_id: str, start: date, end: date, available: bool
    ) -> None:
        self._world.set_availability(self._external(PROPERTY, property_id), start, end, available)

    # --- SimulationPort ------------------------------------------------------------

    async def guest_message_payload(
        self, booking_id: str, text: str, sender_type: str = "guest"
    ) -> dict[str, Any]:
        try:
            reservation_id = self._external(RESERVATION, booking_id)
        except UnmappedEntity as exc:
            raise SimulationError(
                f"booking {booking_id} does not live on the simulated PMS"
            ) from exc
        external_id = new_id("MOCK-MSG")
        sent_at = utcnow().isoformat()
        self._world.add_message(external_id, reservation_id, sender_type, text, sent_at)
        return {
            "event": MESSAGE_EVENT,
            "message": {
                "id": external_id,
                "reservation_id": reservation_id,
                "sender_type": sender_type,
                "body": text,
                "sent_at": sent_at,
            },
        }

    async def cancel_reservation(self, booking_id: str) -> None:
        try:
            reservation_id = self._external(RESERVATION, booking_id)
        except UnmappedEntity as exc:
            raise SimulationError(
                f"booking {booking_id} does not live on the simulated PMS"
            ) from exc
        self._world.cancel_reservation(reservation_id)

    async def aclose(self) -> None:
        return None
