"""Endpointurile F2."""

from __future__ import annotations

from fastapi import APIRouter

from app.features.f2_incidents.service import Incident, IncidentService


def build_router(service: IncidentService) -> APIRouter:
    router = APIRouter(tags=["f2 · incidents"])

    @router.get("/incidents", response_model=list[Incident])
    async def list_incidents() -> list[Incident]:
        return service.list()

    return router
