"""Evenimente private ale F2.

Stau aici cât timp le emite și le consumă doar F2. Dacă alt feature are nevoie de unul, se
mută în `app/core/contracts/events.py` (contractele comune), prin adăugare.

Pașii asincroni ai unui incident trec prin event bus (nu prin apeluri directe): așa moștenesc
corelarea, sunt izolați, apar în timeline și sunt așteptați de `?wait=true`.
"""

from __future__ import annotations

from typing import Literal

from app.core.contracts.events import Event


class IncidentDecided(Event):
    """Hostul a ales ce se întâmplă cu incidentul."""

    incident_id: str
    property_id: str
    booking_id: str | None = None
    option: Literal["remediate", "relocate"]


class IncidentResolved(Event):
    """Incidentul s-a încheiat: închidere aprobată de host sau respingere."""

    incident_id: str
    property_id: str
    booking_id: str | None = None
    resolution: Literal["remediation", "relocation", "dismissed"]


# --- Pași interni ---------------------------------------------------------------------


class StaffContactRequested(Event):
    """Incident nou: agentul trebuie să întrebe mentenanța cât durează reparația."""

    incident_id: str


class StaffReplied(Event):
    """Personalul a răspuns în conversația incidentului."""

    incident_id: str
    message_id: str


class SimStaffReplyDue(Event):
    """Simulare: personalul simulat trebuie să răspundă la ultima întrebare."""

    incident_id: str


class EstimateReady(Event):
    """Estimarea e cunoscută (sau definitiv necunoscută): se poate face propunerea."""

    incident_id: str


class GuestAnsweredRelocation(Event):
    """Oaspetele a răspuns la oferta de relocare."""

    incident_id: str
    choice: Literal["accept", "stay", "timeout"]
    simulated: bool = False


class SimGuestAnswerDue(Event):
    """Simulare: oaspetele simulat trebuie să răspundă la oferta de relocare."""

    incident_id: str


class IncidentTaskCompleted(Event):
    """O sarcină a personalului a fost marcată terminată."""

    incident_id: str
    task_id: str


class SimTaskDue(Event):
    """Simulare: personalul simulat trebuie să termine sarcina."""

    incident_id: str
    task_id: str


class IncidentTimeout(Event):
    """Un termen a expirat: personalul sau oaspetele nu a răspuns la timp."""

    incident_id: str
    kind: Literal["staff", "guest"]
