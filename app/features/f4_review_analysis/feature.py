"""F4 — Analiza recenziilor. Punctul de intrare descoperit automat."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.contracts.events import ReviewAnalysisRequested
from app.core.feature_context import FeatureContext
from app.features.f4_review_analysis.analysis import AGENT_NAME, Insight, ReviewAnalysis
from app.features.f4_review_analysis.mock_llm import policy


def build_router(analysis: ReviewAnalysis) -> APIRouter:
    router = APIRouter(tags=["f4 · review analysis"])

    @router.get("/f4/insights", response_model=list[Insight])
    async def list_insights(property_id: str | None = None, limit: int = 50) -> list[Insight]:
        return analysis.list_insights(property_id, limit)

    return router


def register(ctx: FeatureContext) -> None:
    analysis = ReviewAnalysis(ctx)
    ctx.subscribe(ReviewAnalysisRequested, analysis.on_analysis_requested)
    ctx.register_mock_llm_policy(AGENT_NAME, policy)
    ctx.include_router(build_router(analysis))
