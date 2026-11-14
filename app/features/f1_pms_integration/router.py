"""Endpointurile F1: webhook-urile PMS și mesajul simulat de la oaspete."""

from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.feature_context import FeatureContext
from app.features.f1_pms_integration.inbound import InboundPipeline, InboundResult
from app.features.f1_pms_integration.settings import F1Settings
from app.ports.simulation import SimulationError


class SimGuestMessageIn(BaseModel):
    booking_id: str
    text: str = Field(min_length=1, max_length=4000)


class InboundResponse(InboundResult):
    completed: bool | None = None  # doar cu ?wait=true


def build_router(
    ctx: FeatureContext, pipeline: InboundPipeline, settings: F1Settings
) -> APIRouter:
    @asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await pipeline.close()

    router = APIRouter(tags=["f1 · pms integration"], lifespan=lifespan)
    allowed_networks = tuple(
        ip_network(cidr) for cidr in (settings.hospitable_webhook_allow_cidrs or "").split(",")
        if cidr
    )

    def authenticate_provider(provider: str, request: Request) -> None:
        if provider == "ownerrez" and settings.ownerrez_webhook_username is not None:
            scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
            username, password = b"", b""
            valid = False
            if scheme.lower() == "basic":
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                    username, separator, password = decoded.partition(b":")
                    valid = bool(separator)
                except (binascii.Error, ValueError):
                    pass
            expected_password = settings.ownerrez_webhook_password
            assert expected_password is not None  # F1Settings validează perechea la încărcare.
            username_matches = hmac.compare_digest(
                username, settings.ownerrez_webhook_username.encode("utf-8")
            )
            password_matches = hmac.compare_digest(
                password, expected_password.get_secret_value().encode("utf-8")
            )
            if not (valid & username_matches & password_matches):
                raise HTTPException(
                    401, "invalid webhook credentials", headers={"WWW-Authenticate": "Basic"}
                )
        if provider == "hospitable" and allowed_networks:
            # IP-ul vine din conexiune; antetele Forwarded/X-Forwarded-For nu sunt de încredere.
            try:
                address = ip_address(request.client.host) if request.client else None
            except ValueError:
                address = None
            if address is None or not any(address in network for network in allowed_networks):
                raise HTTPException(403, "webhook source IP is not allowed")

    @router.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str, request: Request, token: str | None = None
    ) -> dict[str, str]:
        """Payload autentificat: răspunde 200; handlerii evenimentelor rulează în background."""
        expected = ctx.settings.webhook_token
        if expected is not None and not hmac.compare_digest(
            (token or "").encode(), expected.get_secret_value().encode()
        ):
            raise HTTPException(403, "invalid webhook token")
        authenticate_provider(provider, request)
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

    @router.post("/f1/reprocess-unmapped")
    async def reprocess_unmapped(
        provider: str | None = None, wait: bool = False
    ) -> dict[str, Any]:
        """Reîncearcă mesajele nemapate, pentru o conexiune sau pentru toate."""
        result = await pipeline.reprocess_unmapped(provider)
        if wait:
            result["completed"] = await ctx.bus.join(timeout=ctx.settings.sim_wait_timeout_s)
        return result

    return router
