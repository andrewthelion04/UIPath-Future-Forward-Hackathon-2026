"""F1 — Integrare PMS (doar transport). Punctul de intrare descoperit automat."""

from __future__ import annotations

from app.core.feature_context import FeatureContext
from app.features.f1_pms_integration.inbound import InboundPipeline
from app.features.f1_pms_integration.router import build_router


def register(ctx: FeatureContext) -> None:
    pipeline = InboundPipeline(ctx)
    ctx.include_router(build_router(ctx, pipeline))
