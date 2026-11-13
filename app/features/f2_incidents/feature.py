"""F2 — Incidente. Punctul de intrare descoperit automat."""

from __future__ import annotations

from app.core.contracts.events import IssueReported
from app.core.feature_context import FeatureContext
from app.features.f2_incidents.router import build_router
from app.features.f2_incidents.service import APPROVAL_KIND, IncidentService


def register(ctx: FeatureContext) -> None:
    service = IncidentService(ctx)
    ctx.subscribe(IssueReported, service.on_issue_reported)
    ctx.on_decision(APPROVAL_KIND, service.on_decision)
    ctx.include_router(build_router(service))
