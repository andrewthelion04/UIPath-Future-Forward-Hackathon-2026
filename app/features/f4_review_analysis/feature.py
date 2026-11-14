"""F4 — Analiza recenziilor. Punctul de intrare descoperit automat."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.contracts.events import ReviewAnalysisRequested
from app.core.feature_context import FeatureContext
from app.features.f4_review_analysis.analysis import (
    AGENT_NAME,
    Insight,
    ReviewAnalysis,
    SnapshotInfo,
)
from app.features.f4_review_analysis.events import PropertyReviewAnalysisRequested
from app.features.f4_review_analysis.mock_llm import policy


def build_router(analysis: ReviewAnalysis) -> APIRouter:
    router = APIRouter(tags=["f4 · review analysis"])

    @router.get("/f4/insights", response_model=list[Insight])
    async def list_insights(property_id: str | None = None, limit: int = 50) -> list[Insight]:
        """Analizele, cele mai noi primele; recomandările din `items` sunt sortate după prioritate."""
        return analysis.list_insights(property_id, limit)

    @router.get("/f4/snapshots", response_model=list[SnapshotInfo])
    async def list_snapshots(property_id: str | None = None, limit: int = 50) -> list[SnapshotInfo]:
        """Snapshot-urile de recenzii salvate (fără textul recenziilor)."""
        return analysis.list_snapshots(property_id, limit)

    return router


def register(ctx: FeatureContext) -> None:
    analysis = ReviewAnalysis(ctx)
    # Cererea comună doar se desface în câte un eveniment privat per proprietate.
    ctx.subscribe(ReviewAnalysisRequested, analysis.on_analysis_requested)
    # Fiecare proprietate are handlerul și timeoutul ei (preluare + agent).
    ctx.subscribe(
        PropertyReviewAnalysisRequested,
        analysis.on_property_analysis_requested,
        timeout_s=analysis.settings.property_timeout_s,
    )
    ctx.register_mock_llm_policy(AGENT_NAME, policy)
    ctx.include_router(build_router(analysis))
