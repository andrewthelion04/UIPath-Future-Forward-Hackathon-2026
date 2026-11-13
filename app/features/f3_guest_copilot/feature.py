"""F3 — Copilot oaspeți. Punctul de intrare descoperit automat."""

from __future__ import annotations

from app.core.contracts.events import GuestMessageReceived
from app.core.feature_context import FeatureContext
from app.features.f3_guest_copilot.copilot import AGENT_NAME, GuestCopilot
from app.features.f3_guest_copilot.mock_llm import policy
from app.features.f3_guest_copilot.tools import APPROVAL_KIND


def register(ctx: FeatureContext) -> None:
    copilot = GuestCopilot(ctx)
    ctx.subscribe(GuestMessageReceived, copilot.on_guest_message)
    ctx.on_decision(APPROVAL_KIND, copilot.on_guest_request_decision)
    ctx.register_mock_llm_policy(AGENT_NAME, policy)
