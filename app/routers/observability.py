"""Starea aplicației, jurnalul de evenimente și timeline-ul agentului."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.container import Container


def build_router(c: Container) -> APIRouter:
    router = APIRouter(tags=["observability"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        s = c.settings
        return {
            "status": "ok",
            "today": c.clock.today().isoformat(),
            "providers": {
                "pms": s.pms_provider,
                "llm": s.llm_provider,
                "reviews": s.reviews_provider,
                "market": s.market_provider,
            },
            "pms_connections": c.pms.providers(),
            "market_attribution": getattr(c.market, "attribution", None),
            "features": [
                {"name": f.name, "loaded": f.loaded, "error": f.error} for f in c.features
            ],
        }

    @router.get("/events")
    async def list_events(limit: int = 100, correlation_id: str | None = None) -> list[dict]:
        """Evenimentele publicate, cu handlerii care le-au procesat și starea fiecăruia."""
        return c.bus.list_events(limit=limit, correlation_id=correlation_id)

    @router.get("/agent/overview")
    async def agent_overview() -> dict[str, Any]:
        """Indicatorii dashboardului: cât a lucrat agentul singur, cât a cerut hostului, cât a costat."""
        return c.flows.overview()

    @router.get("/agent/flows")
    async def list_flows(limit: int = 50) -> list[dict[str, Any]]:
        """Fluxurile (un declanșator și tot ce a urmat), cele mai recente primele."""
        return c.flows.list_flows(limit=limit)

    @router.get("/agent/flows/{correlation_id}")
    async def get_flow(correlation_id: str) -> dict[str, Any]:
        """Povestea unui flux: evenimente, pașii agentului cu motivul lor, aprobări, mesaje."""
        story = c.flows.story(correlation_id)
        if story is None:
            raise HTTPException(404, f"unknown flow {correlation_id}")
        return story

    @router.get("/agent/decisions")
    async def list_decisions(limit: int = 40) -> list[dict[str, Any]]:
        """Jurnalul deciziilor din toate fluxurile: agent (cu motiv), cod, host."""
        return c.flows.decisions(limit=limit)

    @router.get("/agent/runs")
    async def list_agent_runs(
        limit: int = 50, feature: str | None = None, booking_id: str | None = None
    ) -> list[dict]:
        return c.agent_trace.list_runs(limit=limit, feature=feature, booking_id=booking_id)

    @router.get("/agent/runs/{run_id}")
    async def get_agent_run(run_id: str) -> dict[str, Any]:
        """O rulare cu toți pașii ei: mesaje, unealtă, argumente, rezultat, tokeni, cost."""
        run = c.agent_trace.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"unknown agent run {run_id}")
        return run

    return router
