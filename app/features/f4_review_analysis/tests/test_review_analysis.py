"""F4: cerere de analiză -> recenzii din adaptor -> recomandări trimise hostului."""

from __future__ import annotations

import asyncio
from datetime import date

from app.core.clock import utcnow
from app.core.contracts.models import Review, ReviewBatch
from app.features.f4_review_analysis.analysis import rating_stats
from app.ports.reviews import ReviewsError
from conftest import app_client, booking_by_guest, drain, property_by_name


def analyse(client, property_name: str | None = None) -> dict | None:
    prop = property_by_name(client, property_name) if property_name else None
    result = client.post(
        "/sim/review-analysis", params={"wait": True}, json={"property_id": prop["id"] if prop else None}
    ).json()
    assert result["status"] == "published" and result["completed"] is True
    return prop


def test_reviews_become_recommendations_for_the_host(client):
    studio = analyse(client, "Old Town Studio")

    (insight,) = client.get("/f4/insights").json()
    assert insight["property_id"] == studio["id"] and insight["used_fallback"] is False
    assert (insight["review_count"], insight["average_rating"]) == (5, 3.8)  # calculate din date
    assert "Wi-Fi" in insight["recommendations"] and "Check-in" in insight["recommendations"]
    assert any("wifi kept dropping" in quote for quote in insight["evidence"])

    (notification,) = client.get("/notifications").json()
    assert notification["title"] == "Review recommendations for Old Town Studio"
    assert insight["recommendations"] in notification["body"]
    assert "Based on 5 reviews, average 3.8 (mock snapshot" in notification["body"]

    run = client.get(f"/agent/runs/{insight['agent_run_id']}").json()
    tools = [s["tool_name"] for s in run["steps_detail"] if s["kind"] in ("tool", "finish")]
    # Câte o recomandare per temă (Wi-Fi, Check-in, Noise), apoi finish.
    assert tools == ["get_rating_stats", "get_reviews", *["add_recommendation"] * 3, "finish"]
    assert client.get("/approvals").json() == []  # recomandările sunt informative


def test_request_without_a_property_analyses_all_of_them(client):
    analyse(client)

    insights = client.get("/f4/insights").json()
    assert len(insights) == 6 and len({i["property_id"] for i in insights}) == 6
    studio = property_by_name(client, "Floreasca Garden Studio")
    (studio_insight,) = client.get("/f4/insights", params={"property_id": studio["id"]}).json()
    assert "Heating and hot water" in studio_insight["recommendations"]


class FakeReviews:
    """Sursă de recenzii controlată de test: merge, cade sau se blochează."""

    name = "fake"

    def __init__(self) -> None:
        self.mode = "ok"

    async def fetch_reviews(self, property_id: str) -> ReviewBatch:
        if self.mode == "fail":
            raise ReviewsError("actor run failed")
        if self.mode == "hang":
            await asyncio.sleep(60)
        reviews = [
            Review(id="r1", property_id=property_id, rating=2, source=self.name,
                   text="The wifi was unusable.", reviewed_on=date(2026, 5, 20)),
            Review(id="r2", property_id=property_id, rating=5, source=self.name,
                   text="Lovely stay.", reviewed_on=date(2026, 5, 25)),
        ]  # fmt: skip
        return ReviewBatch(property_id=property_id, source=self.name, fetched_at=utcnow(), reviews=reviews)


def test_adapter_failure_falls_back_to_the_last_snapshot(settings):
    source = FakeReviews()
    with app_client(settings, reviews=source) as client:
        analyse(client, "Old Town Studio")  # prima rulare salvează un snapshot
        source.mode = "fail"
        analyse(client, "Old Town Studio")

        latest, first = client.get("/f4/insights").json()
        assert (first["used_fallback"], latest["used_fallback"]) == (False, True)
        assert latest["snapshot_id"] == first["snapshot_id"]
        notification = client.get("/notifications").json()[0]
        assert notification["level"] == "warning"
        assert "Live reviews were unavailable" in notification["body"]
        assert "actor run failed" in notification["body"] and "fake snapshot" in notification["body"]


def test_adapter_failure_without_a_snapshot_is_reported_not_hidden(settings):
    source = FakeReviews()
    source.mode = "fail"
    with app_client(settings, reviews=source) as client:
        analyse(client, "Old Town Studio")

        assert client.get("/f4/insights").json() == []
        (notification,) = client.get("/notifications").json()
        assert notification["level"] == "warning"
        assert notification["title"] == "Review analysis skipped for Old Town Studio"
        handlers = client.get("/events").json()[0]["handlers"]
        assert handlers[0]["status"] == "ok"  # eșecul adaptorului e tratat, nu propagat


def test_hanging_adapter_times_out_and_leaves_the_other_features_untouched(settings, monkeypatch):
    monkeypatch.setenv("F4_FETCH_TIMEOUT_S", "0.2")  # setare proprie a feature-ului, din mediu
    source = FakeReviews()
    source.mode = "hang"
    with app_client(settings, reviews=source) as client:
        studio = property_by_name(client, "Old Town Studio")
        emma = booking_by_guest(client, "Emma Johnson")
        client.post("/sim/review-analysis", json={"property_id": studio["id"]})  # fără wait: F4 e blocat

        # În timp ce F4 așteaptă adaptorul, F1 și F3 lucrează normal.
        client.post("/sim/guest-message", json={"booking_id": emma["id"], "text": "What time is check-in?"})
        assert drain(client)

        senders = [m["sender"] for m in client.get(f"/bookings/{emma['id']}/messages").json()]
        assert senders == ["guest", "copilot"]
        skipped = [n for n in client.get("/notifications").json() if "skipped" in n["title"]]
        assert len(skipped) == 1 and "timeout after 0.2s" in skipped[0]["body"]


def test_rating_stats_come_from_the_data():
    reviews = [
        Review(id="1", property_id="p", rating=5, text="a", source="t", reviewed_on=date(2026, 1, 1)),
        Review(id="2", property_id="p", rating=4, text="b", source="t", reviewed_on=date(2026, 3, 1)),
        Review(id="3", property_id="p", rating=2, text="c", source="t"),
    ]
    stats = rating_stats(reviews)
    assert (stats["count"], stats["average"]) == (3, 3.67)
    assert stats["distribution"] == {"5": 1, "4": 1, "3": 0, "2": 1, "1": 0}
    assert (stats["oldest"], stats["newest"]) == ("2026-01-01", "2026-03-01")
    assert rating_stats([])["average"] is None
