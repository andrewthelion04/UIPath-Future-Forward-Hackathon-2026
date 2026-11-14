"""Politica deterministă a LLM-ului mock pentru agentul F4.

Citește statisticile și recenziile critice prin unelte, grupează problemele pe teme după
cuvinte-cheie, înregistrează câte o recomandare per temă prin `add_recommendation` (toate
într-un singur mesaj, ca rundele să rămână puține) și încheie cu `finish`. Suficient pentru
demo și teste fără cheie API.
"""

from __future__ import annotations

from typing import Any

from app.core.agent.mock_support import call, called_tools, failed, finish, last_result
from app.ports.llm import ChatMessage, ChatRequest, ToolCall

# temă -> (cuvinte-cheie, recomandare)
THEMES: list[tuple[str, tuple[str, ...], str]] = [
    (
        "Wi-Fi",
        ("wifi", "wi-fi", "internet"),
        "Unstable or slow Wi-Fi: test the speed in every room, then upgrade the router or the plan.",
    ),
    (
        "Check-in",
        ("check-in", "check in", "lockbox", "key", "instructions"),
        "Confusing arrival: send step-by-step check-in instructions with photos of the lockbox.",
    ),
    (
        "Cleanliness",
        ("not properly clean", "not clean", "wasn't clean", "dust", "dirty", "stain"),
        "Cleanliness slips: add a photo checklist for the cleaner, with the bathroom and under the bed on it.",
    ),
    (
        "Noise",
        ("nois", "loud"),
        "Noise complaints: mention it honestly in the listing and provide earplugs or better window sealing.",
    ),
    (
        "Heating and hot water",
        ("heating", "hot water", "lukewarm", "cold"),
        "Heating or hot water falls short: service the boiler and pre-heat the place before arrival.",
    ),
    (
        "Beds",
        ("mattress", "bed "),
        "Sleep comfort: replace the mattress guests mention or add a firm topper.",
    ),
]
MAX_THEMES = 4
QUOTE_CHARS = 160


def policy(request: ChatRequest) -> ChatMessage:
    done = called_tools(request)
    if "get_rating_stats" not in done:
        return call("get_rating_stats", reason="The numbers must come from the data, not from me.")
    if "get_reviews" not in done:
        return call(
            "get_reviews",
            max_rating=4,
            limit=15,
            reason="Improvement ideas come from the reviews that are not perfect.",
        )

    stats = last_result(request, "get_rating_stats")
    reviews = last_result(request, "get_reviews")
    if failed(stats) or failed(reviews) or not isinstance(reviews, list):
        return finish(
            "The reviews could not be read, so no recommendations were produced.",
            [str(stats), str(reviews)],
            reason="The tools failed and I must not invent findings.",
        )

    found = _recommend(reviews)
    summary = (
        f"{stats.get('count')} reviews, average rating {stats.get('average')}."
        if isinstance(stats, dict)
        else ""
    )
    if not found:
        return finish(
            f"{summary} Guests report nothing that needs fixing; keep the current standard.",
            [],
            reason="No recurring problem appears in the reviews.",
        )
    if "add_recommendation" not in done:
        return ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall.make(
                    "add_recommendation",
                    theme=item["theme"],
                    priority=item["priority"],
                    action=item["action"],
                    quotes=item["quotes"],
                    reason=f"{len(item['quotes'])} critical review(s) mention {item['theme']}.",
                )
                for item in found
            ],
        )
    return finish(
        f"{summary} {len(found)} recommendation(s) recorded, most impactful first.",
        [f'{item["theme"]}: "{item["quotes"][0]}"' for item in found],
        reason="Each recommendation is recorded and tied to what guests wrote.",
    )


def _recommend(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for theme, keywords, advice in THEMES:
        matching = [
            review
            for review in reviews
            if any(word in str(review.get("text", "")).lower() for word in keywords)
        ]
        if not matching:
            continue
        lowest = min(float(review.get("rating") or 5) for review in matching)
        priority = "high" if len(matching) >= 2 or lowest <= 2 else "medium" if lowest <= 3 else "low"
        quotes = [str(review.get("text", ""))[:QUOTE_CHARS] for review in matching[:2]]
        scored.append(
            (len(matching), {"theme": theme, "priority": priority, "action": advice, "quotes": quotes})
        )
    scored.sort(key=lambda item: -item[0])
    return [item for _, item in scored[:MAX_THEMES]]
