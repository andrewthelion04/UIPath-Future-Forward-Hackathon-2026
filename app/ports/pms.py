"""Porturile unei conexiuni PMS: proprietăți, rezervări și canalul de mesaje.

Înregistrările întoarse de `list_*` și `parse_webhook` poartă ID-ul extern doar ca
`SyncService` și pipeline-ul F1 să-l poată traduce prin `IdMapper`. Metodele apelate de
aplicație (`send_message`, calendarul) primesc ID-uri interne; adaptorul le traduce.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.core.contracts.models import BookingStatus, SourceChannel
from app.ports.calendar import CalendarPort


class PmsError(RuntimeError):
    """Eroare de transport sau de protocol la un apel către PMS."""


class CapabilityNotSupported(PmsError):
    """Conexiunea nu oferă operația cerută (ex. scrierea în calendar)."""


class UnmappedEntity(PmsError):
    """Entitatea internă nu are corespondent extern pe această conexiune."""


class PmsCapabilities(BaseModel):
    list_properties: bool = False
    list_reservations: bool = False
    send_messages: bool = False
    receive_webhooks: bool = False
    read_calendar: bool = False
    write_prices: bool = False
    write_availability: bool = False


class PmsProperty(BaseModel):
    external_id: str
    name: str
    city: str = ""
    address: str = ""
    bedrooms: int = 1
    max_guests: int = 2
    amenities: list[str] = Field(default_factory=list)
    base_price: float = 0.0
    currency: str = "EUR"


class PmsReservation(BaseModel):
    external_id: str
    property_external_id: str
    guest_name: str
    guest_count: int = 1
    check_in: date
    check_out: date
    status: BookingStatus = BookingStatus.CONFIRMED
    source_channel: SourceChannel = SourceChannel.DIRECT
    total_price: float | None = None
    currency: str = "EUR"


class InboundMessage(BaseModel):
    """Un mesaj extras dintr-un payload de webhook, încă neasociat unei rezervări interne."""

    external_message_id: str
    external_reservation_id: str | None = None
    sender_type: str  # "guest" | "host" | altceva; F1 procesează doar "guest"
    body: str
    sent_at: datetime | None = None


class SentMessage(BaseModel):
    external_message_id: str | None = None


@runtime_checkable
class PropertiesPort(Protocol):
    async def list_properties(self) -> list[PmsProperty]: ...


@runtime_checkable
class ReservationsPort(Protocol):
    async def list_reservations(self) -> list[PmsReservation]: ...


@runtime_checkable
class MessagingPort(Protocol):
    async def send_message(self, booking_id: str, body: str) -> SentMessage:
        """Trimite un mesaj oaspetelui rezervării (ID intern)."""
        ...

    def parse_webhook(self, payload: dict[str, Any]) -> InboundMessage | None:
        """Extrage mesajul dintr-un payload brut. None dacă payload-ul nu e un mesaj."""
        ...


@runtime_checkable
class PmsConnection(PropertiesPort, ReservationsPort, MessagingPort, CalendarPort, Protocol):
    """O conexiune PMS completă: implementează toate porturile și își declară capabilitățile."""

    provider: str
    capabilities: PmsCapabilities

    async def aclose(self) -> None: ...


class PmsRegistry:
    """Conexiunile PMS active, după numele providerului."""

    def __init__(self, connections: dict[str, PmsConnection] | None = None) -> None:
        self._connections: dict[str, PmsConnection] = dict(connections or {})

    def register(self, connection: PmsConnection) -> None:
        self._connections[connection.provider] = connection

    def get(self, provider: str) -> PmsConnection | None:
        return self._connections.get(str(provider))

    def providers(self) -> list[str]:
        return sorted(self._connections)

    def all(self) -> list[PmsConnection]:
        return [self._connections[name] for name in self.providers()]
