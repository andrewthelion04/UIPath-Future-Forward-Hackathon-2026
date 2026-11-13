"""Evenimentele comune (contracte). Se modifică DOAR prin adăugare.

Aici stau evenimentele care trec granița dintre componente. Un eveniment folosit de un
singur feature poate fi definit în folderul acelui feature și mutat aici abia când îl
consumă altcineva.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.clock import utcnow
from app.core.contracts.models import PmsProvider, SourceChannel
from app.core.ids import new_id


class Event(BaseModel):
    """Baza tuturor evenimentelor. `correlation_id` leagă un flux întreg."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    occurred_at: datetime = Field(default_factory=utcnow)
    correlation_id: str | None = None
    causation_id: str | None = None
    source: str = "system"  # ex. "simulator", "webhook:hospitable", "feature:f3_guest_copilot"

    @classmethod
    def event_type(cls) -> str:
        return cls.__name__


# --- Mesagerie (F1 / ChannelGateway) -----------------------------------------------


class GuestMessageReceived(Event):
    """Un oaspete a scris pe o rezervare mapată. Același eveniment din simulator și din PMS."""

    message_id: str
    booking_id: str
    property_id: str
    text: str
    pms_provider: PmsProvider
    source_channel: SourceChannel


class GuestMessageSent(Event):
    """Un mesaj a plecat către oaspete prin `ChannelGateway`."""

    message_id: str
    booking_id: str
    property_id: str
    text: str
    sender: Literal["host", "copilot"]
    pms_provider: PmsProvider


# --- Declanșatoare de fluxuri ------------------------------------------------------


class IssueReported(Event):
    """O problemă la o proprietate, semnalată de personal sau de oaspete (prin F3)."""

    property_id: str
    booking_id: str | None = None
    description: str
    reported_via: Literal["staff", "guest"]
    reporter: str = ""
    severity: Literal["low", "medium", "high"] = "medium"


class BookingCancelled(Event):
    """O rezervare a trecut în starea `cancelled` (observat de `SyncService`)."""

    booking_id: str
    property_id: str
    check_in: date
    check_out: date
    pms_provider: PmsProvider
    source_channel: SourceChannel


class ReviewAnalysisRequested(Event):
    """Hostul cere analiza recenziilor. `property_id=None` înseamnă toate proprietățile."""

    property_id: str | None = None
    requested_by: str = "host"


# --- Human-in-the-loop -------------------------------------------------------------


class ApprovalRequested(Event):
    approval_id: str
    feature: str
    kind: str
    title: str


class ApprovalDecided(Event):
    """Decizia hostului. Ajunge doar la feature-ul care a cerut aprobarea."""

    approval_id: str
    feature: str
    kind: str
    decision: Literal["approved", "rejected"]
    option: str | None = None
    note: str | None = None
    decided_by: str = "host"
    payload: dict[str, Any] = Field(default_factory=dict)
