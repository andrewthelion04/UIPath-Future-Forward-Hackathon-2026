"""F2 — Incidente.

Fluxul: sesizare -> triere (atașez sau deschid) -> agentul întreabă mentenanța cât durează ->
estimare -> propunere către host (relocare sau remediere, cu mesajele exacte pentru oaspete) ->
decizia hostului -> oferta de relocare și răspunsul oaspetelui -> sarcina de reparație ->
personalul termină -> aprobarea închiderii -> `resolved`.

Reguli de execuție (vezi CLAUDE.md-ul feature-ului):
- un singur agent per handler; pașii asincroni trec prin evenimente interne;
- orice muncă se face într-un handler, ca aprobările și mesajele să moștenească corelarea
  incidentului; un pas pornit din afară (endpoint, termen expirat) doar publică un eveniment
  cu `correlation_id`-ul incidentului;
- nimic nu pleacă spre oaspete în afara unei aprobări, iar ce pleacă e textul aprobat.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from app.core.channel_gateway import ChannelError
from app.core.clock import utcnow
from app.core.contracts.events import (
    ApprovalDecided,
    Event,
    GuestMessageReceived,
    GuestOfferAnswered,
    IssueReported,
)
from app.core.contracts.models import ApprovalOption, Booking, BookingStatus, Property, StaffMember
from app.core.feature_context import FeatureContext
from app.features.f2_incidents import templates
from app.features.f2_incidents.agents import IncidentAgents
from app.features.f2_incidents import guest_view
from app.features.f2_incidents.events import (
    EstimateReady,
    GuestAnsweredRelocation,
    IncidentDecided,
    IncidentResolved,
    IncidentTaskCompleted,
    IncidentTimeout,
    SimGuestAnswerDue,
    SimStaffReplyDue,
    SimTaskDue,
    StaffContactRequested,
    StaffReplied,
)
from app.features.f2_incidents.models import (
    OPTION_RELOCATE,
    OPTION_REMEDIATE,
    GuestDrafts,
    Incident,
    IncidentView,
    StaffMessage,
    StaffTask,
)
from app.features.f2_incidents.repo import IncidentRepository
from app.features.f2_incidents.rules import (
    covers_free_nights,
    explain_recommendation,
    fix_in_time,
    format_hours,
    friendly_hours,
    higher_severity,
    hours_until_check_in,
    is_compatible,
    parse_triage_result,
    problem_phrase,
    recommend,
    valid_guest_message,
)
from app.features.f2_incidents.settings import F2Settings
from app.features.f2_incidents.simulation import scripted_staff_reply, scripted_task_note

logger = logging.getLogger("staypilot.f2")

APPROVAL_KIND = "incident_resolution"
CLOSURE_KIND = "incident_closure"
OPTION_CLOSE = "close"

__all__ = [  # păstrate pentru cine importa din `service` înainte de împărțirea în module
    "APPROVAL_KIND",
    "CLOSURE_KIND",
    "OPTION_RELOCATE",
    "OPTION_REMEDIATE",
    "Incident",
    "IncidentService",
    "is_compatible",
]


# --- Ceasul (înlocuibil în teste) -------------------------------------------------------


def utc_now() -> datetime:
    return utcnow()


def local_time_of_day() -> time:
    """Ora locală curentă; data vine din `ctx.clock.today()` (poate fi înghețată)."""
    return datetime.now().time()


# --- Faptele propunerii -------------------------------------------------------------------


@dataclass
class ProposalFacts:
    """Tot ce trebuie pentru propunere: calculat în cod, citit de agentul `proposal`."""

    property_name: str
    description: str
    booking: Booking | None
    has_booking: bool
    in_house: bool
    hours_until: float | None
    eta_hours: float | None
    eta_source: str
    eta_quote: str | None
    contact_name: str | None
    candidate: Property | None
    in_time: bool
    recommendation: str
    reason: str

    def needed_messages(self) -> set[str]:
        needed: set[str] = set()
        if not self.has_booking:
            return needed
        if self.candidate is not None:
            needed |= {"relocation_offer", "relocation_accepted", "relocation_declined"}
        if self.in_house or not self.in_time:
            needed.add("fix_apology")  # dacă reparația se termină înainte de sosire: fără mesaj
        return needed

    def as_context(self) -> dict[str, Any]:
        booking = self.booking
        return {
            "guest_first_name": templates.first_name(booking.guest_name) if booking else None,
            "property": self.property_name,
            "problem": problem_phrase(self.description),
            "guest_status": (
                "in the property now" if self.in_house
                else f"arrives in {format_hours(self.hours_until)}" if self.has_booking
                else "no guest affected"
            ),
            "fix_estimate": (
                friendly_hours(self.eta_hours) if self.eta_hours
                else "unknown: say it will be fixed as soon as possible"
            ),
            "relocation_target": (
                {"name": self.candidate.name, "address": self.candidate.address,
                 "sleeps": self.candidate.max_guests, "bedrooms": self.candidate.bedrooms,
                 "listing_url": self.candidate.listing_url}
                if self.candidate else None
            ),
            "messages_needed": sorted(self.needed_messages()),
            "rules": [
                "relocation is at no extra cost for the guest",
                "do not promise compensation, refunds or exact times beyond the fix estimate",
                "sign as the host's team; no placeholders",
            ],
        }  # fmt: skip


class IncidentService:
    def __init__(self, ctx: FeatureContext, settings: F2Settings) -> None:
        self._ctx = ctx
        self.settings = settings
        self.repo = IncidentRepository(ctx.db)
        self.agents = IncidentAgents(ctx, self.repo)
        self._property_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._incident_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._background: set[asyncio.Task[None]] = set()
        self.repo.on_change = self._sync_guest_topic

    async def stop_background(self) -> None:
        """Oprește pașii simulați aflați în pauză (shutdown sau resetul lumii)."""
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- Starea pentru oaspete (citită de cine vorbește cu el) -----------------------------

    def _sync_guest_topic(self, incident: Incident) -> None:
        """Publică ce are voie să afle oaspetele despre incident, după fiecare schimbare."""
        ctx = self._ctx
        topic = guest_view.topic_id(ctx.feature, incident.id)
        try:
            booking = self._live_booking(incident)
            target = (
                ctx.world.get_property(incident.relocation_property_id)
                if incident.relocation_property_id
                else None
            )
            view = guest_view.guest_view(
                incident, booking, self._property_name(incident.property_id), target, ctx.clock.today()
            )
            if view is None or booking is None:
                ctx.guest_topics.close(topic)
                return
            if view.offer is not None:
                # Un răspuns deja dat de oaspete (încă neprelucrat) nu se rescrie ca „în așteptare”.
                current = ctx.guest_topics.get(topic)
                if current and current.offer and current.offer.status in ("accepted", "declined"):
                    view.offer.status = current.offer.status
            ctx.guest_topics.publish(
                topic,
                booking_id=booking.id,
                feature=ctx.feature,
                kind=guest_view.TOPIC_KIND,
                title=view.title,
                facts=view.facts,
                offer=view.offer,
                valid_until=view.valid_until,
            )
        except Exception:  # starea publică nu are voie să oprească fluxul incidentului
            logger.exception("Could not publish the guest view of incident %s", incident.id)

    # --- Citire ---------------------------------------------------------------------------

    def list(self) -> list[IncidentView]:
        return [self.repo.view(incident) for incident in self.repo.list()]

    def get(self, incident_id: str) -> Incident | None:
        return self.repo.get(incident_id)

    def view(self, incident_id: str) -> IncidentView | None:
        incident = self.repo.get(incident_id)
        return self.repo.view(incident) if incident else None

    # --- Ajutoare -------------------------------------------------------------------------

    def _now_local(self) -> datetime:
        return datetime.combine(self._ctx.clock.today(), local_time_of_day())

    async def _emit(self, event_cls: type[Event], incident: Incident, **fields: Any) -> None:
        """Publică un eveniment intern pe corelarea incidentului (și din afara unui handler)."""
        await self._ctx.bus.publish(
            event_cls(
                incident_id=incident.id,
                correlation_id=incident.correlation_id,
                source=f"feature:{self._ctx.feature}",
                **fields,
            )
        )

    async def _later(self, delay_s: float, event: Event) -> None:
        """Publică acum (întârziere 0: `?wait=true` îl așteaptă) sau după o pauză, în fundal."""
        if delay_s <= 0:
            await self._ctx.bus.publish(event)
            return

        async def publish_later() -> None:
            try:
                await asyncio.sleep(delay_s)
                await self._ctx.bus.publish(event)
            except asyncio.CancelledError:
                pass
            except Exception:  # fundalul nu are cine să prindă eroarea: o logăm
                logger.exception("Delayed F2 step %s failed", event.event_type())

        # Contextul (inclusiv evenimentul în curs) se copiază la creare: corelarea se păstrează.
        task = asyncio.get_running_loop().create_task(publish_later())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _event(self, event_cls: type[Event], incident: Incident, **fields: Any) -> Event:
        return event_cls(
            incident_id=incident.id,
            correlation_id=incident.correlation_id,
            source=f"feature:{self._ctx.feature}",
            **fields,
        )

    def _property_name(self, property_id: str) -> str:
        prop = self._ctx.world.get_property(property_id)
        return prop.name if prop else property_id

    def _contact(self, property_id: str) -> StaffMember | None:
        """Persoana de contact: întâi mentenanța alocată proprietății, apoi oricine alocat."""
        staff = [m for m in self._ctx.world.list_staff() if property_id in m.property_ids]
        return next((m for m in staff if m.role == "maintenance"), staff[0] if staff else None)

    def _live_booking(self, incident: Incident) -> Booking | None:
        """Rezervarea afectată, dacă e confirmată și sejurul nu s-a terminat."""
        if not incident.booking_id:
            return None
        booking = self._ctx.world.get_booking(incident.booking_id)
        if booking is None or booking.status != BookingStatus.CONFIRMED:
            return None
        return booking if booking.check_out > self._ctx.clock.today() else None

    async def _message_guest(self, booking_id: str, text: str | None) -> bool:
        if not text:
            return False
        try:
            await self._ctx.channel.send_to_guest(
                booking_id, text, sender="host", feature=self._ctx.feature
            )
            return True
        except ChannelError:
            return False  # ChannelGateway a notificat deja hostul; restul acțiunii continuă

    def _stay_context(self, incident: Incident) -> str:
        booking = self._live_booking(incident)
        if booking is None:
            return "No guest is affected right now."
        hours = hours_until_check_in(
            booking.check_in, self._ctx.world.host_rules().check_in_time, self._now_local()
        )
        if hours <= 0:
            return "A guest is staying there right now."
        return f"The next guest checks in in {format_hours(hours)}."

    # --- Canalul cu personalul (o singură funcție, înlocuibilă de un canal real) -------------

    async def send_to_staff(self, incident: Incident, text: str) -> StaffMessage:
        """Întrebare către mentenanță: pornește termenul și, în simulare, răspunsul automat."""
        message = self.repo.add_staff_message(
            incident.id,
            direction="to_staff",
            body=text,
            staff_id=incident.contact_staff_id,
            staff_name=incident.contact_staff_name or "",
        )
        deadline = utc_now() + timedelta(minutes=self.settings.staff_reply_timeout_min)
        incident = self.repo.update(
            incident.id, staff_questions=incident.staff_questions + 1, staff_deadline=deadline
        )
        if self.settings.sim_enabled:
            await self._later(self.settings.sim_staff_delay_s, self._event(SimStaffReplyDue, incident))
        return message

    def _note_to_staff(self, incident: Incident, staff_id: str | None, name: str | None, text: str) -> None:
        """Mesaj informativ către personal (fără întrebare, fără termen)."""
        if staff_id:
            self.repo.add_staff_message(
                incident.id, direction="to_staff", body=text, staff_id=staff_id, staff_name=name or ""
            )

    # --- Intrări independente de canal (endpointuri, simulare, un viitor WhatsApp) ---------

    async def report_issue(
        self,
        *,
        property_id: str,
        description: str,
        staff_id: str | None,
        booking_id: str | None,
        severity: str,
    ) -> Event:
        """Sesizare de la personal: același eveniment ca din simulator sau de la F3."""
        world = self._ctx.world
        if world.get_property(property_id) is None:
            raise LookupError(f"unknown property {property_id}")
        booking = world.get_booking(booking_id) if booking_id else None
        if booking_id and booking is None:
            raise LookupError(f"unknown booking {booking_id}")
        if booking is not None and booking.property_id != property_id:
            raise ValueError(f"booking {booking_id} is not a stay at property {property_id}")
        staff = {m.id: m for m in world.list_staff()}
        if staff_id and staff_id not in staff:
            raise LookupError(f"unknown staff member {staff_id}")
        return await self._ctx.bus.publish(
            IssueReported(
                property_id=property_id,
                booking_id=booking_id,
                description=description,
                reported_via="staff",
                reporter=staff[staff_id].name if staff_id else "Staff",
                severity=severity,  # type: ignore[arg-type]
                source=f"feature:{self._ctx.feature}",
            )
        )

    async def receive_staff_reply(
        self, incident_id: str, text: str, *, staff_id: str | None = None, simulated: bool = False
    ) -> StaffMessage:
        incident = self.repo.get(incident_id)
        if incident is None:
            raise LookupError(f"unknown incident {incident_id}")
        if not incident.is_open:
            raise ValueError(f"incident {incident_id} is {incident.status}")
        name = incident.contact_staff_name or ""
        if staff_id and staff_id != incident.contact_staff_id:
            member = next((m for m in self._ctx.world.list_staff() if m.id == staff_id), None)
            name = member.name if member else staff_id
        message = self.repo.add_staff_message(
            incident.id,
            direction="from_staff",
            body=text,
            staff_id=staff_id or incident.contact_staff_id,
            staff_name=name,
            simulated=simulated,
        )
        await self._emit(StaffReplied, incident, message_id=message.id)
        return message

    async def answer_relocation(self, incident_id: str, choice: str, *, simulated: bool = False) -> None:
        incident = self.repo.get(incident_id)
        if incident is None:
            raise LookupError(f"unknown incident {incident_id}")
        if incident.status != "awaiting_guest":
            raise ValueError(f"incident {incident_id} is not waiting for the guest ({incident.status})")
        await self._emit(GuestAnsweredRelocation, incident, choice=choice, simulated=simulated)

    async def complete_task(
        self, task_id: str, *, note: str | None, completed_by: str | None = None
    ) -> StaffTask:
        task = self.repo.task(task_id)
        if task is None:
            raise LookupError(f"unknown task {task_id}")
        incident = self.repo.get(task.incident_id)
        if self.repo.complete_task(task_id, note=note, completed_by=completed_by or task.staff_name or "staff"):
            if incident is not None:
                await self._emit(IncidentTaskCompleted, incident, task_id=task_id)
        return self.repo.task(task_id)  # type: ignore[return-value]

    # --- Termene ---------------------------------------------------------------------------

    async def check_deadlines(self) -> int:
        """Publică un timeout pentru fiecare termen depășit. Termenele stau în baza de date."""
        now, fired = utc_now(), 0
        for column, status, kind in (
            ("staff_deadline", "awaiting_staff", "staff"),
            ("guest_deadline", "awaiting_guest", "guest"),
        ):
            for incident in self.repo.with_deadline_before(column, status, now):
                deadline = getattr(incident, column)
                self.repo.update(incident.id, **{column: None})  # o singură dată
                try:
                    await self._emit(IncidentTimeout, incident, kind=kind)
                except Exception:
                    # Fără eveniment, incidentul ar rămâne blocat: termenul se reia data viitoare.
                    self.repo.update(incident.id, **{column: deadline})
                    raise
                fired += 1
        return fired

    # === Handleri ============================================================================

    async def on_issue_reported(self, event: IssueReported) -> None:
        """Sesizare -> triere -> incident nou (sau raport atașat). Singurul abonat F2 la eveniment."""
        ctx = self._ctx
        await self.check_deadlines()
        prop = ctx.world.get_property(event.property_id)
        if prop is None:
            ctx.notify(
                "Issue reported for an unknown property",
                f"{event.description} (reported by {event.reporter or event.reported_via})",
                level="warning",
            )
            return
        today = ctx.clock.today()
        booking = ctx.world.get_booking(event.booking_id) if event.booking_id else None
        if booking is not None and booking.property_id != prop.id:
            # Sesizarea numește un sejur de la altă proprietate: nu mutăm și nu anunțăm
            # oaspetele greșit. Folosim sejurul real al proprietății raportate.
            logger.warning(
                "Issue at %s names booking %s of another property; ignoring it", prop.id, booking.id
            )
            booking = None
        if booking is None:
            booking = ctx.world.active_booking_for_property(prop.id, today)

        async with self._property_locks[prop.id]:
            target = await self._triage(event, prop)
            if target is not None:
                await self._attach(target, event, prop)
                return
            incident = self.repo.create(
                property_id=prop.id,
                booking_id=booking.id if booking else None,
                description=event.description,
                reported_via=event.reported_via,
                reporter=event.reporter,
                severity=event.severity,
                correlation_id=event.correlation_id,
            )
            self.repo.add_report(
                incident.id,
                description=event.description,
                reported_via=event.reported_via,
                reporter=event.reporter,
                severity=event.severity,
                booking_id=booking.id if booking else None,
            )
        await self._emit(StaffContactRequested, incident)

    async def _triage(self, event: IssueReported, prop: Property) -> Incident | None:
        open_incidents = self.repo.open_for_property(prop.id)
        if not open_incidents:
            return None  # nimic de comparat: incident nou, fără LLM
        outcome = await self.agents.triage(
            property_id=prop.id,
            property_name=prop.name,
            description=event.description,
            reporter=event.reporter,
        )
        if outcome.result.status != "finished":
            self._ctx.notify(
                f"Possible duplicate incident at {prop.name}",
                "The duplicate check could not run, so a new incident was opened for: "
                f'"{event.description}". It may describe an open incident.\n\n'
                f"Reason: {outcome.result.error or outcome.result.status}",
                level="warning",
                property_id=prop.id,
            )
            return None
        target_id = parse_triage_result(outcome.result.result, outcome.open_ids)
        return self.repo.get(target_id) if target_id else None

    async def _attach(self, incident: Incident, event: IssueReported, prop: Property) -> None:
        self.repo.add_report(
            incident.id,
            description=event.description,
            reported_via=event.reported_via,
            reporter=event.reporter,
            severity=event.severity,
            booking_id=event.booking_id,
        )
        severity = higher_severity(incident.severity, event.severity)
        self.repo.update(incident.id, report_count=incident.report_count + 1, severity=severity)
        escalated = f" Severity raised to {severity}." if severity != incident.severity else ""
        self._ctx.notify(
            f"Another report on the open incident at {prop.name}",
            f'{event.reporter or event.reported_via} reported: "{event.description}"\n'
            f"It was attached to the existing incident ({incident.report_count + 1} reports).{escalated}",
            property_id=prop.id,
            booking_id=incident.booking_id,
        )
        await self._reopen_closure(incident.id, event, prop)

    async def _reopen_closure(self, incident_id: str, event: IssueReported, prop: Property) -> None:
        """Un raport nou după „reparat”: cererea de închidere nu mai descrie realitatea."""
        async with self._incident_locks[incident_id]:
            incident = self.repo.get(incident_id)
            if (
                incident is None
                or incident.closure_approval_id is None
                or incident.status not in ("remediation_scheduled", "relocated")
            ):
                return
            if not self._ctx.approvals.withdraw(
                incident.closure_approval_id,
                reason="A new report arrived after the repair was marked done.",
            ):
                return  # hostul a decis deja: decizia lui rămâne
            incident = self.repo.update(incident.id, closure_approval_id=None, closure_message=None)
            task = await self._create_task(
                incident, f"Follow-up: reported again after the repair: {event.description}"
            )
            self._ctx.notify(
                f"Incident at {prop.name} is not ready to close",
                f'{event.reporter or event.reported_via} reported after the repair: "{event.description}"\n'
                "The request to close the incident was withdrawn and follow-up task "
                f"{task.id} was created for {task.staff_name or 'nobody (unassigned)'}.",
                level="warning",
                property_id=prop.id,
                booking_id=incident.booking_id,
            )

    # --- Conversația cu mentenanța -----------------------------------------------------------

    def _liaison_context(self, incident: Incident) -> dict[str, Any]:
        return {
            "incident_id": incident.id,
            "property": self._property_name(incident.property_id),
            "problem": incident.description,
            "severity": incident.severity,
            "reports": incident.report_count,
            "affected_stay": self._stay_context(incident),
            "maintenance_contact": incident.contact_staff_name,
        }

    async def on_staff_contact(self, event: StaffContactRequested) -> None:
        await self.check_deadlines()
        async with self._incident_locks[event.incident_id]:
            incident = self.repo.get(event.incident_id)
            if incident is None or incident.status != "awaiting_staff" or incident.staff_questions:
                return
            contact = self._contact(incident.property_id)
            if contact is None:
                self._ctx.notify(
                    f"No staff assigned to {self._property_name(incident.property_id)}",
                    "Nobody could be asked how long the fix takes; the proposal is made without "
                    "an estimate. Assign a maintenance person to this property.",
                    level="warning",
                    property_id=incident.property_id,
                    booking_id=incident.booking_id,
                )
                self.repo.update(incident.id, eta_source="no_staff")
                await self._emit(EstimateReady, incident)
                return
            incident = self.repo.update(
                incident.id, contact_staff_id=contact.id, contact_staff_name=contact.name
            )
            outcome = await self.agents.staff_liaison(
                incident,
                mode="ask",
                context=self._liaison_context(incident),
                send_to_staff=self.send_to_staff,
            )
            if not outcome.sent:  # fallback explicit: întrebarea-șablon
                incident = self.repo.get(incident.id) or incident
                await self.send_to_staff(
                    incident,
                    templates.staff_question(
                        contact.name,
                        self._property_name(incident.property_id),
                        incident.description,
                        self._stay_context(incident),
                    ),
                )

    async def on_staff_replied(self, event: StaffReplied) -> None:
        await self.check_deadlines()
        async with self._incident_locks[event.incident_id]:
            incident = self.repo.get(event.incident_id)
            if incident is None or incident.status != "awaiting_staff":
                return  # răspunsul rămâne în conversație; nu mai schimbă nimic
            outcome = await self.agents.staff_liaison(
                incident,
                mode="interpret",
                context=self._liaison_context(incident),
                send_to_staff=self.send_to_staff,
            )
            if outcome.estimate is not None:
                estimate = outcome.estimate
                self.repo.update(
                    incident.id,
                    eta_hours=estimate["hours"],
                    eta_source="staff" if estimate["hours"] else "unknown",
                    eta_quote=estimate["quote"],
                    eta_confidence=estimate["confidence"],
                    staff_deadline=None,
                )
            elif outcome.sent:
                return  # a cerut o clarificare; rulează din nou la următorul răspuns
            else:  # fallback explicit: estimare necunoscută
                reply = self.repo.staff_message(event.message_id)
                self.repo.update(
                    incident.id,
                    eta_hours=None,
                    eta_source="unknown",
                    eta_quote=reply.body if reply else None,
                    staff_deadline=None,
                )
                self._ctx.notify(
                    "Could not read the repair estimate",
                    f"The maintenance reply could not be interpreted "
                    f"({outcome.result.error or outcome.result.status}); the proposal is made "
                    "without an estimate.",
                    level="warning",
                    property_id=incident.property_id,
                    booking_id=incident.booking_id,
                )
            await self._emit(EstimateReady, incident)

    async def on_sim_staff_reply(self, event: SimStaffReplyDue) -> None:
        """Simulare: personalul de mentenanță răspunde la ultima întrebare."""
        incident = self.repo.get(event.incident_id)
        if incident is None or incident.status != "awaiting_staff":
            return
        conversation = self.repo.staff_messages(incident.id)
        questions = [m for m in conversation if m.direction == "to_staff"]
        if not questions:
            return
        round_index = len(questions) - 1
        prop_name = self._property_name(incident.property_id)
        text = await self.agents.simulated_staff(
            incident,
            mode="reply",
            staff_name=incident.contact_staff_name or "Maintenance",
            property_name=prop_name,
            description=incident.description,
            message=questions[-1].body,
            round_index=round_index,
            situation=scripted_staff_reply(incident.description, round_index),
        )
        text = text or scripted_staff_reply(incident.description, round_index)
        await self.receive_staff_reply(
            incident.id, text, staff_id=incident.contact_staff_id, simulated=True
        )

    async def on_timeout(self, event: IncidentTimeout) -> None:
        async with self._incident_locks[event.incident_id]:
            incident = self.repo.get(event.incident_id)
            if incident is None:
                return
            name = self._property_name(incident.property_id)
            if event.kind == "staff" and incident.status == "awaiting_staff":
                self.repo.update(incident.id, eta_hours=None, eta_source="timeout", staff_deadline=None)
                self._ctx.notify(
                    f"Maintenance did not reply about {name}",
                    f"No answer in {self.settings.staff_reply_timeout_min:g} minutes; the proposal "
                    "is made without a repair estimate.",
                    level="warning",
                    property_id=incident.property_id,
                    booking_id=incident.booking_id,
                )
                await self._emit(EstimateReady, incident)
            elif event.kind == "guest" and incident.status == "awaiting_guest":
                await self._emit(GuestAnsweredRelocation, incident, choice="timeout")

    # --- Propunerea către host ----------------------------------------------------------------

    def _facts(self, incident: Incident, candidate: Property | None) -> ProposalFacts:
        booking = self._live_booking(incident)
        has_booking = booking is not None
        hours_until = (
            hours_until_check_in(
                booking.check_in, self._ctx.world.host_rules().check_in_time, self._now_local()
            )
            if booking
            else None
        )
        in_house = bool(hours_until is not None and hours_until <= 0)
        in_time = fix_in_time(
            incident.eta_hours,
            hours_until,
            in_house,
            margin_hours=self.settings.fix_margin_hours,
            inhouse_tolerable_hours=self.settings.inhouse_tolerable_hours,
        )
        option = recommend(
            has_booking=has_booking, in_time=in_time, relocation_available=candidate is not None
        )
        reason = explain_recommendation(
            option,
            eta_hours=incident.eta_hours,
            hours_until=hours_until,
            in_house=in_house,
            has_booking=has_booking,
            in_time=in_time,
            relocation_available=candidate is not None,
            inhouse_tolerable_hours=self.settings.inhouse_tolerable_hours,
        )
        return ProposalFacts(
            property_name=self._property_name(incident.property_id),
            description=incident.description,
            booking=booking,
            has_booking=has_booking,
            in_house=in_house,
            hours_until=hours_until,
            eta_hours=incident.eta_hours,
            eta_source=incident.eta_source or "unknown",
            eta_quote=incident.eta_quote,
            contact_name=incident.contact_staff_name,
            candidate=candidate,
            in_time=in_time,
            recommendation=option,
            reason=reason,
        )

    def _template_drafts(self, facts: ProposalFacts) -> GuestDrafts:
        booking, needed = facts.booking, facts.needed_messages()
        if booking is None:
            return GuestDrafts()
        name, prop, problem = booking.guest_name, facts.property_name, facts.description
        target = facts.candidate
        values = {
            "relocation_offer": templates.relocation_offer(
                name, prop, problem, target.name, target.address, target.listing_url
            )
            if target else None,
            "relocation_accepted": templates.relocation_accepted(name, target.name, target.address)
            if target else None,
            "relocation_declined": templates.fix_apology(name, prop, problem, facts.eta_hours),
            "fix_apology": templates.fix_apology(name, prop, problem, facts.eta_hours),
        }  # fmt: skip
        return GuestDrafts(**{key: values[key] for key in needed})

    async def on_estimate_ready(self, event: EstimateReady) -> None:
        await self.check_deadlines()
        async with self._incident_locks[event.incident_id]:
            incident = self.repo.get(event.incident_id)
            if incident is None:
                return
            fresh = incident.status == "awaiting_staff"
            redo = incident.status == "awaiting_decision" and incident.approval_id is None
            if not (fresh or redo):
                return
            booking = self._live_booking(incident)
            prop = self._ctx.world.get_property(incident.property_id)
            candidate = (
                await self._find_relocation(booking, prop, self._ctx.clock.today())
                if booking and prop
                else None
            )
            facts = self._facts(incident, candidate)

            needed = facts.needed_messages()
            drafts, source = None, "llm"
            if needed:
                outcome = await self.agents.proposal(incident, facts)
                drafts = outcome.drafts
                if drafts is None or not all(valid_guest_message(getattr(drafts, n)) for n in needed):
                    drafts, source = self._template_drafts(facts), "template"
            else:
                drafts, source = GuestDrafts(), None

            round_no = incident.proposal_round + 1
            incident = self.repo.update(
                incident.id,
                status="awaiting_decision",
                in_house=facts.in_house,
                hours_until_check_in=facts.hours_until,
                relocation_property_id=candidate.id if candidate else None,
                recommendation=facts.recommendation,
                recommendation_reason=facts.reason,
                drafts=drafts,
                drafts_source=source,
                proposal_round=round_no,
                staff_deadline=None,
            )
            approval = await self._ctx.approvals.request(
                feature=self._ctx.feature,
                kind=APPROVAL_KIND,
                title=f"Incident at {facts.property_name}",
                summary=self._proposal_summary(incident, facts),
                options=self._proposal_options(incident, facts, drafts),
                payload={"incident_id": incident.id, "round": round_no},
                booking_id=incident.booking_id,
                property_id=incident.property_id,
                dedupe_key=f"{self._ctx.feature}:incident:{incident.id}:proposal:{round_no}",
            )
            self.repo.update(incident.id, approval_id=approval.id)

    def _proposal_summary(self, incident: Incident, facts: ProposalFacts) -> str:
        lines = [
            f"Reported by {incident.reporter or 'unknown'} ({incident.reported_via}), "
            f"severity {incident.severity}: {incident.description}"
        ]
        if incident.report_count > 1:
            lines.append(f"{incident.report_count} reports describe this problem.")
        contact = incident.contact_staff_name
        if incident.eta_source == "no_staff":
            lines.append("No maintenance person is assigned to this property.")
        elif incident.eta_source == "timeout":
            lines.append(f"Maintenance ({contact}) did not reply in time.")
        elif incident.eta_quote:
            lines.append(f'Maintenance ({contact}) replied: "{incident.eta_quote}"')
        lines.append(
            f"Estimated fix time: {format_hours(incident.eta_hours)}"
            + (f" (confidence {incident.eta_confidence})" if incident.eta_hours and incident.eta_confidence else "")
            + "."
        )
        booking = facts.booking
        if booking is not None:
            when = (
                "the guest is in the property now"
                if facts.in_house
                else f"check-in in {format_hours(facts.hours_until)}"
            )
            lines.append(
                f"Affected stay: {booking.guest_name}, {booking.guest_count} guest(s), "
                f"{booking.check_in} to {booking.check_out}; {when}."
            )
            if facts.candidate is None:
                lines.append("No compatible free property was found, so relocation is not offered.")
        else:
            lines.append("No current or upcoming stay is affected.")
        label = "Offer relocation" if facts.recommendation == OPTION_RELOCATE else "Fix in place"
        lines.append(f"Recommendation: {label}. {facts.reason}")
        if incident.drafts_source == "template":
            lines.append("The guest messages below are templates: the AI draft was not available.")
        return "\n".join(lines)

    def _proposal_options(
        self, incident: Incident, facts: ProposalFacts, drafts: GuestDrafts
    ) -> list[ApprovalOption]:
        contact = incident.contact_staff_name or "the maintenance team"
        if drafts.fix_apology:
            remediate_msg = f'Guest will receive: "{drafts.fix_apology}"'
        elif facts.has_booking:
            remediate_msg = "No message to the guest: the fix should be done before they arrive."
        else:
            remediate_msg = "No guest is affected."
        remediate = ApprovalOption(
            key=OPTION_REMEDIATE,
            label="Fix it in place",
            description=f"{contact} gets a repair task. {remediate_msg}",
        )
        options = [remediate]
        target = facts.candidate
        if target is not None:
            relocate = ApprovalOption(
                key=OPTION_RELOCATE,
                label=f"Offer relocation to {target.name}",
                description=(
                    f"{target.name} ({target.address}) is free for the rest of the stay, sleeps "
                    f"{target.max_guests} and has {target.bedrooms} bedroom(s).\n"
                    f'Guest will receive: "{drafts.relocation_offer}"\n'
                    f'If the guest accepts: "{drafts.relocation_accepted}"\n'
                    f'If the guest stays: "{drafts.relocation_declined}"'
                ),
            )
            options = [relocate, remediate] if facts.recommendation == OPTION_RELOCATE else [remediate, relocate]
        return options

    async def _find_relocation(
        self, booking: Booking, original: Property, today: date
    ) -> Property | None:
        """Cea mai potrivită proprietate compatibilă, liberă pe restul sejurului."""
        start, end = max(booking.check_in, today), booking.check_out
        if (end - start).days <= 0:
            return None
        shared = set(original.amenities)
        candidates = sorted(
            (
                p
                for p in self._ctx.world.list_properties()
                if is_compatible(p, original, booking.guest_count)
            ),
            key=lambda p: (-len(shared & set(p.amenities)), abs(p.base_price - original.base_price)),
        )
        for candidate in candidates:
            if self._can_host_relocation(candidate) and await self._is_free(candidate.id, start, end):
                return candidate
        return None

    def _can_host_relocation(self, candidate: Property) -> bool:
        """Ținta e sănătoasă și o putem bloca: fără incident deschis, cu calendar scriabil."""
        if self.repo.open_for_property(candidate.id):
            return False  # nu mutăm un oaspete într-o proprietate cu o problemă cunoscută
        connection = self._ctx.pms.get(candidate.pms_provider)
        # Fără blocare în calendar, ținta poate fi vândută altcuiva după confirmare.
        return connection is not None and connection.capabilities.write_availability

    async def _is_free(self, property_id: str, start: date, end: date) -> bool:
        try:
            days = await self._ctx.calendar.get_days(property_id, start, end)
        except Exception as exc:  # calendar indisponibil: candidatul e sărit, nu blocăm fluxul
            logger.warning("Calendar check failed for %s: %s", property_id, exc)
            return False
        return covers_free_nights(days, property_id, start, end)

    # --- Decizia hostului ------------------------------------------------------------------

    async def on_decision(self, event: ApprovalDecided) -> None:
        async with self._incident_locks[str(event.payload.get("incident_id"))]:
            incident = self.repo.get(str(event.payload.get("incident_id")))
            if (
                incident is None
                or incident.approval_id != event.approval_id
                or incident.status != "awaiting_decision"
            ):
                return  # aprobare depășită (ex. propunere refăcută) sau deja tratată
            if event.decision == "rejected":
                await self._dismiss(incident)
                return
            option = OPTION_RELOCATE if event.option == OPTION_RELOCATE else OPTION_REMEDIATE
            await self._ctx.bus.publish(
                IncidentDecided(
                    incident_id=incident.id,
                    property_id=incident.property_id,
                    booking_id=incident.booking_id,
                    option=option,  # type: ignore[arg-type]
                    source=f"feature:{self._ctx.feature}",
                )
            )
            booking = self._live_booking(incident)
            drafts = incident.drafts or GuestDrafts()
            if option == OPTION_RELOCATE and incident.relocation_property_id and booking:
                await self._offer_relocation(incident, booking, drafts)
            else:
                await self._start_remediation(
                    incident, decided=OPTION_REMEDIATE, guest_message=drafts.fix_apology
                )

    async def _dismiss(self, incident: Incident) -> None:
        self.repo.update(incident.id, status="dismissed", resolution="Dismissed by the host.")
        self._ctx.notify(
            "Incident dismissed",
            f"No action was taken for: {incident.description}",
            property_id=incident.property_id,
            booking_id=incident.booking_id,
        )
        await self._publish_resolved(incident, "dismissed")

    async def _offer_relocation(self, incident: Incident, booking: Booking, drafts: GuestDrafts) -> None:
        target_name = self._property_name(incident.relocation_property_id or "")
        if not await self._message_guest(booking.id, drafts.relocation_offer):
            # Oaspetele nu a primit oferta: nu așteptăm (și nu simulăm) un răspuns la ea.
            await self._propose_again(
                incident,
                "The relocation offer could not be delivered",
                f"The offer for {target_name} did not reach {booking.guest_name}, so nothing "
                "changed. A new proposal follows; approve it again once messaging works, or "
                "choose the fix in place.",
                level="error",
            )
            return
        incident = self.repo.update(
            incident.id,
            status="awaiting_guest",
            guest_note=None,
            decided_option=OPTION_RELOCATE,
            guest_deadline=utc_now() + timedelta(minutes=self.settings.guest_answer_timeout_min),
            resolution=f"Relocation to {target_name} offered to the guest.",
        )
        self._ctx.notify(
            f"Relocation offered to {booking.guest_name}",
            f"The guest was offered {target_name}; waiting for their answer.",
            property_id=incident.property_id,
            booking_id=booking.id,
        )
        if self.settings.sim_enabled and self.settings.sim_guest_answer != "off":
            await self._later(self.settings.sim_guest_delay_s, self._event(SimGuestAnswerDue, incident))

    async def _start_remediation(
        self, incident: Incident, *, decided: str, guest_message: str | None
    ) -> None:
        booking = self._live_booking(incident)
        if booking is not None:
            await self._message_guest(booking.id, guest_message)
        contact = self._contact(incident.property_id)
        who = f"{contact.name} ({contact.role})" if contact else "no staff assigned to this property"
        incident = self.repo.update(
            incident.id,
            status="remediation_scheduled",
            guest_note=None,
            decided_option=incident.decided_option or decided,
            resolution=f"Remediation approved. Contact: {who}.",
        )
        task = await self._create_task(incident, f"Fix: {incident.description}")
        name = self._property_name(incident.property_id)
        self._ctx.notify(
            f"Remediation scheduled at {name}",
            f"Issue: {incident.description}\nContact to dispatch: {who}. Task {task.id} created.",
            level="success",
            property_id=incident.property_id,
            booking_id=incident.booking_id,
        )

    async def _create_task(self, incident: Incident, description: str) -> StaffTask:
        contact = self._contact(incident.property_id)
        task = self.repo.create_task(
            incident.id,
            description=description,
            staff_id=contact.id if contact else None,
            staff_name=contact.name if contact else None,
        )
        if contact is None:
            self._ctx.notify(
                f"Task {task.id} has nobody assigned",
                f"No staff is assigned to {self._property_name(incident.property_id)}. "
                f'Find someone for: "{description}".',
                level="warning",
                property_id=incident.property_id,
            )
            return task  # personalul simulat nu poate termina o sarcină neasignată
        self._note_to_staff(
            incident,
            contact.id,
            contact.name,
            templates.staff_go_ahead(
                contact.name, task.id, self._property_name(incident.property_id), description
            ),
        )
        if self.settings.sim_enabled:
            await self._later(
                self.settings.sim_task_delay_s, self._event(SimTaskDue, incident, task_id=task.id)
            )
        return task

    # --- Răspunsul oaspetelui -------------------------------------------------------------

    async def on_guest_offer_answered(self, event: GuestOfferAnswered) -> None:
        """Oaspetele a răspuns la ofertă în conversație (prin cine vorbește cu el)."""
        incident_id = guest_view.incident_id_of(self._ctx.feature, event.topic_id)
        incident = self.repo.get(incident_id) if incident_id else None
        if incident is None or incident.booking_id != event.booking_id:
            logger.warning("Offer answer for an unknown incident topic: %s", event.topic_id)
            return
        try:
            await self.answer_relocation(
                incident.id, "accept" if event.choice == "accept" else "stay"
            )
        except ValueError as exc:  # oferta nu mai așteaptă un răspuns (timeout, propunere nouă)
            logger.info("Offer answer ignored: %s", exc)
            self._ctx.notify(
                "A guest answered a relocation offer that is no longer open",
                f"The guest chose '{event.choice}', but the offer had already been closed "
                f"({incident.status}). Please follow up with the guest.",
                level="warning",
                property_id=incident.property_id,
                booking_id=incident.booking_id,
            )

    async def on_guest_message(self, event: GuestMessageReceived) -> None:
        """Un oaspete care scrie cât are o ofertă deschisă nu e trecut automat la „rămâne”."""
        deadline = utc_now() + timedelta(minutes=self.settings.guest_answer_timeout_min)
        for incident in self.repo.awaiting_guest_for_booking(event.booking_id):
            self.repo.update(incident.id, guest_deadline=deadline)

    async def on_sim_guest_answer(self, event: SimGuestAnswerDue) -> None:
        incident = self.repo.get(event.incident_id)
        if incident is None or incident.status != "awaiting_guest":
            return
        if self.settings.sim_guest_answer in ("accept", "stay"):
            await self.answer_relocation(incident.id, self.settings.sim_guest_answer, simulated=True)

    async def on_guest_answer(self, event: GuestAnsweredRelocation) -> None:
        await self.check_deadlines()
        async with self._incident_locks[event.incident_id]:
            incident = self.repo.get(event.incident_id)
            if incident is None or incident.status != "awaiting_guest":
                return
            incident = self.repo.update(incident.id, guest_answer=event.choice, guest_deadline=None)
            booking = self._live_booking(incident)
            drafts = incident.drafts or GuestDrafts()
            if event.choice == "accept" and booking is not None:
                await self._relocate(incident, booking, drafts)
                return
            if event.choice == "timeout":
                self._ctx.notify(
                    "The guest did not answer the relocation offer",
                    f"No answer in {self.settings.guest_answer_timeout_min:g} minutes: the guest "
                    "stays and the fix goes ahead.",
                    level="warning",
                    property_id=incident.property_id,
                    booking_id=incident.booking_id,
                )
            await self._start_remediation(
                incident, decided=OPTION_RELOCATE, guest_message=drafts.relocation_declined
            )

    async def _propose_again(
        self, incident: Incident, title: str, body: str, *, level: str = "warning"
    ) -> None:
        """Pasul aprobat nu s-a putut executa: nimic către oaspete, propunere nouă către host."""
        self.repo.update(
            incident.id,
            status="awaiting_decision",
            approval_id=None,
            guest_answer=None,
            guest_deadline=None,
            decided_option=None,
            resolution=None,
            guest_note=(
                "The move that was offered could not be arranged. The host is reviewing the "
                "options and will get back to the guest; nothing has changed for the stay yet."
            ),
        )
        self._ctx.notify(
            title, body, level=level, property_id=incident.property_id, booking_id=incident.booking_id
        )
        await self._emit(EstimateReady, incident)

    async def _relocate(self, incident: Incident, booking: Booking, drafts: GuestDrafts) -> None:
        ctx = self._ctx
        original = ctx.world.get_property(incident.property_id)
        target = ctx.world.get_property(incident.relocation_property_id or "")
        start, end = max(booking.check_in, ctx.clock.today()), booking.check_out
        name = target.name if target else "The relocation target"
        if target is None or original is None:
            await self._propose_again(
                incident,
                f"{name} is no longer available",
                f"{booking.guest_name} accepted the move, but the property no longer exists. "
                "Nothing was confirmed to the guest; a new proposal follows.",
            )
            return

        # Verificarea și blocarea stau sub lockul comun al calendarului țintei: două relocări
        # (sau o relocare și o relistare) nu pot decide pe aceeași citire.
        async with ctx.calendar.lock(target.id):
            still_fits = is_compatible(target, original, booking.guest_count)
            if not (
                still_fits
                and self._can_host_relocation(target)
                and await self._is_free(target.id, start, end)
            ):
                why = (
                    "was booked or blocked in the meantime"
                    if still_fits
                    else "no longer fits this stay (city, capacity or bedrooms changed)"
                )
                await self._propose_again(
                    incident,
                    f"{name} is no longer free",
                    f"{booking.guest_name} accepted the move, but {name} {why}. "
                    "Nothing was confirmed to the guest; a new proposal follows.",
                )
                return
            try:
                await ctx.calendar.set_availability(target.id, start, end, False)
            except Exception as exc:
                # Fără blocare nu putem garanta ținta: nu confirmăm nimic oaspetelui.
                await self._propose_again(
                    incident,
                    f"{name} could not be blocked in the calendar",
                    f"{booking.guest_name} accepted the move, but blocking {name} from {start} to "
                    f"{end} failed ({exc}). Nothing was confirmed to the guest; a new proposal "
                    "follows.",
                    level="error",
                )
                return

        notes = [f"{target.name} is blocked from {start} to {end}."]
        if not await self._message_guest(booking.id, drafts.relocation_accepted):
            notes.append("The confirmation could not be delivered to the guest; contact them directly.")
        notes.append("The reservation itself must still be moved in the PMS.")
        incident = self.repo.update(
            incident.id,
            status="relocated",
            resolution=f"Guest accepted; relocated to {target.name}.",
        )
        task = await self._create_task(incident, f"Fix: {incident.description}")
        notes.append(f"Repair task {task.id} created.")
        ctx.notify(
            f"{booking.guest_name} is being relocated to {target.name}",
            "\n".join(notes),
            level="success",
            property_id=incident.property_id,
            booking_id=booking.id,
        )

    # --- Sarcini și închidere -------------------------------------------------------------

    async def on_sim_task(self, event: SimTaskDue) -> None:
        task = self.repo.task(event.task_id)
        incident = self.repo.get(event.incident_id)
        if task is None or task.status != "open" or incident is None:
            return
        note = await self.agents.simulated_staff(
            incident,
            mode="done",
            staff_name=task.staff_name or "Maintenance",
            property_name=self._property_name(incident.property_id),
            description=incident.description,
            message=task.description,
            round_index=0,
            situation=scripted_task_note(incident.description),
        )
        await self.complete_task(
            task.id,
            note=note or scripted_task_note(incident.description),
            completed_by=task.staff_name,
        )

    async def on_task_completed(self, event: IncidentTaskCompleted) -> None:
        await self.check_deadlines()
        async with self._incident_locks[event.incident_id]:
            incident = self.repo.get(event.incident_id)
            task = self.repo.task(event.task_id)
            if incident is None or incident.status not in ("remediation_scheduled", "relocated"):
                return
            if task is not None:
                self._note_to_staff(
                    incident, task.staff_id, task.staff_name, templates.staff_thanks(task.staff_name, task.id)
                )
            tasks = self.repo.tasks(incident.id)
            if any(t.status == "open" for t in tasks) or incident.closure_approval_id:
                return
            await self._request_closure(incident, tasks)

    def _guest_was_told(self, incident: Incident) -> bool:
        """Oaspetele a rămas și a primit scuzele: îi spunem și când s-a rezolvat."""
        drafts = incident.drafts or GuestDrafts()
        if incident.status != "remediation_scheduled":
            return False
        if incident.decided_option == OPTION_RELOCATE:
            return bool(drafts.relocation_declined)
        return bool(drafts.fix_apology)

    async def _request_closure(self, incident: Incident, tasks: list[StaffTask]) -> None:
        booking = self._live_booking(incident)
        guest_needed = booking is not None and self._guest_was_told(incident)
        history = self._history(incident, tasks)
        outcome = await self.agents.closure(incident, history, guest_needed)
        summary, guest_message, source = outcome.summary, outcome.guest_message, "llm"
        if not summary or (guest_needed and not valid_guest_message(guest_message)):
            summary = self._raw_history_text(history)
            guest_message = (
                templates.issue_fixed(
                    booking.guest_name, self._property_name(incident.property_id), incident.description
                )
                if guest_needed and booking
                else None
            )
            source = "template"
        if source == "template":
            summary += "\n(The AI summary was not available; these are the recorded facts.)"
        round_no = incident.closure_round + 1
        option = ApprovalOption(
            key=OPTION_CLOSE,
            label="Close the incident",
            description=(
                f'Guest will receive: "{guest_message}"' if guest_message else "No message to the guest."
            ),
        )
        approval = await self._ctx.approvals.request(
            feature=self._ctx.feature,
            kind=CLOSURE_KIND,
            title=f"Close the incident at {self._property_name(incident.property_id)}?",
            summary=summary
            + (
                f'\n\nOn approval the guest will receive: "{guest_message}"'
                if guest_message
                else "\n\nNo message will be sent to the guest."
            )
            + "\n\nApprove to close the incident. Reject (with a note) to send the staff back.",
            options=[option],
            payload={"incident_id": incident.id, "round": round_no},
            booking_id=incident.booking_id,
            property_id=incident.property_id,
            dedupe_key=f"{self._ctx.feature}:incident:{incident.id}:closure:{round_no}",
        )
        self.repo.update(
            incident.id,
            closure_round=round_no,
            closure_approval_id=approval.id,
            closure_message=guest_message,
        )

    def _history(self, incident: Incident, tasks: list[StaffTask]) -> dict[str, Any]:
        booking = self._live_booking(incident)
        return {
            "guest_first_name": templates.first_name(booking.guest_name) if booking else None,
            "property": self._property_name(incident.property_id),
            "problem": incident.description,
            "reports": [
                {"by": r.reporter or r.reported_via, "via": r.reported_via, "text": r.description[:200]}
                for r in self.repo.reports(incident.id)
            ],
            "maintenance_estimate": format_hours(incident.eta_hours),
            "maintenance_quote": incident.eta_quote,
            "host_decision": incident.decided_option,
            "guest_choice": incident.guest_answer,
            "status": incident.status,
            "tasks": [
                {"id": t.id, "what": t.description, "by": t.completed_by or t.staff_name,
                 "note": t.completion_note, "done_at": t.completed_at.isoformat()[:16] if t.completed_at else None}
                for t in tasks
            ],
        }  # fmt: skip

    @staticmethod
    def _raw_history_text(history: dict[str, Any]) -> str:
        lines = [f"Problem at {history['property']}: {history['problem']}"]
        lines += [f"- Reported by {r['by']} ({r['via']}): {r['text']}" for r in history["reports"]]
        if history["maintenance_quote"]:
            lines.append(f'- Maintenance estimate: {history["maintenance_estimate"]} ("{history["maintenance_quote"]}")')
        lines.append(f"- Host decision: {history['host_decision'] or 'n/a'}; guest choice: {history['guest_choice'] or 'n/a'}")
        lines += [
            f"- Task {t['id']} done by {t['by']}: {t['note'] or 'no note'}" for t in history["tasks"]
        ]
        return "\n".join(lines)

    async def on_closure_decision(self, event: ApprovalDecided) -> None:
        async with self._incident_locks[str(event.payload.get("incident_id"))]:
            incident = self.repo.get(str(event.payload.get("incident_id")))
            if (
                incident is None
                or incident.closure_approval_id != event.approval_id
                or incident.status not in ("remediation_scheduled", "relocated")
            ):
                return
            name = self._property_name(incident.property_id)
            if event.decision == "rejected":
                note = (event.note or "").strip()
                self.repo.update(incident.id, closure_approval_id=None, closure_message=None)
                task = await self._create_task(
                    incident,
                    f"Follow-up: {note}" if note else "Follow-up requested by the host",
                )
                self._ctx.notify(
                    f"Incident at {name} stays open",
                    f"Follow-up task {task.id} was created for {task.staff_name or 'nobody (unassigned)'}.",
                    property_id=incident.property_id,
                    booking_id=incident.booking_id,
                )
                return
            booking = self._live_booking(incident)
            if booking is not None:
                await self._message_guest(booking.id, incident.closure_message)
            resolution = "relocation" if incident.status == "relocated" else "remediation"
            self.repo.update(incident.id, status="resolved", resolution=f"Resolved ({resolution}).")
            self._ctx.notify(
                f"Incident at {name} resolved",
                incident.description,
                level="success",
                property_id=incident.property_id,
                booking_id=incident.booking_id,
            )
            await self._publish_resolved(incident, resolution)

    async def _publish_resolved(self, incident: Incident, resolution: str) -> None:
        await self._ctx.bus.publish(
            IncidentResolved(
                incident_id=incident.id,
                property_id=incident.property_id,
                booking_id=incident.booking_id,
                resolution=resolution,  # type: ignore[arg-type]
                source=f"feature:{self._ctx.feature}",
            )
        )
