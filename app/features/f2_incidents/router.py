"""Endpointurile F2.

`GET /incidents` e citit de UI și de extensie. Celelalte sunt intrările independente de canal
pentru personal și oaspete: le folosește simularea acum și le poate folosi o aplicație a
personalului (sau un webhook WhatsApp) mai târziu. Ca la simulator, `?wait=true` răspunde
după ce handlerii porniți au terminat.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.features.f2_incidents.models import IncidentView, StaffTask
from app.features.f2_incidents.service import IncidentService


class StaffReportIn(BaseModel):
    property_id: str
    description: str = Field(min_length=3, max_length=1000)
    staff_id: str | None = None
    booking_id: str | None = None
    severity: Literal["low", "medium", "high"] = "medium"


class StaffReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    staff_id: str | None = None


class GuestAnswerIn(BaseModel):
    choice: Literal["accept", "stay"]


class TaskCompleteIn(BaseModel):
    note: str | None = Field(None, max_length=1000)
    staff_id: str | None = None


class ActionResult(BaseModel):
    status: str
    detail: str = ""
    completed: bool | None = None  # doar cu ?wait=true


def build_router(service: IncidentService) -> APIRouter:
    router = APIRouter(tags=["f2 · incidents"])
    ctx = service._ctx  # noqa: SLF001 — routerul face parte din același feature

    async def finish(wait: bool, result: ActionResult) -> ActionResult:
        if wait:
            result.completed = await ctx.bus.join(timeout=ctx.settings.sim_wait_timeout_s)
        return result

    @router.get("/incidents", response_model=list[IncidentView])
    async def list_incidents() -> list[IncidentView]:
        await service.check_deadlines()  # termenele expirate avansează și când nu vine nimic
        return service.list()

    @router.get("/incidents/{incident_id}", response_model=IncidentView)
    async def get_incident(incident_id: str) -> IncidentView:
        view = service.view(incident_id)
        if view is None:
            raise HTTPException(404, f"unknown incident {incident_id}")
        return view

    @router.post("/incidents/report", response_model=ActionResult)
    async def report(body: StaffReportIn, wait: bool = False) -> ActionResult:
        """Sesizare de la personal (aplicația personalului; publică `IssueReported`)."""
        try:
            event = await service.report_issue(
                property_id=body.property_id,
                description=body.description,
                staff_id=body.staff_id,
                booking_id=body.booking_id,
                severity=body.severity,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return await finish(wait, ActionResult(status="published", detail=event.event_id))

    @router.post("/incidents/{incident_id}/staff-reply", response_model=ActionResult)
    async def staff_reply(incident_id: str, body: StaffReplyIn, wait: bool = False) -> ActionResult:
        """Răspunsul personalului în conversația incidentului."""
        try:
            message = await service.receive_staff_reply(incident_id, body.text, staff_id=body.staff_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await finish(wait, ActionResult(status="recorded", detail=message.id))

    @router.post("/incidents/{incident_id}/guest-answer", response_model=ActionResult)
    async def guest_answer(incident_id: str, body: GuestAnswerIn, wait: bool = False) -> ActionResult:
        """Răspunsul oaspetelui la oferta de relocare (acum din simulare sau manual)."""
        try:
            await service.answer_relocation(incident_id, body.choice)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await finish(wait, ActionResult(status="recorded", detail=body.choice))

    @router.post("/incidents/tasks/{task_id}/complete", response_model=StaffTask)
    async def complete_task(task_id: str, body: TaskCompleteIn, wait: bool = False) -> StaffTask:
        """Personalul marchează sarcina terminată (idempotent)."""
        staff = {m.id: m.name for m in ctx.world.list_staff()}
        try:
            task = await service.complete_task(
                task_id, note=body.note, completed_by=staff.get(body.staff_id or "")
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        if wait:
            await ctx.bus.join(timeout=ctx.settings.sim_wait_timeout_s)
            task = service.repo.task(task_id) or task
        return task

    return router
