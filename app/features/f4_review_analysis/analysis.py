"""F4 — Analiza recenziilor: cerere -> recenzii din port -> snapshot -> agent -> recomandări.

Fluxul pentru o proprietate:

1. recenzii proaspete din portul `reviews`, cu timeout; la eșec, ultimul snapshot salvat;
2. dacă recenziile sunt identice cu ultimul snapshot, nu se salvează unul nou; dacă acel
   snapshot are deja o analiză, hostul află că nu s-a schimbat nimic și agentul nu mai rulează;
3. agentul citește statisticile și recenziile prin unelte și înregistrează fiecare recomandare
   prin `add_recommendation` (temă, prioritate, acțiune, citate verificate în cod);
4. analiza se salvează (`f4_insights` + `f4_recommendations`) și se compară cu cea anterioară
   a proprietății: teme noi, care persistă, care au dispărut;
5. hostul primește o notificare.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import SettingsConfigDict

from app.core.agent.loop import AgentSpec
from app.core.agent.tools import RunContext, Tool, ToolArgs
from app.core.clock import utcnow
from app.core.contracts.events import ReviewAnalysisRequested
from app.core.contracts.models import Property, Review, ReviewBatch
from app.core.db import from_json, to_json
from app.core.feature_context import FeatureContext, FeatureSettings
from app.core.ids import new_id
from app.features.f4_review_analysis.events import PropertyReviewAnalysisRequested

logger = logging.getLogger("staypilot.f4")

AGENT_NAME = "f4_review_analysis.recommend"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
MAX_REVIEW_CHARS = 400
MAX_RECOMMENDATIONS = 5
MAX_QUOTES = 3
# Text scris în cod: fără recomandări înregistrate prin unealtă, nimic din textul liber al
# modelului nu ajunge la host ca recomandare.
NOTHING_TO_FIX = "No improvement is recommended: nothing in these reviews was recorded as a problem."

Priority = Literal["high", "medium", "low"]
PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


class F4Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F4_", extra="ignore", env_ignore_empty=True)

    fetch_timeout_s: float = 150.0  # un actor Apify poate dura; peste atât folosim snapshot-ul
    # Timeoutul handlerului care analizează O proprietate: preluare + rularea agentului.
    property_timeout_s: float = 600.0


# --- Modele -------------------------------------------------------------------------


class Recommendation(BaseModel):
    theme: str
    priority: Priority
    action: str
    quotes: list[str] = Field(default_factory=list)


class ThemeChanges(BaseModel):
    """Comparația cu analiza anterioară a aceleiași proprietăți."""

    previous_insight_id: str | None = None
    previous_created_at: datetime | None = None
    new: list[str] = Field(default_factory=list)
    persisting: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)


class Insight(BaseModel):
    id: str
    property_id: str
    snapshot_id: str
    used_fallback: bool = False
    review_count: int = 0
    average_rating: float | None = None
    recommendations: str  # textul lizibil (lista formatată); păstrat pentru compatibilitate
    evidence: list[str] = Field(default_factory=list)
    agent_run_id: str | None = None
    created_at: datetime
    items: list[Recommendation] = Field(default_factory=list)  # sortate după prioritate
    changes: ThemeChanges | None = None  # None la prima analiză a proprietății


class Snapshot(BaseModel):
    id: str
    property_id: str
    source: str
    fetched_at: datetime
    reviews: list[Review]


class SnapshotInfo(BaseModel):
    id: str
    property_id: str
    source: str
    fetched_at: datetime
    review_count: int


# --- Funcții pure ----------------------------------------------------------------------


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


def theme_key(theme: str) -> str:
    """Cheia de comparație a unei teme: „Wi-Fi”, „wifi” și „WiFi ” sunt aceeași temă."""
    return re.sub(r"[^a-z0-9]+", "", theme.lower())


def sort_recommendations(items: list[Recommendation]) -> list[Recommendation]:
    """După prioritate (high, medium, low); la egalitate, în ordinea în care au fost date."""
    return sorted(items, key=lambda item: PRIORITY_RANK[item.priority])


def compare_themes(previous: list[Recommendation], current: list[Recommendation]) -> ThemeChanges:
    before = {theme_key(item.theme): item.theme for item in previous}
    after = {theme_key(item.theme): item.theme for item in current}
    return ThemeChanges(
        new=[theme for key, theme in after.items() if key not in before],
        persisting=[theme for key, theme in after.items() if key in before],
        resolved=[theme for key, theme in before.items() if key not in after],
    )


def same_reviews(snapshot: Snapshot, batch: ReviewBatch) -> bool:
    """Recenziile preluate sunt identice cu snapshot-ul (data preluării nu contează)."""
    if snapshot.source != batch.source:
        return False
    return _canonical(snapshot.reviews) == _canonical(batch.reviews)


def checked_batch(batch: ReviewBatch, property_id: str) -> ReviewBatch:
    """Recenziile aparțin proprietății cerute, fiecare o singură dată.

    Un răspuns despre altă proprietate e o eroare a sursei (se folosește ultimul snapshot), nu
    date de analizat. Recenziile repetate ar denatura numărul, media și frecvența problemelor.
    """
    if batch.property_id != property_id:
        raise ValueError(f"the source returned reviews of {batch.property_id}, not {property_id}")
    foreign = sorted({review.property_id for review in batch.reviews} - {property_id})
    if foreign:
        raise ValueError(f"the source mixed in reviews of other properties: {', '.join(foreign)}")
    unique: dict[str, Review] = {}
    for review in batch.reviews:
        unique.setdefault(review.id, review)
    if len(unique) == len(batch.reviews):
        return batch
    return batch.model_copy(update={"reviews": list(unique.values())})


def _canonical(reviews: list[Review]) -> list[str]:
    return sorted(to_json(review.model_dump(mode="json")) for review in reviews)


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def quote_found(quote: str, reviews: list[Review]) -> bool:
    """Citatul apare într-o recenzie. Fragmentele separate de „...” trebuie să apară toate."""
    cleaned = quote.strip().strip("\"'“”").replace("…", "...")
    fragments = [_normalize(part).strip(" .,;:") for part in cleaned.split("...")]
    fragments = [part for part in fragments if part]
    if not fragments:
        return False
    return any(_in_order(fragments, _normalize(review.text)) for review in reviews)


def _in_order(fragments: list[str], text: str) -> bool:
    """Fragmentele apar în text în ordinea dată, fără să se suprapună.

    „dirty ... not” nu e un citat din „was not dirty”: ordinea inversată schimbă sensul.
    """
    position = 0
    for fragment in fragments:
        found = text.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def format_recommendations(items: list[Recommendation]) -> str:
    return "\n".join(
        f"{i}. [{item.priority.capitalize()}] {item.theme}: {item.action}"
        for i, item in enumerate(items, start=1)
    )


def format_changes(changes: ThemeChanges | None) -> str:
    if changes is None:
        return "First analysis for this property."
    since = (
        f"the analysis of {changes.previous_created_at:%Y-%m-%d %H:%M} UTC"
        if changes.previous_created_at
        else "the previous analysis"
    )
    if not (changes.new or changes.resolved):
        return f"Same themes as {since}." if changes.persisting else f"Nothing to fix, as in {since}."
    parts = []
    if changes.new:
        parts.append("new: " + ", ".join(changes.new))
    if changes.persisting:
        parts.append("still present: " + ", ".join(changes.persisting))
    if changes.resolved:
        parts.append("no longer mentioned: " + ", ".join(changes.resolved))
    return f"Compared with {since} — " + "; ".join(parts) + "."


# --- Uneltele agentului --------------------------------------------------------------


class GetRatingStatsArgs(ToolArgs):
    pass


class GetReviewsArgs(ToolArgs):
    max_rating: float | None = Field(
        None, ge=1, le=5, description="Only reviews rated at most this (e.g. 4 for critical ones)."
    )
    limit: int = Field(10, ge=1, le=25, description="Maximum number of reviews, newest first.")


class AddRecommendationArgs(ToolArgs):
    theme: str = Field(
        ..., min_length=2, max_length=60, description="Short name of the problem area, e.g. 'Wi-Fi'."
    )
    priority: Priority = Field(
        ...,
        description="high = hurts ratings now or many guests mention it; medium = recurring "
        "annoyance; low = minor or single mention.",
    )
    action: str = Field(
        ..., min_length=10, max_length=400, description="The specific action the host should take."
    )
    quotes: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_QUOTES,
        description="Exact fragments copied from the reviews that support this recommendation.",
    )

    @field_validator("theme", "action", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        # Lungimile minime se aplică textului real, nu spațiilor din jur.
        return value.strip() if isinstance(value, str) else value


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

    async def add_recommendation(args: AddRecommendationArgs, run: RunContext) -> dict[str, Any]:
        # Garanțiile stau în cod, nu în prompt: citatele trebuie să existe în recenzii,
        # o temă apare o singură dată, iar numărul de recomandări e limitat.
        reviews: list[Review] = run.state["reviews"]
        missing = [quote for quote in args.quotes if not quote_found(quote, reviews)]
        if missing:
            raise ValueError(
                f"these quotes do not appear in any review: {missing}. "
                "Copy the exact words from get_reviews."
            )
        items: list[Recommendation] = run.state.setdefault("recommendations", [])
        item = Recommendation(
            theme=args.theme.strip(),
            priority=args.priority,
            action=args.action.strip(),
            quotes=[quote.strip() for quote in args.quotes],
        )
        key = theme_key(item.theme)
        for i, existing in enumerate(items):
            if theme_key(existing.theme) == key:
                items[i] = item
                return {"status": "updated", "theme": item.theme, "total": len(items)}
        if len(items) >= MAX_RECOMMENDATIONS:
            raise ValueError(
                f"at most {MAX_RECOMMENDATIONS} recommendations; keep the most impactful "
                "ones or call finish"
            )
        items.append(item)
        return {"status": "added", "theme": item.theme, "total": len(items)}

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
        Tool(
            "add_recommendation",
            "Record one improvement recommendation for the host. Call once per theme; calling "
            "again with the same theme replaces it.",
            AddRecommendationArgs,
            add_recommendation,
        ),
    ]


# --- Serviciul -----------------------------------------------------------------------


class ReviewAnalysis:
    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx
        self.settings = ctx.load_settings(F4Settings)
        self.spec = AgentSpec(
            name=AGENT_NAME, feature=ctx.feature, system_prompt=SYSTEM_PROMPT, tools=build_tools()
        )
        # O singură analiză per proprietate la un moment dat: două cereri simultane nu rulează
        # agentul de două ori, iar analiza pornită prima nu ajunge „ultima” peste una mai nouă.
        self._locks: dict[str, asyncio.Lock] = {}

    # --- Handleri ----------------------------------------------------------------

    async def on_analysis_requested(self, event: ReviewAnalysisRequested) -> None:
        """Desface cererea în câte un eveniment per proprietate (fiecare cu handlerul lui)."""
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
            await ctx.bus.publish(
                PropertyReviewAnalysisRequested(
                    property_id=prop.id,
                    requested_by=event.requested_by,
                    source=f"feature:{ctx.feature}",
                )
            )

    async def on_property_analysis_requested(self, event: PropertyReviewAnalysisRequested) -> None:
        ctx = self._ctx
        prop = ctx.world.get_property(event.property_id)
        if prop is None:
            ctx.notify(
                "Review analysis: unknown property",
                f"Property {event.property_id} no longer exists.",
                level="warning",
            )
            return
        try:
            async with self._locks.setdefault(prop.id, asyncio.Lock()):
                await self._analyse(prop)
        except Exception as exc:  # eroarea e raportată hostului, nu ajunge la event bus
            logger.exception("Review analysis failed for %s", prop.id)
            ctx.notify(
                f"Review analysis failed for {prop.name}",
                f"{type(exc).__name__}: {exc}",
                level="error",
                property_id=prop.id,
            )

    # --- Analiza unei proprietăți ------------------------------------------------

    async def _analyse(self, prop: Property) -> None:
        ctx = self._ctx
        snapshot, used_fallback, problem, unchanged = await self._fetch_snapshot(prop)
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

        origin = f"{snapshot.source} snapshot of {snapshot.fetched_at:%Y-%m-%d %H:%M} UTC"
        if unchanged:
            done = self._latest_insight(prop.id, snapshot_id=snapshot.id)
            if done is not None:
                self._notify_unchanged(prop, snapshot, done, origin)
                return

        previous = self._latest_insight(prop.id)
        state: dict[str, Any] = {"reviews": snapshot.reviews, "recommendations": []}
        result = await ctx.agent.run(
            self.spec,
            objective=self._objective(prop, previous),
            state=state,
            property_id=prop.id,
        )
        if result.status != "finished" or not result.result:
            ctx.notify(
                f"Review analysis did not complete for {prop.name}",
                f"Agent outcome: {result.status} ({result.error or 'no result'}). "
                f"The reviews are saved ({origin}); try again later.",
                level="warning",
                property_id=prop.id,
            )
            return

        items = sort_recommendations(state["recommendations"])
        insight = self._save_insight(prop, snapshot, used_fallback, items, result, previous)

        note = (
            f"Live reviews were unavailable ({problem}); this uses the {origin}."
            if used_fallback
            else f"Based on {insight.review_count} reviews, average {insight.average_rating} ({origin})."
        )
        ctx.notify(
            f"Review recommendations for {prop.name}",
            f"{insight.recommendations}\n\n{format_changes(insight.changes)}\n\n{note}",
            level="warning" if used_fallback else "info",
            property_id=prop.id,
        )

    def _objective(self, prop: Property, previous: Insight | None) -> str:
        objective = (
            f"Analyse the guest reviews of the property '{prop.name}' ({prop.city}) "
            "and recommend improvements to its host."
        )
        if previous and previous.items:
            themes = ", ".join(item.theme for item in previous.items)
            objective += (
                f"\nThemes of the previous analysis: {themes}. "
                "When the same problem still appears, reuse exactly the same theme name."
            )
        return objective

    def _notify_unchanged(
        self, prop: Property, snapshot: Snapshot, done: Insight, origin: str
    ) -> None:
        body = (
            f"The {len(snapshot.reviews)} reviews are identical to the {origin}, so nothing "
            f"changed since the analysis of {done.created_at:%Y-%m-%d %H:%M} UTC and no new "
            "snapshot was saved. Its recommendations still apply:\n\n"
            f"{done.recommendations}"
        )
        self._ctx.notify(
            f"No new reviews for {prop.name}", body, level="info", property_id=prop.id
        )

    # --- Preluare și snapshot-uri ------------------------------------------------

    async def _fetch_snapshot(self, prop: Property) -> tuple[Snapshot | None, bool, str, bool]:
        """(snapshot, used_fallback, problem, unchanged).

        Recenzii proaspete din port; la eșec sau timeout, ultimul snapshot salvat. Dacă
        recenziile proaspete sunt identice cu ultimul snapshot, acesta e refolosit.
        """
        try:
            batch = await asyncio.wait_for(
                self._ctx.reviews.fetch_reviews(prop.id), timeout=self.settings.fetch_timeout_s
            )
            batch = checked_batch(batch, prop.id)
        except Exception as exc:
            problem = (
                f"timeout after {self.settings.fetch_timeout_s:g}s"
                if isinstance(exc, TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            logger.warning("Fetching reviews for %s failed: %s", prop.id, problem)
            return self.latest_snapshot(prop.id), True, problem, False
        latest = self.latest_snapshot(prop.id)
        if latest is not None and same_reviews(latest, batch):
            return latest, False, "", True
        return self._save_snapshot(batch), False, "", False

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

    # --- Salvarea analizei -------------------------------------------------------

    def _save_insight(
        self,
        prop: Property,
        snapshot: Snapshot,
        used_fallback: bool,
        items: list[Recommendation],
        result: Any,
        previous: Insight | None,
    ) -> Insight:
        stats = rating_stats(snapshot.reviews)
        # Dovezile sunt doar citatele verificate în cod; `evidence` din `finish` e text liber
        # al modelului și poate fi inventat, deci nu se salvează.
        evidence = [f'{item.theme}: "{quote}"' for item in items for quote in item.quotes]
        changes = None
        if previous is not None:
            changes = compare_themes(previous.items, items)
            changes.previous_insight_id = previous.id
            changes.previous_created_at = previous.created_at
        insight = Insight(
            id=new_id("ins"),
            property_id=prop.id,
            snapshot_id=snapshot.id,
            used_fallback=used_fallback,
            review_count=stats["count"],
            average_rating=stats["average"],
            recommendations=format_recommendations(items) if items else NOTHING_TO_FIX,
            evidence=evidence,
            agent_run_id=result.run_id,
            created_at=utcnow(),
            items=items,
            changes=changes,
        )
        db = self._ctx.db
        with db.transaction():
            db.execute(
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
            for position, item in enumerate(items):
                db.execute(
                    "INSERT INTO f4_recommendations (id, insight_id, property_id, position, theme, "
                    "theme_key, priority, priority_rank, action, quotes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("rec"),
                        insight.id,
                        prop.id,
                        position,
                        item.theme,
                        theme_key(item.theme),
                        item.priority,
                        PRIORITY_RANK[item.priority],
                        item.action,
                        to_json(item.quotes),
                    ),
                )
        return insight

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

    def list_snapshots(self, property_id: str | None = None, limit: int = 50) -> list[SnapshotInfo]:
        where, params = ("WHERE property_id = ? ", (property_id,)) if property_id else ("", ())
        rows = self._ctx.db.query(
            "SELECT id, property_id, source, fetched_at, json_array_length(reviews) AS review_count "
            f"FROM f4_review_snapshots {where}ORDER BY fetched_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        return [SnapshotInfo(**row) for row in rows]

    def list_insights(self, property_id: str | None = None, limit: int = 50) -> list[Insight]:
        where, params = ("WHERE property_id = ? ", (property_id,)) if property_id else ("", ())
        rows = self._ctx.db.query(
            f"SELECT * FROM f4_insights {where}ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        insights = [self._to_insight(row) for row in rows]
        for insight in insights:
            insight.changes = self._changes_for(insight)
        return insights

    def _latest_insight(self, property_id: str, snapshot_id: str | None = None) -> Insight | None:
        if snapshot_id:
            row = self._ctx.db.query_one(
                "SELECT * FROM f4_insights WHERE property_id = ? AND snapshot_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (property_id, snapshot_id),
            )
        else:
            row = self._ctx.db.query_one(
                "SELECT * FROM f4_insights WHERE property_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (property_id,),
            )
        return self._to_insight(row) if row else None

    def _changes_for(self, insight: Insight) -> ThemeChanges | None:
        """Comparația cu analiza imediat anterioară a proprietății (calculată la citire)."""
        row = self._ctx.db.query_one(
            "SELECT * FROM f4_insights WHERE property_id = ? AND "
            "rowid < (SELECT rowid FROM f4_insights WHERE id = ?) "
            "ORDER BY rowid DESC LIMIT 1",
            (insight.property_id, insight.id),
        )
        if row is None:
            return None
        previous = self._to_insight(row)
        changes = compare_themes(previous.items, insight.items)
        changes.previous_insight_id = previous.id
        changes.previous_created_at = previous.created_at
        return changes

    def _to_insight(self, row: dict[str, Any]) -> Insight:
        row = dict(row)
        row["evidence"] = from_json(row["evidence"], [])
        row["used_fallback"] = bool(row["used_fallback"])
        rec_rows = self._ctx.db.query(
            "SELECT theme, priority, action, quotes FROM f4_recommendations "
            "WHERE insight_id = ? ORDER BY priority_rank, position",
            (row["id"],),
        )
        row["items"] = [
            Recommendation(
                theme=r["theme"], priority=r["priority"], action=r["action"],
                quotes=from_json(r["quotes"], []),
            )
            for r in rec_rows
        ]  # fmt: skip
        return Insight(**row)
