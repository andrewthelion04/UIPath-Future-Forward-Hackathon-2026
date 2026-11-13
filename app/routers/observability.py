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
