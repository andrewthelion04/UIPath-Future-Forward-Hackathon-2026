"""Uneltele agentului F5 pentru trierea anulărilor și propunerea unei relistări."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.agent.tools import RunContext, Tool, ToolArgs

if TYPE_CHECKING:
    from app.core.contracts.events import BookingCancelled
    from app.features.f5_relisting.relisting import Relisting

AGENT_NAME = "f5_relisting.reprice"
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")


class InspectCancellationArgs(ToolArgs):
    pass


class SearchComparablesArgs(ToolArgs):
    pass


class PrepareRelistingArgs(ToolArgs):
    comparable_ref: str


def build_tools(workflow: Relisting) -> list[Tool]:
    async def inspect_cancellation(
        _: InspectCancellationArgs, run: RunContext
    ) -> dict[str, Any]:
        event: BookingCancelled = run.state["event"]
        return await workflow._agent_inspect_cancellation(event, run)

    async def search_comparables(
        _: SearchComparablesArgs, run: RunContext
    ) -> dict[str, Any]:
        return await workflow._agent_search_comparables(run)

    async def prepare_relisting(
        args: PrepareRelistingArgs, run: RunContext
    ) -> dict[str, Any]:
        return await workflow._agent_prepare_relisting(args.comparable_ref, run)

    return [
        Tool(
            "inspect_cancellation",
            "Validate the cancelled booking, check-in deadline, host's last-minute window, "
            "and the released calendar nights. Call this first.",
            InspectCancellationArgs,
            inspect_cancellation,
        ),
        Tool(
            "search_comparables",
            "Search the configured market data source for listings that satisfy the property's "
            "bedroom, capacity, amenity, and currency requirements. Only call after an eligible "
            "cancellation is confirmed.",
            SearchComparablesArgs,
            search_comparables,
        ),
        Tool(
            "prepare_relisting",
            "Create a price proposal for the cheapest valid comparable and request host approval. "
            "This never writes a price to the calendar.",
            PrepareRelistingArgs,
            prepare_relisting,
        ),
    ]
