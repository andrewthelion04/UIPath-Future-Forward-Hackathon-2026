"""Intrările simulatorului.

Fiecare endpoint doar normalizează datele într-un eveniment intern și îl publică (sau, la
anulare, acționează asupra PMS-ului simulat și lasă `SyncService` să emită evenimentul).
`POST /sim/guest-message` este servit de F1, fiindcă trece prin același pipeline ca un webhook.

Cu `?wait=true` răspunsul vine după ce handlerii au terminat (util în UI și în teste).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.container import Container
from app.core.contracts.events import IssueReported, ReviewAnalysisRequested
from app.core.contracts.models import BookingStatus, PmsProvider
from app.ports.simulation import SimulationError


class StaffReportIn(BaseModel):
    property_id: str
    description: str = Field(min_length=3)
    staff_id: str | None = None
    booking_id: str | None = None
    severity: Literal["low", "medium", "high"] = "medium"


class CancelBookingIn(BaseModel):
    booking_id: str


class ReviewAnalysisIn(BaseModel):
    property_id: str | None = None  # None = toate proprietățile


class SimResult(BaseModel):
    status: str
    event_id: str | None = None
    correlation_id: str | None = None
    detail: str = ""
    completed: bool | None = None  # doar cu ?wait=true


def build_router(c: Container) -> APIRouter:
    router = APIRouter(prefix="/sim", tags=["simulator"])

    async def finish(wait: bool, result: SimResult) -> SimResult:
        if wait:
            result.completed = await c.bus.join(timeout=c.settings.sim_wait_timeout_s)
        return result

    @router.post("/staff-report", response_model=SimResult)
    async def staff_report(body: StaffReportIn, wait: bool = False) -> SimResult:
        """Sesizare de la personal (ex. doamna de curățenie) -> `IssueReported`."""
        if c.world.get_property(body.property_id) is None:
            raise HTTPException(404, f"unknown property {body.property_id}")
        booking = c.world.get_booking(body.booking_id) if body.booking_id else None
        if body.booking_id and booking is None:
            raise HTTPException(404, f"unknown booking {body.booking_id}")
        if booking is not None and booking.property_id != body.property_id:
            raise HTTPException(
                422, f"booking {booking.id} is not a stay at property {body.property_id}"
            )
        staff = {member.id: member for member in c.world.list_staff()}
        if body.staff_id and body.staff_id not in staff:
            raise HTTPException(404, f"unknown staff member {body.staff_id}")
        reporter = staff[body.staff_id].name if body.staff_id else "Staff"
        event = await c.bus.publish(
            IssueReported(
                property_id=body.property_id,
                booking_id=body.booking_id,
                description=body.description,
                reported_via="staff",
                reporter=reporter,
                severity=body.severity,
                source="simulator",
            )
        )
        return await finish(
            wait,
            SimResult(
                status="published", event_id=event.event_id, correlation_id=event.correlation_id
            ),
        )

    @router.post("/cancel-booking", response_model=SimResult)
    async def cancel_booking(body: CancelBookingIn, wait: bool = False) -> SimResult:
        """Anulare în PMS-ul simulat, apoi sincronizare: `SyncService` emite `BookingCancelled`."""
        booking = c.world.get_booking(body.booking_id)
        if booking is None:
            raise HTTPException(404, f"unknown booking {body.booking_id}")
        if booking.pms_provider != PmsProvider.MOCK:
            raise HTTPException(
                400, "only bookings on the simulated PMS can be cancelled from the simulator"
            )
        if booking.status == BookingStatus.CANCELLED:
            return SimResult(status="already_cancelled", detail="booking was already cancelled")
        try:
            await c.simulation.cancel_reservation(body.booking_id)
        except SimulationError as exc:
            raise HTTPException(400, str(exc)) from exc
        report = await c.sync.sync("mock")
        return await finish(
            wait,
            SimResult(
                status="cancelled",
                detail=f"cancellations detected by sync: {report.cancellations_detected}",
            ),
        )

    @router.post("/review-analysis", response_model=SimResult)
    async def review_analysis(body: ReviewAnalysisIn, wait: bool = False) -> SimResult:
        """Cerere de analiză a recenziilor -> `ReviewAnalysisRequested`."""
        if body.property_id and c.world.get_property(body.property_id) is None:
            raise HTTPException(404, f"unknown property {body.property_id}")
        event = await c.bus.publish(
            ReviewAnalysisRequested(property_id=body.property_id, source="simulator")
        )
        return await finish(
            wait,
            SimResult(
                status="published", event_id=event.event_id, correlation_id=event.correlation_id
            ),
        )

    @router.post("/reset")
    async def reset_world() -> dict[str, Any]:
        """Șterge toate datele și reface lumea simulată din seed."""
        summary = await c.reset_world()
        return {"status": "reset", **summary}

    return router
