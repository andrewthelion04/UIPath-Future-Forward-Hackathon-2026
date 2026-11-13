"""Conexiunile PMS: sincronizare și corespondențele de ID-uri (pentru depanare)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.container import Container
from app.core.id_mapper import MappingConflict
from app.core.sync_service import SyncReport, UnknownProvider
from app.ports.pms import PmsCapabilities


class ConnectionInfo(BaseModel):
    provider: str
    selected: bool  # providerul ales în config (`PMS_PROVIDER`)
    capabilities: PmsCapabilities


class MappingIn(BaseModel):
    connection: str  # ex. "mock", "hospitable", "airbnb"
    entity_type: str  # ex. "property", "reservation", "listing"
    external_id: str
    internal_id: str


def build_router(c: Container) -> APIRouter:
    router = APIRouter(prefix="/integrations", tags=["integrations"])

    @router.get("", response_model=list[ConnectionInfo])
    async def list_connections() -> list[ConnectionInfo]:
        return [
            ConnectionInfo(
                provider=conn.provider,
                selected=conn.provider == c.settings.pms_provider,
                capabilities=conn.capabilities,
            )
            for conn in c.pms.all()
        ]

    @router.get("/mappings")
    async def list_mappings(connection: str | None = None) -> list[dict[str, Any]]:
        return c.ids.list(connection)

    @router.put("/mappings")
    async def put_mapping(body: MappingIn) -> dict[str, str]:
        """Mapare manuală (depanare), ex. proprietate internă -> URL de anunț Airbnb."""
        try:
            c.ids.link(body.connection, body.entity_type, body.external_id, body.internal_id)
        except MappingConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "linked"}

    @router.post("/{pms_provider}/sync", response_model=SyncReport)
    async def sync(pms_provider: str) -> SyncReport:
        """Sincronizează proprietățile și rezervările conexiunii. Repetabil fără dubluri."""
        try:
            return await c.sync.sync(pms_provider)
        except UnknownProvider as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:  # eroare a PMS-ului extern: nu doborâm API-ul
            raise HTTPException(502, f"sync with {pms_provider} failed: {exc}") from exc

    return router
