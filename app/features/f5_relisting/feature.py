"""F5 — Relistare după anulare. Punctul de intrare descoperit automat."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.contracts.events import BookingCancelled
from app.core.feature_context import FeatureContext
from app.features.f5_relisting.relisting import APPROVAL_KIND, Relisting, RelistProposal


def build_router(relisting: Relisting) -> APIRouter:
    router = APIRouter(tags=["f5 · relisting"])

    @router.get("/f5/proposals", response_model=list[RelistProposal])
    async def list_proposals(limit: int = Query(default=50, ge=1, le=200)) -> list[RelistProposal]:
        return relisting.list(limit)

    return router


def register(ctx: FeatureContext) -> None:
    relisting = Relisting(ctx)
    ctx.subscribe(BookingCancelled, relisting.on_booking_cancelled)
    ctx.on_decision(APPROVAL_KIND, relisting.on_decision)
    ctx.include_router(build_router(relisting))
