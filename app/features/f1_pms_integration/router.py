"""Endpointurile F1: webhook-urile PMS și mesajul simulat de la oaspete."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.feature_context import FeatureContext
from app.features.f1_pms_integration.inbound import InboundPipeline, InboundResult
from app.ports.simulation import SimulationError


class SimGuestMessageIn(BaseModel):
    booking_id: str
    text: str = Field(min_length=1, max_length=4000)


class InboundResponse(InboundResult):
    completed: bool | None = None  # doar cu ?wait=true


def build_router(ctx: FeatureContext, pipeline: InboundPipeline) -> APIRouter:
    router = APIRouter(tags=["f1 · pms integration"])

    @router.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str, request: Request, token: str | None = None
    ) -> dict[str, str]:
        """Webhook de la PMS. Răspunde mereu 200; handlerii rulează în background."""
        expected = ctx.settings.webhook_token
        if expected is not None and not hmac.compare_digest(
            (token or "").encode(), expected.get_secret_value().encode()
        ):
            raise HTTPException(403, "invalid webhook token")
        try:
            payload: Any = await request.json()
        except Exception:
            raw = (await request.body()).decode("utf-8", errors="replace")
            payload = {"_unparsed_body": raw[:10_000]}
        result = await pipeline.process(provider, payload, source=f"webhook:{provider}")
        return {"status": result.outcome.value, "detail": result.detail}

    @router.post("/sim/guest-message", response_model=InboundResponse)
    async def sim_guest_message(body: SimGuestMessageIn, wait: bool = False) -> InboundResponse:
        """Mesaj simulat: oaspetele scrie în PMS-ul mock, care „trimite” webhook-ul."""
        if ctx.world.get_booking(body.booking_id) is None:
            raise HTTPException(404, f"unknown booking {body.booking_id}")
        try:
            payload = await ctx.simulation.guest_message_payload(body.booking_id, body.text)
        except SimulationError as exc:
            raise HTTPException(400, str(exc)) from exc
        result = await pipeline.process("mock", payload, source="simulator")
        response = InboundResponse(**result.model_dump())
        if wait:
            response.completed = await ctx.bus.join(timeout=ctx.settings.sim_wait_timeout_s)
        return response

    @router.get("/f1/webhook-log")
    async def webhook_log(limit: int = 50) -> list[dict[str, Any]]:
        """Payload-urile brute primite și ce s-a întâmplat cu fiecare."""
        return pipeline.log(limit)

    return router
