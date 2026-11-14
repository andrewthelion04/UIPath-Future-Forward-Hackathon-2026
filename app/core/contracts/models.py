"""Modelele de domeniu comune (contracte). Se modifică DOAR prin adăugare.

Toate ID-urile de aici sunt interne. ID-urile externe (PMS, Airbnb) nu apar în aceste modele.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class PmsProvider(StrEnum):
    MOCK = "mock"
    OWNERREZ = "ownerrez"
    HOSPITABLE = "hospitable"


class SourceChannel(StrEnum):
    AIRBNB = "airbnb"
    BOOKING_COM = "booking_com"
    VRBO = "vrbo"
    DIRECT = "direct"
    SIMULATED = "simulated"


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    CANCELLED = "cancelled"


# --- Host, proprietăți, rezervări, personal ----------------------------------------


class HostRules(BaseModel):
    """Regulile hostului. Limitele de aici se impun în cod, nu în prompt."""

    check_in_time: str = "15:00"
    check_out_time: str = "11:00"
    early_check_in_earliest: str | None = "12:00"  # mai devreme de atât nu se negociază
    late_check_out_latest: str | None = "13:00"
    pets_allowed: bool = False
    smoking_allowed: bool = False
    parties_allowed: bool = False
    quiet_hours: str | None = "22:00-08:00"
    last_minute_window_days: int = 7  # F5: o anulare e „de ultim moment” sub acest prag
    max_relist_discount_pct: float = 25.0  # F5: reducerea maximă față de prețul de bază
    house_notes: list[str] = Field(default_factory=list)


class Host(BaseModel):
    id: str
    name: str
    rules: HostRules = Field(default_factory=HostRules)


class Property(BaseModel):
    id: str
    name: str
    city: str = ""
    address: str = ""
    bedrooms: int = 1
    max_guests: int = 2
    amenities: list[str] = Field(default_factory=list)
    base_price: float = 0.0
    currency: str = "EUR"
    pms_provider: PmsProvider = PmsProvider.MOCK
    listing_url: str | None = None  # anunțul public al proprietății (poze, descriere), dacă există


class Booking(BaseModel):
    id: str
    property_id: str
    guest_name: str
    guest_count: int = 1
    check_in: date
    check_out: date
    status: BookingStatus = BookingStatus.CONFIRMED
    pms_provider: PmsProvider = PmsProvider.MOCK
    source_channel: SourceChannel = SourceChannel.SIMULATED
    total_price: float | None = None
    currency: str = "EUR"


class StaffMember(BaseModel):
    id: str
    name: str
    role: Literal["cleaner", "maintenance", "manager"]
    property_ids: list[str] = Field(default_factory=list)


# --- Calendar, recenzii, piață -----------------------------------------------------


class CalendarDay(BaseModel):
    property_id: str
    day: date
    available: bool
    price: float
    currency: str = "EUR"


class Review(BaseModel):
    id: str
    property_id: str
    rating: float = Field(ge=1, le=5)
    text: str
    author: str = ""
    reviewed_on: date | None = None
    source: str


class ReviewBatch(BaseModel):
    property_id: str
    source: str
    fetched_at: datetime
    reviews: list[Review] = Field(default_factory=list)


class ComparableQuery(BaseModel):
    city: str
    bedrooms: int
    max_guests: int
    amenities: list[str] = Field(default_factory=list)
    check_in: date
    check_out: date


class Comparable(BaseModel):
    """Anunț de pe piață. `ref` e o referință opacă a sursei, folosită doar la afișare."""

    ref: str
    title: str
    city: str = ""
    bedrooms: int = 1
    max_guests: int = 2
    amenities: list[str] = Field(default_factory=list)
    nightly_price: float
    currency: str = "EUR"
    source: str
    url: str | None = None


# --- Mesaje, notificări, aprobări --------------------------------------------------


class Message(BaseModel):
    id: str
    booking_id: str
    direction: Literal["inbound", "outbound"]
    sender: Literal["guest", "host", "copilot"]
    body: str
    status: Literal["received", "sent", "failed"]
    pms_provider: PmsProvider
    created_at: datetime


NotificationLevel = Literal["info", "success", "warning", "error", "action_required"]


class Notification(BaseModel):
    id: str
    feature: str
    level: NotificationLevel = "info"
    title: str
    body: str = ""
    booking_id: str | None = None
    property_id: str | None = None
    approval_id: str | None = None
    correlation_id: str | None = None
    read: bool = False
    created_at: datetime


class ApprovalOption(BaseModel):
    key: str
    label: str
    description: str = ""


class Approval(BaseModel):
    id: str
    feature: str
    kind: str
    title: str
    summary: str = ""
    options: list[ApprovalOption]
    payload: dict[str, Any] = Field(default_factory=dict)
    # `withdrawn`: retrasă de feature înainte de decizie (a rămas în urma realității).
    status: Literal["pending", "approved", "rejected", "withdrawn"] = "pending"
    chosen_option: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    booking_id: str | None = None
    property_id: str | None = None
    correlation_id: str | None = None
    dedupe_key: str | None = None
    created_at: datetime
    decided_at: datetime | None = None


# --- Subiecte deschise cu oaspetele ---------------------------------------------------


class GuestOffer(BaseModel):
    """O ofertă făcută oaspetelui, la care poate răspunde „accept” sau „decline”."""

    question: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "accepted", "declined", "withdrawn"] = "pending"
    expires_at: datetime | None = None


class GuestTopic(BaseModel):
    """Ce se întâmplă acum cu sejurul oaspetelui, spus de feature-ul care se ocupă.

    `facts` conține doar ce are voie să afle oaspetele (ex. o estimare aprobată de host).
    """

    id: str
    booking_id: str
    feature: str
    kind: str
    title: str
    facts: dict[str, Any] = Field(default_factory=dict)
    offer: GuestOffer | None = None
    status: Literal["open", "closed"] = "open"
    valid_until: date | None = None  # după această zi subiectul nu mai e arătat
    created_at: datetime
    updated_at: datetime


class ApprovalDecisionIn(BaseModel):
    """Corpul pentru `POST /approvals/{id}/decision`."""

    decision: Literal["approve", "reject"]
    option: str | None = None
    note: str | None = None
    decided_by: str = "host"
