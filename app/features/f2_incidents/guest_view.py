"""Ce are voie să afle oaspetele despre un incident, în fiecare stare a lui.

F2 publică rezultatul prin `ctx.guest_topics`, iar cine vorbește cu oaspetele (copilotul)
răspunde din aceste fapte. Regula: aici intră doar ce a aprobat deja hostul (estimarea din
mesajul aprobat, oferta aprobată, mutarea confirmată) sau ce e pur informativ („echipa
evaluează”). Nimic din ce e încă propunere nu ajunge la oaspete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.contracts.models import Booking, GuestOffer, Property
from app.features.f2_incidents.models import OPTION_RELOCATE, GuestDrafts, Incident
from app.features.f2_incidents.rules import friendly_hours, problem_phrase

TOPIC_KIND = "incident"


@dataclass
class GuestView:
    title: str
    facts: dict[str, Any]
    offer: GuestOffer | None = None
    valid_until: date | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def topic_id(feature: str, incident_id: str) -> str:
    return f"{feature}:{TOPIC_KIND}:{incident_id}"


def incident_id_of(feature: str, topic: str) -> str | None:
    prefix = f"{feature}:{TOPIC_KIND}:"
    return topic.removeprefix(prefix) if topic.startswith(prefix) else None


def place(prop: Property) -> dict[str, Any]:
    """Detaliile unei proprietăți pe care oaspetele le poate primi."""
    details: dict[str, Any] = {
        "name": prop.name,
        "address": prop.address,
        "bedrooms": prop.bedrooms,
        "sleeps": prop.max_guests,
        "amenities": prop.amenities,
    }
    if prop.listing_url:
        details["listing_url"] = prop.listing_url  # anunțul cu poze și descriere
    return details


def _approved_estimate(incident: Incident) -> str | None:
    """Estimarea, doar dacă oaspetele a primit-o într-un mesaj aprobat de host."""
    drafts = incident.drafts or GuestDrafts()
    told = drafts.relocation_declined if incident.decided_option == OPTION_RELOCATE else drafts.fix_apology
    if not told:
        return None
    return friendly_hours(incident.eta_hours) if incident.eta_hours else "as soon as possible"


def guest_view(
    incident: Incident, booking: Booking | None, property_name: str, target: Property | None, today: date
) -> GuestView | None:
    """Starea incidentului pentru oaspete; None = nu (mai) există nimic de spus."""
    if booking is None:
        return None
    moved = incident.guest_answer == "accept" and target is not None
    if incident.status == "dismissed" or (incident.status == "resolved" and not moved):
        return None

    start = max(booking.check_in, today)
    facts: dict[str, Any] = {
        "property": property_name,
        "problem": problem_phrase(incident.description),
    }
    view = GuestView(title=f"Problem at {property_name}: {facts['problem']}", facts=facts)

    if incident.status in ("relocated", "resolved") and moved and target is not None:
        facts["status"] = (
            f"The guest accepted the move and now stays at {target.name} ({target.address}) "
            f"until {booking.check_out}. Questions about the address or the place refer to "
            "this property, not to the one originally booked."
        )
        facts["current_accommodation"] = place(target)
        facts["check_in_details"] = "The host's team sends the check-in details for the new place."
        view.valid_until = booking.check_out
        return view

    if incident.status == "awaiting_guest" and target is not None:
        facts["status"] = (
            f"The host offered to move the guest to {target.name} for the rest of the stay, at no "
            "extra cost, and is waiting for the guest's answer."
        )
        if_stay = _approved_estimate_if_stays(incident)
        facts["if_the_guest_stays"] = f"The repair goes ahead; it should be fixed {if_stay}."
        view.offer = GuestOffer(
            question=f"Would you like to move to {target.name} for the rest of your stay?",
            details={
                **place(target),
                "extra_cost": "none",
                "from": start.isoformat(),
                "until": booking.check_out.isoformat(),
            },
            expires_at=incident.guest_deadline,
        )
        return view

    if incident.status == "remediation_scheduled":
        estimate = _approved_estimate(incident)
        if incident.closure_approval_id:
            facts["status"] = "Maintenance reported that the work is done and the host is checking it."
            facts["note"] = "Do not tell the guest it is fixed until this status changes."
        elif estimate:
            facts["status"] = "The repair is scheduled with the maintenance team."
            facts["repair_estimate"] = estimate
        else:
            facts["status"] = (
                "The repair is scheduled with the maintenance team. There is no completion time "
                "to share yet."
            )
        if incident.guest_answer == "stay":
            facts["relocation"] = "The guest was offered a move and chose to stay."
        elif incident.guest_answer == "timeout":
            facts["relocation"] = "The relocation offer expired without an answer, so the guest stays."
        return view

    # awaiting_staff / awaiting_decision: nimic nu e încă hotărât.
    facts["status"] = incident.guest_note or (
        "The host's team has been informed and is assessing the problem. There is no repair "
        "time or solution to share yet."
    )
    return view


def _approved_estimate_if_stays(incident: Incident) -> str:
    drafts = incident.drafts or GuestDrafts()
    if drafts.relocation_declined and incident.eta_hours:
        return f"in {friendly_hours(incident.eta_hours)}"
    return "as soon as possible"
