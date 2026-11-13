"""F2 — Incidente: sesizare -> incident -> aprobare (remediere / relocare) -> acțiune."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.core.channel_gateway import ChannelError
from app.core.clock import utcnow
from app.core.contracts.events import ApprovalDecided, IssueReported
from app.core.contracts.models import ApprovalOption, Booking, Property
from app.core.feature_context import FeatureContext
from app.core.ids import new_id
from app.features.f2_incidents.events import IncidentResolved

logger = logging.getLogger("staypilot.f2")

APPROVAL_KIND = "incident_resolution"
OPTION_REMEDIATE = "remediate"
OPTION_RELOCATE = "relocate"

IncidentStatus = Literal["awaiting_decision", "remediation_scheduled", "relocated", "dismissed"]


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


def is_compatible(candidate: Property, original: Property, guest_count: int) -> bool:
    """Regula de compatibilitate pentru relocare: același oraș, loc pentru toți, cel puțin la fel de mare."""
    return (
        candidate.id != original.id
        and candidate.city.strip().lower() == original.city.strip().lower()
        and candidate.max_guests >= guest_count
        and candidate.bedrooms >= original.bedrooms
    )


class IncidentService:
    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx

    # --- Citire --------------------------------------------------------------------

    def list(self) -> list[Incident]:
        rows = self._ctx.db.query("SELECT * FROM f2_incidents ORDER BY created_at DESC, rowid DESC")
        return [Incident(**row) for row in rows]

    def get(self, incident_id: str) -> Incident | None:
        row = self._ctx.db.query_one("SELECT * FROM f2_incidents WHERE id = ?", (incident_id,))
        return Incident(**row) if row else None

    def _update(self, incident_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._ctx.db.execute(
            f"UPDATE f2_incidents SET {assignments} WHERE id = ?", (*fields.values(), incident_id)
        )

    # --- Sesizare -> incident -> cerere de aprobare --------------------------------

    async def on_issue_reported(self, event: IssueReported) -> None:
        ctx = self._ctx
        prop = ctx.world.get_property(event.property_id)
        if prop is None:
            ctx.notify(
                "Issue reported for an unknown property",
                f"{event.description} (reported by {event.reporter or event.reported_via})",
                level="warning",
            )
            return

        today = ctx.clock.today()
        booking = (
            ctx.world.get_booking(event.booking_id)
            if event.booking_id
            else ctx.world.active_booking_for_property(prop.id, today)
        )
        candidate = await self._find_relocation(booking, prop, today) if booking else None

        incident_id = new_id("inc")
        now = utcnow().isoformat()
        ctx.db.execute(
            "INSERT INTO f2_incidents (id, property_id, booking_id, description, reported_via, "
            "reporter, severity, status, relocation_property_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_decision', ?, ?, ?)",
            (
                incident_id,
                prop.id,
                booking.id if booking else None,
                event.description,
                event.reported_via,
                event.reporter,
                event.severity,
                candidate.id if candidate else None,
                now,
                now,
            ),
        )

        options = [
            ApprovalOption(
                key=OPTION_REMEDIATE,
                label="Fix it in time",
                description="Schedule the fix with your maintenance contact and inform the guest.",
            )
        ]
        if candidate is not None:
            options.append(
                ApprovalOption(
                    key=OPTION_RELOCATE,
                    label=f"Relocate guest to {candidate.name}",
                    description=(
                        f"{candidate.name} ({candidate.address}) is free for the stay, "
                        f"sleeps {candidate.max_guests} and has {candidate.bedrooms} bedroom(s)."
                    ),
                )
            )

        lines = [
            f"Reported by {event.reporter or 'unknown'} ({event.reported_via}), "
            f"severity {event.severity}: {event.description}"
        ]
        if booking is not None:
            lines.append(
                f"Affected stay: {booking.guest_name}, {booking.guest_count} guest(s), "
                f"{booking.check_in} to {booking.check_out}."
            )
            if candidate is None:
                lines.append("No compatible free property was found, so relocation is not offered.")
        else:
            lines.append("No current or upcoming stay is affected.")

        approval = await ctx.approvals.request(
            feature=ctx.feature,
            kind=APPROVAL_KIND,
            title=f"Incident at {prop.name}",
            summary="\n".join(lines),
            options=options,
            payload={"incident_id": incident_id},
            booking_id=booking.id if booking else None,
            property_id=prop.id,
            dedupe_key=f"{ctx.feature}:incident:{incident_id}",
        )
        self._update(incident_id, approval_id=approval.id)

    async def _find_relocation(
        self, booking: Booking, original: Property, today: date
    ) -> Property | None:
        """Cea mai apropiată proprietate compatibilă, liberă pe restul sejurului."""
        ctx = self._ctx
        start, end = max(booking.check_in, today), booking.check_out
        nights = (end - start).days
        if nights <= 0:
            return None
        shared = set(original.amenities)
        candidates = sorted(
            (p for p in ctx.world.list_properties() if is_compatible(p, original, booking.guest_count)),
            key=lambda p: (-len(shared & set(p.amenities)), abs(p.base_price - original.base_price)),
        )
        for candidate in candidates:
            try:
                days = await ctx.calendar.get_days(candidate.id, start, end)
            except Exception as exc:  # calendar indisponibil: candidatul e sărit, nu blocăm fluxul
                logger.warning("Calendar check failed for %s: %s", candidate.id, exc)
                continue
            if len(days) == nights and all(day.available for day in days):
                return candidate
        return None

    # --- Decizia hostului -> acțiune -----------------------------------------------

    async def on_decision(self, event: ApprovalDecided) -> None:
        incident = self.get(str(event.payload.get("incident_id")))
        if incident is None:
            return
        if event.decision == "rejected":
            await self._dismiss(incident)
        elif event.option == OPTION_RELOCATE:
            await self._relocate(incident)
        else:
            await self._remediate(incident)

    async def _dismiss(self, incident: Incident) -> None:
        self._update(incident.id, status="dismissed", resolution="Dismissed by the host.")
        self._ctx.notify(
            "Incident dismissed",
            f"No action was taken for: {incident.description}",
            property_id=incident.property_id,
            booking_id=incident.booking_id,
        )
        await self._publish_resolved(incident, "dismissed")

    async def _remediate(self, incident: Incident) -> None:
        ctx = self._ctx
        prop = ctx.world.get_property(incident.property_id)
        name = prop.name if prop else incident.property_id
        staff = [m for m in ctx.world.list_staff() if incident.property_id in m.property_ids]
        contact = next((m for m in staff if m.role == "maintenance"), staff[0] if staff else None)
        who = f"{contact.name} ({contact.role})" if contact else "no staff assigned to this property"
        self._update(
            incident.id,
            status="remediation_scheduled",
            resolution=f"Remediation approved. Contact: {who}.",
        )
        if incident.booking_id:
            booking = ctx.world.get_booking(incident.booking_id)
            first_name = booking.guest_name.split()[0] if booking else "there"
            await self._message_guest(
                incident.booking_id,
                f"Hello {first_name}, we are aware of an issue at {name} "
                f"({incident.description}). Our team is on it and we will keep you posted. "
                "Sorry for the inconvenience!",
            )
        ctx.notify(
            f"Remediation scheduled at {name}",
            f"Issue: {incident.description}\nContact to dispatch: {who}.",
            level="success",
            property_id=incident.property_id,
            booking_id=incident.booking_id,
        )
        await self._publish_resolved(incident, "remediation")

    async def _relocate(self, incident: Incident) -> None:
        ctx = self._ctx
        target = (
            ctx.world.get_property(incident.relocation_property_id)
            if incident.relocation_property_id
            else None
        )
        booking = ctx.world.get_booking(incident.booking_id) if incident.booking_id else None
        if target is None or booking is None:
            ctx.notify(
                "Relocation could not be carried out",
                "The incident has no relocation target or no affected booking.",
                level="warning",
                property_id=incident.property_id,
            )
            return

        notes: list[str] = []
        start, end = max(booking.check_in, ctx.clock.today()), booking.check_out
        try:
            await ctx.calendar.set_availability(target.id, start, end, False)
            notes.append(f"{target.name} is blocked from {start} to {end}.")
        except Exception as exc:  # fallback explicit: hostul blochează manual
            notes.append(f"Could not block {target.name} in the calendar ({exc}); block it manually.")

        prop = ctx.world.get_property(incident.property_id)
        await self._message_guest(
            booking.id,
            f"Hello {booking.guest_name.split()[0]}, because of an issue at "
            f"{prop.name if prop else 'your apartment'} we would like to move you to "
            f"{target.name} ({target.address}) for the rest of your stay, at no extra cost. "
            "We will send the check-in details shortly. Sorry for the inconvenience!",
        )
        notes.append("The reservation itself must still be moved in the PMS.")
        self._update(
            incident.id, status="relocated", resolution=f"Relocation to {target.name} approved."
        )
        ctx.notify(
            f"{booking.guest_name} is being relocated to {target.name}",
            "\n".join(notes),
            level="success",
            property_id=incident.property_id,
            booking_id=booking.id,
        )
        await self._publish_resolved(incident, "relocation")

    async def _message_guest(self, booking_id: str, text: str) -> None:
        try:
            await self._ctx.channel.send_to_guest(
                booking_id, text, sender="host", feature=self._ctx.feature
            )
        except ChannelError:
            pass  # ChannelGateway a notificat deja hostul; restul acțiunii continuă

    async def _publish_resolved(
        self, incident: Incident, resolution: Literal["remediation", "relocation", "dismissed"]
    ) -> None:
        await self._ctx.bus.publish(
            IncidentResolved(
                incident_id=incident.id,
                property_id=incident.property_id,
                booking_id=incident.booking_id,
                resolution=resolution,
                source=f"feature:{self._ctx.feature}",
            )
        )
