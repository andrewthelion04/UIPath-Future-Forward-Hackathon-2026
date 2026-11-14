"""F4 la granițele cu sursa de recenzii și cu modelul (auditul de integrare, R01–R08)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.apify.reviews import ApifyReviewsAdapter, normalize_review, same_listing
from app.core.clock import utcnow
from app.core.contracts.events import ReviewAnalysisRequested
from app.core.contracts.models import ReviewBatch
from app.features.f4_review_analysis.analysis import (
    NOTHING_TO_FIX,
    AddRecommendationArgs,
    checked_batch,
    quote_found,
)
from app.features.f4_review_analysis.tests.test_phase1 import WIFI, FakeReviews, analyse, review
from app.ports.llm import ChatMessage, ChatResponse, ToolCall, Usage
from app.ports.reviews import ReviewsError
from conftest import app_client, drain, property_by_name


class Scripted:
    """Model controlat de test: `add` decide dacă înregistrează recomandarea înainte de `finish`."""

    name = "scripted"

    def __init__(self, add: bool = True) -> None:
        self.add, self.runs = add, 0

    async def chat(self, request):
        done = [call.name for message in request.messages for call in message.tool_calls]
        if self.add and "add_recommendation" not in done:
            self.runs += 1
            calls = [ToolCall.make("add_recommendation", theme="Wi-Fi", priority="high",
                                   action="Upgrade the unreliable Wi-Fi router.",
                                   quotes=["wifi was unusable"], reason="quoted by a guest")]  # fmt: skip
        else:
            calls = [ToolCall.make(
                "finish", result="Replace all mattresses: 20 guests reported bed bugs.",
                evidence=["Every bed has bugs."], reason="done",
            )]  # fmt: skip
        return ChatResponse(message=ChatMessage(role="assistant", tool_calls=calls), usage=Usage(), model=self.name)


# --- R01: textul liber al modelului nu devine recomandare -----------------------------------


def test_text_and_evidence_from_finish_never_reach_the_host(settings):
    with app_client(settings, reviews=FakeReviews(WIFI), llm=Scripted(add=False)) as client:
        analyse(client, "Old Town Studio")

        (insight,) = client.get("/f4/insights").json()
        assert insight["items"] == [] and insight["recommendations"] == NOTHING_TO_FIX
        assert insight["evidence"] == []
        (notification,) = client.get("/notifications").json()
        assert "bed bugs" not in notification["body"] and "mattresses" not in notification["body"]


def test_saved_evidence_is_only_the_verified_quotes(settings):
    with app_client(settings, reviews=FakeReviews(WIFI), llm=Scripted()) as client:
        analyse(client, "Old Town Studio")

        (insight,) = client.get("/f4/insights").json()
        assert insight["evidence"] == ['Wi-Fi: "wifi was unusable"']


# --- R02: o singură analiză per proprietate la un moment dat -----------------------------------


def test_simultaneous_requests_for_one_property_run_the_agent_once(settings):
    llm = Scripted()
    with app_client(settings, reviews=FakeReviews(WIFI), llm=llm) as client:
        container = client.app.state.container
        studio = property_by_name(client, "Old Town Studio")

        async def twice() -> bool:
            await asyncio.gather(*(
                container.bus.publish(ReviewAnalysisRequested(property_id=studio["id"])) for _ in range(2)
            ))  # fmt: skip
            return await container.bus.join(timeout=10)

        assert client.portal.call(twice)
        assert llm.runs == 1 and len(client.get("/f4/insights").json()) == 1
        titles = sorted(n["title"] for n in client.get("/notifications").json())
        assert titles == ["No new reviews for Old Town Studio", "Review recommendations for Old Town Studio"]


# --- R03 / R08: lotul e despre proprietatea cerută, fiecare recenzie o dată ---------------------


def batch(property_id: str, *reviews) -> ReviewBatch:
    return ReviewBatch(property_id=property_id, source="fake", fetched_at=utcnow(), reviews=list(reviews))


def test_a_batch_about_another_property_is_rejected():
    with pytest.raises(ValueError, match="not p"):
        checked_batch(batch("other", review("1", WIFI, property_id="other")), "p")
    with pytest.raises(ValueError, match="other properties"):
        checked_batch(batch("p", review("1", WIFI), review("2", WIFI, property_id="other")), "p")


def test_repeated_reviews_are_counted_once():
    cleaned = checked_batch(batch("p", review("1", WIFI), review("1", WIFI), review("2", WIFI)), "p")
    assert [r.id for r in cleaned.reviews] == ["1", "2"]


def test_reviews_of_another_property_fall_back_instead_of_being_analysed(settings):
    class WrongProperty:
        name = "fake"

        async def fetch_reviews(self, property_id: str) -> ReviewBatch:
            return batch("someone-else", review("1", WIFI, property_id="someone-else"))

    with app_client(settings, reviews=WrongProperty()) as client:
        analyse(client, "Old Town Studio")

        assert client.get("/f4/snapshots").json() == [] and client.get("/f4/insights").json() == []
        (notification,) = client.get("/notifications").json()
        assert notification["title"] == "Review analysis skipped for Old Town Studio"


# --- R04 / R05: validarea recomandării -------------------------------------------------------------


@pytest.mark.parametrize("fields", [{"theme": "   "}, {"action": " " * 12}, {"theme": " x "}])
def test_blank_theme_or_action_is_rejected(fields):
    valid = {"theme": "Wi-Fi", "priority": "high", "action": "Upgrade the router.",
             "quotes": ["wifi"], "reason": "test"}  # fmt: skip
    with pytest.raises(ValidationError):
        AddRecommendationArgs(**(valid | fields))
    assert AddRecommendationArgs(**(valid | {"theme": "  Wi-Fi  "})).theme == "Wi-Fi"


def test_quote_fragments_must_appear_in_the_order_given():
    reviews = [review("1", "The room was not dirty. The wifi was unusable.")]
    assert quote_found("room was ... wifi was unusable", reviews)
    assert quote_found("not dirty", reviews)
    assert not quote_found("dirty ... not", reviews)  # ordinea inversată schimbă sensul
    assert not quote_found("wifi ... room", reviews)


# --- R03 / R07 / R08 în adaptorul Apify --------------------------------------------------------------


def test_boolean_rating_is_not_a_star_score():
    assert normalize_review({"id": "1", "rating": True, "text": "Nice."}, "p") is None
    assert normalize_review({"id": "1", "rating": 1, "text": "Bad."}, "p").rating == 1


def test_listing_urls_are_compared_by_room():
    listing = "https://www.airbnb.com/rooms/111"
    assert same_listing("https://www.airbnb.ro/rooms/111?source=x", listing)
    assert same_listing(None, listing)  # actorul nu spune de unde vine itemul
    assert not same_listing("https://www.airbnb.com/rooms/222", listing)


async def test_apify_ignores_items_of_another_listing_and_repeated_reviews(container):
    prop = container.world.list_properties()[0]
    listing = "https://www.airbnb.com/rooms/111"
    container.ids.link("airbnb", "listing", listing, prop.id)
    items = [
        {"id": "a", "startUrl": "https://www.airbnb.com/rooms/999", "rating": 1, "text": "Pests elsewhere."},
        {"id": "b", "startUrl": listing, "rating": 4, "text": "Good stay."},
        {"id": "b", "startUrl": listing, "rating": 4, "text": "Good stay."},
    ]

    def adapter(payload):
        return ApifyReviewsAdapter(
            token="test", actor_id="user/actor", ids=container.ids,
            input_template='{"startUrls": [{"url": "{listing_url}"}]}',
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        )  # fmt: skip

    source = adapter(items)
    try:
        fetched = await source.fetch_reviews(prop.id)
        assert [(r.text, r.property_id) for r in fetched.reviews] == [("Good stay.", prop.id)]
    finally:
        await source.aclose()

    only_foreign = adapter(items[:1])
    try:
        with pytest.raises(ReviewsError, match="another listing"):
            await only_foreign.fetch_reviews(prop.id)
    finally:
        await only_foreign.aclose()
    await container.bus.join(timeout=5)


# --- R06: pagina nu anunță „finished” când analiza încă rulează ----------------------------------


def test_the_page_says_when_the_analysis_is_still_running(client, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from ui import api_client

    def get(path, **params):
        return client.get(path, params={k: v for k, v in params.items() if v is not None}).json()

    monkeypatch.setattr(api_client, "get", get)
    monkeypatch.setattr(api_client, "post", lambda *args, **kwargs: {"status": "published", "completed": False})
    page = AppTest.from_file(str(Path(__file__).parents[1] / "ui_page.py")).run()
    page.button[0].click().run()

    assert not page.exception
    assert [w.value for w in page.success] == []
    assert any("still running" in w.value for w in page.warning)
    assert drain(client)
