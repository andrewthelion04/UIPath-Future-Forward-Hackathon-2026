"""Modelele F2: incidentul, rapoartele, conversația cu personalul și sarcinile."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

IncidentStatus = Literal[
    "awaiting_staff",  # agentul așteaptă estimarea mentenanței
    "awaiting_decision",  # aprobarea `incident_resolution` e la host
    "awaiting_guest",  # oferta de relocare a plecat; oaspetele alege
    "remediation_scheduled",
    "relocated",
    "dismissed",
    "resolved",
]
CLOSED_STATUSES: frozenset[str] = frozenset({"dismissed", "resolved"})

OPTION_REMEDIATE = "remediate"
OPTION_RELOCATE = "relocate"


class GuestDrafts(BaseModel):
    """Mesajele către oaspete, scrise înainte de decizie și arătate exact în aprobare."""

    relocation_offer: str | None = None
    relocation_accepted: str | None = None
    relocation_declined: str | None = None  # scuze + estimare, oaspetele rămâne
    fix_apology: str | None = None  # varianta „remediere”; None = fără mesaj


class Incident(BaseModel):
    id: str
    property_id: str
    booking_id: str | None = None
    description: str
    reported_via: str
    reporter: str = ""
    severity: str = "medium"
    status: IncidentStatus
    relocation_property_id: str | None = None
    approval_id: str | None = None
    resolution: str | None = None
    created_at: datetime
    updated_at: datetime
    correlation_id: str | None = None
    report_count: int = 1
    contact_staff_id: str | None = None
    contact_staff_name: str | None = None
    staff_questions: int = 0
    staff_deadline: datetime | None = None
    eta_hours: float | None = None
    eta_source: str | None = None  # staff | unknown | timeout | no_staff
    eta_quote: str | None = None
    eta_confidence: str | None = None
    in_house: bool | None = None
    hours_until_check_in: float | None = None
    recommendation: str | None = None
    recommendation_reason: str | None = None
    drafts: GuestDrafts | None = None
    drafts_source: str | None = None  # llm | template
    proposal_round: int = 0
    decided_option: str | None = None
    guest_answer: str | None = None
    guest_deadline: datetime | None = None
    closure_round: int = 0
    closure_approval_id: str | None = None
    closure_message: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED_STATUSES


class IncidentReport(BaseModel):
    id: str
    incident_id: str
    description: str
    reported_via: str
    reporter: str = ""
    severity: str = "medium"
    booking_id: str | None = None
    created_at: datetime


class StaffMessage(BaseModel):
    id: str
    incident_id: str
    staff_id: str | None = None
    staff_name: str = ""
    direction: Literal["to_staff", "from_staff"]
    body: str
    simulated: bool = False
    created_at: datetime


class StaffTask(BaseModel):
    id: str
    incident_id: str
    staff_id: str | None = None
    staff_name: str | None = None
    description: str
    status: Literal["open", "done"]
    completion_note: str | None = None
    completed_by: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class IncidentView(Incident):
    """Răspunsul `GET /incidents`: incidentul cu tot istoricul lui (UI, extensie)."""

    reports: list[IncidentReport] = Field(default_factory=list)
    staff_messages: list[StaffMessage] = Field(default_factory=list)
    tasks: list[StaffTask] = Field(default_factory=list)
