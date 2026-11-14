"""Politica mock pentru agentul F5; păstrează demo-ul determinist și fără chei."""

from __future__ import annotations

from app.core.agent.mock_support import call, called_tools, failed, finish, last_result
from app.ports.llm import ChatMessage, ChatRequest


def policy(request: ChatRequest) -> ChatMessage:
    done = called_tools(request)
    if "inspect_cancellation" not in done:
        return call(
            "inspect_cancellation",
            reason="I need to verify the cancelled booking, deadline, and calendar before acting.",
        )

    context = last_result(request, "inspect_cancellation")
    if failed(context):
        return finish(
            "The cancellation could not be verified; no price was proposed.",
            [str(context)],
            reason="The inspection tool failed, so I must not infer booking facts.",
        )
    if context.get("status") != "eligible":
        return finish(
            f"No relisting proposal was created: {context.get('status', 'cancellation not eligible')}.",
            [str(context)],
            reason="The verified cancellation does not qualify for market repricing.",
        )

    if "search_comparables" not in done:
        return call(
            "search_comparables",
            reason="The cancellation qualifies; I need market listings before recommending a price.",
        )

    search = last_result(request, "search_comparables")
    if failed(search) or search.get("status") != "ready":
        return finish(
            "No relisting price was proposed because comparable market data is unavailable.",
            [str(search)],
            reason="Without validated comparables, a price must not be invented.",
        )

    listings = search["comparables"]
    cheapest = min(listings, key=lambda item: (item["nightly_price"], item["ref"]))
    if "prepare_relisting" not in done:
        return call(
            "prepare_relisting",
            comparable_ref=cheapest["ref"],
            reason="The tool-validated cheapest comparable is the safest reference for the proposal.",
        )

    proposal = last_result(request, "prepare_relisting")
    if failed(proposal) or proposal.get("status") != "awaiting_approval":
        return finish(
            "The agent could not prepare a host approval; no calendar price was changed.",
            [str(proposal)],
            reason="The proposal tool did not confirm an approval request.",
        )
    return finish(
        "The nightly price proposal is ready and is waiting for the host's approval.",
        [
            f"Comparable: {proposal['comparable_title']} at "
            f"{proposal['comparable_price']:g} {proposal['currency']}/night.",
            f"Proposed nightly prices: {proposal['price_label']}.",
        ],
        reason="A validated proposal was submitted for human approval; no price was written.",
    )
