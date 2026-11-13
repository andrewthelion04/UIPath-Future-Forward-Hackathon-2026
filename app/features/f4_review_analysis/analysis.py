"""F4 — Analiza recenziilor: cerere -> recenzii din port -> snapshot -> agent -> recomandări."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from app.core.agent.loop import AgentSpec
from app.core.agent.tools import RunContext, Tool, ToolArgs
from app.core.clock import utcnow
from app.core.contracts.events import ReviewAnalysisRequested
from app.core.contracts.models import Property, Review, ReviewBatch
from app.core.db import from_json, to_json
from app.core.feature_context import FeatureContext, FeatureSettings
from app.core.ids import new_id

logger = logging.getLogger("staypilot.f4")

AGENT_NAME = "f4_review_analysis.recommend"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
MAX_REVIEW_CHARS = 400


class F4Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F4_", extra="ignore", env_ignore_empty=True)

    fetch_timeout_s: float = 150.0  # un actor Apify poate dura; peste atât folosim snapshot-ul


class Insight(BaseModel):
    id: str
    property_id: str
    snapshot_id: str
    used_fallback: bool = False
    review_count: int = 0
    average_rating: float | None = None
    recommendations: str
    evidence: list[str] = Field(default_factory=list)
    agent_run_id: str | None = None
    created_at: datetime


class Snapshot(BaseModel):
    id: str
    property_id: str
    source: str
    fetched_at: datetime
    reviews: list[Review]


# --- Uneltele agentului --------------------------------------------------------------


class GetRatingStatsArgs(ToolArgs):
    pass


class GetReviewsArgs(ToolArgs):
    max_rating: float | None = Field(
        None, ge=1, le=5, description="Only reviews rated at most this (e.g. 4 for critical ones)."
    )
    limit: int = Field(10, ge=1, le=25, description="Maximum number of reviews, newest first.")


def rating_stats(reviews: list[Review]) -> dict[str, Any]:
    ratings = [review.rating for review in reviews]
    dates = sorted(r.reviewed_on for r in reviews if r.reviewed_on)
    return {
        "count": len(ratings),
        "average": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "distribution": {str(star): sum(1 for r in ratings if round(r) == star) for star in range(5, 0, -1)},
        "oldest": dates[0].isoformat() if dates else None,
        "newest": dates[-1].isoformat() if dates else None,
    }


def build_tools() -> list[Tool]:
    async def get_rating_stats(_: GetRatingStatsArgs, run: RunContext) -> dict[str, Any]:
        return rating_stats(run.state["reviews"])

    async def get_reviews(args: GetReviewsArgs, run: RunContext) -> list[dict[str, Any]]:
        reviews: list[Review] = run.state["reviews"]
        if args.max_rating is not None:
            reviews = [r for r in reviews if r.rating <= args.max_rating]
        reviews = sorted(reviews, key=lambda r: r.reviewed_on or datetime.min.date(), reverse=True)
        return [
            {
                "rating": r.rating,
                "date": r.reviewed_on.isoformat() if r.reviewed_on else None,
                "text": r.text[:MAX_REVIEW_CHARS],
            }
            for r in reviews[: args.limit]
        ]

    return [
        Tool(
            "get_rating_stats",
            "Review count, average rating and rating distribution for this property.",
            GetRatingStatsArgs,
            get_rating_stats,
        ),
        Tool(
            "get_reviews",
            "Guest reviews of this property, newest first, optionally only the critical ones.",
            GetReviewsArgs,
            get_reviews,
        ),
    ]


# --- Serviciul -----------------------------------------------------------------------


class ReviewAnalysis:
    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx
        self._settings = ctx.load_settings(F4Settings)
        self.spec = AgentSpec(
            name=AGENT_NAME, feature=ctx.feature, system_prompt=SYSTEM_PROMPT, tools=build_tools()
        )

    async def on_analysis_requested(self, event: ReviewAnalysisRequested) -> None:
        ctx = self._ctx
        if event.property_id:
            prop = ctx.world.get_property(event.property_id)
            properties = [prop] if prop else []
        else:
            properties = ctx.world.list_properties()
        if not properties:
            ctx.notify("Review analysis: no property to analyse", level="warning")
            return
        for prop in properties:
            try:
                await self._analyse(prop)
            except Exception as exc:  # o proprietate care eșuează nu le oprește pe celelalte
                logger.exception("Review analysis failed for %s", prop.id)
                ctx.notify(
                    f"Review analysis failed for {prop.name}",
                    f"{type(exc).__name__}: {exc}",
                    level="error",
                    property_id=prop.id,
                )

    async def _analyse(self, prop: Property) -> None:
        ctx = self._ctx
        snapshot, used_fallback, problem = await self._fetch_snapshot(prop)
        if snapshot is None:
            ctx.notify(
                f"Review analysis skipped for {prop.name}",
                f"Reviews are unavailable ({problem}) and no earlier snapshot exists.",
                level="warning",
                property_id=prop.id,
            )
            return
        if not snapshot.reviews:
            ctx.notify(
                f"No reviews yet for {prop.name}",
                f"Source: {snapshot.source}. Nothing to analyse.",
                property_id=prop.id,
            )
            return

        result = await ctx.agent.run(
            self.spec,
            objective=(
                f"Analyse the guest reviews of the property '{prop.name}' ({prop.city}) "
                "and recommend improvements to its host."
            ),
            state={"reviews": snapshot.reviews},
            property_id=prop.id,
        )
        origin = f"{snapshot.source} snapshot of {snapshot.fetched_at:%Y-%m-%d %H:%M} UTC"
        if result.status != "finished" or not result.result:
            ctx.notify(
                f"Review analysis did not complete for {prop.name}",
                f"Agent outcome: {result.status} ({result.error or 'no result'}). "
                f"The reviews are saved ({origin}); try again later.",
                level="warning",
                property_id=prop.id,
            )
            return

        stats = rating_stats(snapshot.reviews)
        insight = Insight(
            id=new_id("ins"),
            property_id=prop.id,
            snapshot_id=snapshot.id,
            used_fallback=used_fallback,
            review_count=stats["count"],
            average_rating=stats["average"],
            recommendations=result.result,
            evidence=result.evidence,
            agent_run_id=result.run_id,
            created_at=utcnow(),
        )
        ctx.db.execute(
            "INSERT INTO f4_insights (id, property_id, snapshot_id, used_fallback, review_count, "
            "average_rating, recommendations, evidence, agent_run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                insight.id,
                insight.property_id,
                insight.snapshot_id,
                int(insight.used_fallback),
                insight.review_count,
                insight.average_rating,
                insight.recommendations,
                to_json(insight.evidence),
                insight.agent_run_id,
                insight.created_at.isoformat(),
            ),
        )
        note = (
            f"\n\nLive reviews were unavailable ({problem}); this uses the {origin}."
            if used_fallback
            else f"\n\nBased on {stats['count']} reviews, average {stats['average']} ({origin})."
        )
        ctx.notify(
            f"Review recommendations for {prop.name}",
            insight.recommendations + note,
            level="warning" if used_fallback else "info",
            property_id=prop.id,
        )

    async def _fetch_snapshot(self, prop: Property) -> tuple[Snapshot | None, bool, str]:
        """Recenzii proaspete din port; la eșec sau timeout, ultimul snapshot salvat."""
        try:
            batch = await asyncio.wait_for(
                self._ctx.reviews.fetch_reviews(prop.id), timeout=self._settings.fetch_timeout_s
            )
        except Exception as exc:
            problem = (
                f"timeout after {self._settings.fetch_timeout_s:g}s"
                if isinstance(exc, TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            logger.warning("Fetching reviews for %s failed: %s", prop.id, problem)
            return self.latest_snapshot(prop.id), True, problem
        return self._save_snapshot(batch), False, ""

    def _save_snapshot(self, batch: ReviewBatch) -> Snapshot:
        snapshot = Snapshot(
            id=new_id("snap"),
            property_id=batch.property_id,
            source=batch.source,
            fetched_at=batch.fetched_at,
            reviews=batch.reviews,
        )
        self._ctx.db.execute(
            "INSERT INTO f4_review_snapshots (id, property_id, source, fetched_at, reviews) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.id,
                snapshot.property_id,
                snapshot.source,
                snapshot.fetched_at.isoformat(),
                to_json([review.model_dump(mode="json") for review in snapshot.reviews]),
            ),
        )
        return snapshot

    # --- Citire --------------------------------------------------------------------

    def latest_snapshot(self, property_id: str) -> Snapshot | None:
        row = self._ctx.db.query_one(
            "SELECT * FROM f4_review_snapshots WHERE property_id = ? "
            "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
            (property_id,),
        )
        if row is None:
            return None
        row["reviews"] = from_json(row["reviews"], [])
        return Snapshot(**row)

    def list_insights(self, property_id: str | None = None, limit: int = 50) -> list[Insight]:
        if property_id:
            rows = self._ctx.db.query(
                "SELECT * FROM f4_insights WHERE property_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (property_id, limit),
            )
        else:
            rows = self._ctx.db.query(
                "SELECT * FROM f4_insights ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            )
        for row in rows:
            row["evidence"] = from_json(row["evidence"], [])
            row["used_fallback"] = bool(row["used_fallback"])
        return [Insight(**row) for row in rows]
