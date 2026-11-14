"""F4 faza 1: recomandări structurate, comparație cu analiza anterioară, snapshot-uri fără
dubluri și analiza tuturor proprietăților câte un eveniment per proprietate."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.core.agent.tools import RunContext
from app.core.clock import utcnow
from app.core.contracts.models import Review, ReviewBatch
from app.features.f4_review_analysis.analysis import (
    AddRecommendationArgs,
    Recommendation,
    build_tools,
    compare_themes,
    quote_found,
    sort_recommendations,
    theme_key,
)
from app.ports.llm import ChatMessage, ChatRequest, ChatResponse, ToolCall, Usage
from app.ports.reviews import ReviewsError
from conftest import app_client, events_of, property_by_name

WIFI = "The wifi was unusable for work."
NOISE = "Very noisy street at night."
HEATING = "The heating barely worked in the morning."


def analyse(client, property_name: str | None = None) -> None:
    prop = property_by_name(client, property_name) if property_name else None
    result = client.post(
        "/sim/review-analysis", params={"wait": True}, json={"property_id": prop["id"] if prop else None}
    ).json()
    assert result["status"] == "published" and result["completed"] is True


def review(rid: str, text: str, rating: float = 3, property_id: str = "p") -> Review:
    return Review(id=rid, property_id=property_id, rating=rating, text=text, source="fake",
                  reviewed_on=date(2026, 5, 20))  # fmt: skip


class FakeReviews:
    """Sursă de recenzii controlată de test: textele se pot schimba între rulări."""

    name = "fake"

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.failing: set[str] = set()

    async def fetch_reviews(self, property_id: str) -> ReviewBatch:
        if property_id in self.failing:
            raise ReviewsError("actor run failed")
        reviews = [review(f"r{i}", text, 3, property_id) for i, text in enumerate(self.texts)]
        return ReviewBatch(property_id=property_id, source=self.name, fetched_at=utcnow(), reviews=reviews)


# --- 1. Recomandări structurate -----------------------------------------------------


def test_recommendations_are_structured_saved_and_sorted_by_priority(client):
    analyse(client, "Old Town Studio")

    (insight,) = client.get("/f4/insights").json()
    items = insight["items"]
    assert [(i["theme"], i["priority"]) for i in items] == [
        ("Wi-Fi", "high"),  # două recenzii o menționează
        ("Check-in", "medium"),
        ("Noise", "low"),
    ]
    assert all(i["action"] and i["quotes"] for i in items)
    assert any("wifi kept dropping" in q for q in items[0]["quotes"])
    assert insight["recommendations"].startswith("1. [High] Wi-Fi:")
    assert insight["changes"] is None  # prima analiză

    rows = client.app.state.container.db.query(
        "SELECT theme, priority FROM f4_recommendations WHERE insight_id = ?", (insight["id"],)
    )
    assert len(rows) == 3  # câte un rând per recomandare

    (notification,) = client.get("/notifications").json()
    assert "First analysis for this property." in notification["body"]


def test_sorting_puts_high_before_medium_before_low_and_keeps_order_on_ties():
    items = [
        Recommendation(theme="A", priority="low", action="x" * 10, quotes=["q"]),
        Recommendation(theme="B", priority="high", action="x" * 10, quotes=["q"]),
        Recommendation(theme="C", priority="medium", action="x" * 10, quotes=["q"]),
        Recommendation(theme="D", priority="high", action="x" * 10, quotes=["q"]),
    ]
    assert [i.theme for i in sort_recommendations(items)] == ["B", "D", "C", "A"]


async def _add(run: RunContext, **kwargs):
    tool = next(t for t in build_tools() if t.name == "add_recommendation")
    args = AddRecommendationArgs(reason="test call", **kwargs)
    return await tool.handler(args, run)


async def test_add_recommendation_rejects_quotes_that_no_guest_wrote():
    run = RunContext(run_id="r", agent="a", state={"reviews": [review("1", WIFI)], "recommendations": []})
    with pytest.raises(ValueError, match="do not appear in any review"):
        await _add(run, theme="Wi-Fi", priority="high", action="Upgrade the router.",
                   quotes=["The wifi was perfect"])  # fmt: skip
    assert run.state["recommendations"] == []

    # Diferențele de majuscule, spații și ghilimele nu contează; „...” leagă fragmente.
    result = await _add(run, theme="Wi-Fi", priority="high", action="Upgrade the router.",
                        quotes=['"the WIFI  was unusable ... work"'])  # fmt: skip
    assert result["status"] == "added"


async def test_add_recommendation_replaces_same_theme_and_caps_the_count():
    texts = [f"problem number {i} here" for i in range(7)]
    run = RunContext(
        run_id="r", agent="a",
        state={"reviews": [review(str(i), t) for i, t in enumerate(texts)], "recommendations": []},
    )  # fmt: skip
    await _add(run, theme="Wi-Fi", priority="low", action="First version.", quotes=[texts[0]])
    updated = await _add(run, theme="wifi", priority="high", action="Second version.", quotes=[texts[0]])
    assert updated["status"] == "updated" and len(run.state["recommendations"]) == 1
    assert run.state["recommendations"][0].priority == "high"

    for i in range(1, 5):
        await _add(run, theme=f"Theme {i}", priority="medium", action="Some action.", quotes=[texts[i]])
    with pytest.raises(ValueError, match="at most 5"):
        await _add(run, theme="One too many", priority="low", action="Some action.", quotes=[texts[5]])


def test_quote_matching_and_theme_keys():
    reviews = [review("1", "Check-in was confusing: it took us 20 minutes.")]
    assert quote_found("check-in was confusing", reviews)
    assert quote_found("Check-in was confusing… 20 minutes", reviews)
    assert not quote_found("check-in was easy", reviews)
    assert not quote_found("   ", reviews)
    assert theme_key("Wi-Fi") == theme_key("wifi ") == theme_key("WiFi")


class ScriptedLLM:
    """Model care dă recomandările în ordinea low, high: salvarea trebuie să le sorteze."""

    name = "scripted"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        done = [c.name for m in request.messages if m.role == "assistant" for c in m.tool_calls]
        if "add_recommendation" not in done:
            calls = [
                ToolCall.make("add_recommendation", theme="Noise", priority="low",
                              action="Add earplugs to the room.", quotes=[NOISE], reason="noise"),
                ToolCall.make("add_recommendation", theme="Wi-Fi", priority="high",
                              action="Upgrade the internet plan.", quotes=[WIFI], reason="wifi"),
            ]  # fmt: skip
        else:
            calls = [ToolCall.make("finish", result="Two problems.", evidence=[], reason="done")]
        return ChatResponse(
            message=ChatMessage(role="assistant", tool_calls=calls), usage=Usage(), model="scripted"
        )


def test_api_returns_items_sorted_by_priority_whatever_order_the_model_used(settings):
    with app_client(settings, reviews=FakeReviews(WIFI, NOISE), llm=ScriptedLLM()) as client:
        analyse(client, "Old Town Studio")
        (insight,) = client.get("/f4/insights").json()
        assert [i["theme"] for i in insight["items"]] == ["Wi-Fi", "Noise"]
        assert insight["recommendations"].splitlines()[0].startswith("1. [High] Wi-Fi")


# --- 2. Comparație cu analiza anterioară -----------------------------------------


def test_notification_compares_themes_with_the_previous_analysis(settings):
    source = FakeReviews(WIFI, NOISE)
    with app_client(settings, reviews=source) as client:
        analyse(client, "Old Town Studio")
        source.texts = [WIFI, HEATING]  # zgomotul a dispărut, a apărut încălzirea
        analyse(client, "Old Town Studio")

        latest, first = client.get("/f4/insights").json()
        assert latest["changes"]["previous_insight_id"] == first["id"]
        assert latest["changes"]["new"] == ["Heating and hot water"]
        assert latest["changes"]["persisting"] == ["Wi-Fi"]
        assert latest["changes"]["resolved"] == ["Noise"]

        body = client.get("/notifications").json()[0]["body"]
        assert "new: Heating and hot water" in body
        assert "still present: Wi-Fi" in body
        assert "no longer mentioned: Noise" in body


def test_previous_themes_are_given_to_the_agent_so_names_stay_stable(settings):
    source = FakeReviews(WIFI)
    with app_client(settings, reviews=source) as client:
        analyse(client, "Old Town Studio")
        source.texts = [WIFI, NOISE]
        analyse(client, "Old Town Studio")
        latest = client.get("/f4/insights").json()[0]
        run = client.get(f"/agent/runs/{latest['agent_run_id']}").json()
        assert "Themes of the previous analysis: Wi-Fi." in run["objective"]


def test_compare_themes_ignores_spelling_differences():
    def recs(*themes: str) -> list[Recommendation]:
        return [Recommendation(theme=t, priority="low", action="something", quotes=["q"]) for t in themes]

    changes = compare_themes(recs("Wi-Fi", "Noise"), recs("wifi", "Beds"))
    assert (changes.new, changes.persisting, changes.resolved) == (["Beds"], ["wifi"], ["Noise"])


# --- 3. Snapshot-uri fără dubluri ----------------------------------------------


def test_second_analysis_on_the_same_reviews_saves_no_snapshot_and_says_nothing_changed(client):
    analyse(client, "Old Town Studio")
    analyse(client, "Old Town Studio")

    studio = property_by_name(client, "Old Town Studio")
    assert len(client.get("/f4/snapshots", params={"property_id": studio["id"]}).json()) == 1
    assert len(client.get("/f4/insights").json()) == 1  # agentul nu a mai rulat

    latest, first = client.get("/notifications").json()
    assert latest["title"] == "No new reviews for Old Town Studio"
    assert "nothing changed" in latest["body"] and "no new snapshot was saved" in latest["body"]
    assert "1. [High] Wi-Fi" in latest["body"]  # recomandările rămân valabile
    assert first["title"] == "Review recommendations for Old Town Studio"


def test_changed_reviews_save_a_new_snapshot(settings):
    source = FakeReviews(WIFI)
    with app_client(settings, reviews=source) as client:
        analyse(client, "Old Town Studio")
        source.texts = [WIFI, NOISE]
        analyse(client, "Old Town Studio")
        snapshots = client.get("/f4/snapshots").json()
        assert [s["review_count"] for s in snapshots] == [2, 1]
        assert len(client.get("/f4/insights").json()) == 2


def test_unchanged_reviews_without_a_finished_analysis_are_analysed_again(settings):
    source = FakeReviews(WIFI)

    class FailingOnceLLM(ScriptedLLM):
        calls = 0

        async def chat(self, request):
            FailingOnceLLM.calls += 1
            if FailingOnceLLM.calls == 1:
                raise RuntimeError("model down")
            return ChatResponse(
                message=ChatMessage(role="assistant", tool_calls=[
                    ToolCall.make("finish", result="Nothing to fix.", evidence=[], reason="done")]),
                usage=Usage(), model="scripted",
            )  # fmt: skip

    with app_client(settings, reviews=source, llm=FailingOnceLLM()) as client:
        analyse(client, "Old Town Studio")  # agentul cade: snapshot salvat, nicio analiză
        assert client.get("/f4/insights").json() == []
        analyse(client, "Old Town Studio")  # aceleași recenzii, dar fără analiză -> rulează
        assert len(client.get("/f4/snapshots").json()) == 1
        (insight,) = client.get("/f4/insights").json()
        assert insight["items"] == [] and insight["recommendations"] == "Nothing to fix."


def test_source_failure_still_uses_the_last_snapshot_and_tells_the_host(settings):
    source = FakeReviews(WIFI)
    with app_client(settings, reviews=source) as client:
        analyse(client, "Old Town Studio")
        studio = property_by_name(client, "Old Town Studio")
        source.failing.add(studio["id"])
        analyse(client, "Old Town Studio")

        latest, first = client.get("/f4/insights").json()
        assert latest["used_fallback"] is True and latest["snapshot_id"] == first["snapshot_id"]
        assert len(client.get("/f4/snapshots").json()) == 1
        notification = client.get("/notifications").json()[0]
        assert notification["level"] == "warning"
        assert "Live reviews were unavailable" in notification["body"]


# --- 4. Toate proprietățile, câte un eveniment per proprietate ------------------


def test_all_properties_fan_out_into_one_event_and_handler_each(client):
    analyse(client)

    per_property = events_of(client, "PropertyReviewAnalysisRequested")
    assert len(per_property) == 4
    assert len({e["payload"]["property_id"] for e in per_property}) == 4
    (request,) = events_of(client, "ReviewAnalysisRequested")
    assert all(e["correlation_id"] == request["correlation_id"] for e in per_property)
    assert all(e["handlers"][0]["status"] == "ok" for e in per_property)
    assert len(client.get("/f4/insights").json()) == 4


def test_one_failing_property_does_not_stop_the_others(settings):
    source = FakeReviews(WIFI)
    with app_client(settings, reviews=source) as client:
        studio = property_by_name(client, "Old Town Studio")
        source.failing.add(studio["id"])
        analyse(client)

        insights = client.get("/f4/insights").json()
        assert len(insights) == 3 and studio["id"] not in {i["property_id"] for i in insights}
        skipped = [n for n in client.get("/notifications").json() if "skipped" in n["title"]]
        assert [n["property_id"] for n in skipped] == [studio["id"]]


def test_each_property_handler_has_its_own_timeout(settings, monkeypatch):
    monkeypatch.setenv("F4_PROPERTY_TIMEOUT_S", "0.3")
    monkeypatch.setenv("F4_FETCH_TIMEOUT_S", "30")

    class SlowForOne(FakeReviews):
        async def fetch_reviews(self, property_id):
            if property_id == self.slow:
                await asyncio.sleep(5)
            return await super().fetch_reviews(property_id)

    source = SlowForOne(WIFI)
    with app_client(settings, reviews=source) as client:
        source.slow = property_by_name(client, "Old Town Studio")["id"]
        analyse(client)

        per_property = events_of(client, "PropertyReviewAnalysisRequested")
        statuses = sorted(e["handlers"][0]["status"] for e in per_property)
        assert statuses.count("ok") == 3 and len(statuses) == 4  # doar cea lentă a expirat
        assert len(client.get("/f4/insights").json()) == 3
