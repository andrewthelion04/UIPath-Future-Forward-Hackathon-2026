"""Subiectele deschise cu oaspetele (`ctx.guest_topics`) și linkul de anunț al proprietății.

Un feature publică aici ce are voie să afle oaspetele despre ce se întâmplă cu sejurul lui;
cine vorbește cu oaspetele citește de aici, fără să atingă tabelele altui feature.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.contracts.events import GuestOfferAnswered
from app.core.contracts.models import GuestOffer
from app.core.db import Database
from conftest import property_by_name


def publish(container, topic_id="f:incident:1", booking_id="bk_1", **overrides):
    values = {"booking_id": booking_id, "feature": "f", "kind": "incident", "title": "Problem",
              "facts": {"status": "being assessed"}}  # fmt: skip
    return container.guest_topics.publish(topic_id, **(values | overrides))


async def test_publish_replaces_the_current_state_of_a_topic(container):
    first = publish(container)
    second = publish(container, facts={"status": "repair scheduled", "repair_estimate": "about 2 hours"})

    assert first.id == second.id and second.created_at == first.created_at
    (topic,) = container.guest_topics.for_booking("bk_1")
    assert topic.facts == {"status": "repair scheduled", "repair_estimate": "about 2 hours"}
    assert topic.offer is None and topic.status == "open"
    assert container.guest_topics.for_booking("bk_other") == []


async def test_an_offer_takes_exactly_one_answer(container):
    offer = GuestOffer(question="Move to B?", details={"name": "B", "listing_url": "https://x/1"})
    publish(container, offer=offer)

    assert container.guest_topics.answer_offer("f:incident:1", "accept") is True
    assert container.guest_topics.answer_offer("f:incident:1", "decline") is False  # deja răspuns
    (topic,) = container.guest_topics.for_booking("bk_1")
    assert topic.offer.status == "accepted" and topic.offer.details["listing_url"] == "https://x/1"
    assert container.guest_topics.answer_offer("missing", "accept") is False


async def test_closing_a_topic_hides_it_and_withdraws_an_unanswered_offer(container):
    publish(container, offer=GuestOffer(question="Move to B?"))

    assert container.guest_topics.close("f:incident:1") is True
    assert container.guest_topics.close("f:incident:1") is False
    assert container.guest_topics.for_booking("bk_1") == []
    closed = container.guest_topics.get("f:incident:1")
    assert closed.status == "closed" and closed.offer.status == "withdrawn"
    assert container.guest_topics.answer_offer("f:incident:1", "accept") is False

    reopened = publish(container)  # feature-ul poate redeschide subiectul cu o stare nouă
    assert reopened.status == "open" and reopened.offer is None


async def test_a_topic_is_shown_only_while_it_is_valid(container):
    today = container.clock.today()
    publish(container, "f:incident:past", valid_until=today - timedelta(days=1))
    publish(container, "f:incident:today", valid_until=today)
    publish(container, "f:incident:open")

    assert [t.id for t in container.guest_topics.for_booking("bk_1")] == ["f:incident:today", "f:incident:open"]


async def test_an_offer_answer_reaches_only_the_feature_that_made_the_offer(container):
    received: dict[str, list[str]] = {"owner": [], "other": []}

    def handler(name):
        async def on_answer(event: GuestOfferAnswered) -> None:
            received[name].append(event.choice)
        return on_answer

    for name in ("owner", "other"):
        ctx = container._make_feature_context(name)  # noqa: SLF001
        ctx.on_guest_offer_answered(handler(name))
        ctx.commit()

    await container.bus.publish(
        GuestOfferAnswered(topic_id="owner:incident:1", booking_id="bk_1", feature="owner", choice="accept")
    )
    await container.bus.join(timeout=5)
    assert received == {"owner": ["accept"], "other": []}


# --- Linkul de anunț ----------------------------------------------------------------------


def test_every_demo_property_has_a_distinct_listing_link(client):
    properties = client.get("/properties").json()
    links = [p["listing_url"] for p in properties]

    assert len(properties) == 6 and {p["city"] for p in properties} == {"Bucharest"}
    assert all(link and link.startswith("https://www.airbnb.com/rooms/") for link in links)
    assert len(set(links)) == 6
    assert property_by_name(client, "Herastrau Family Flat")["listing_url"].endswith("/1063624740877318380")


def test_an_older_database_gets_the_new_column_without_a_reseed(tmp_path):
    db = Database(tmp_path / "old.db")
    db.script("CREATE TABLE properties (id TEXT PRIMARY KEY, name TEXT NOT NULL);")
    db.execute("INSERT INTO properties (id, name) VALUES ('p1', 'Old')")

    assert db.ensure_column("properties", "listing_url", "TEXT") is True
    assert db.ensure_column("properties", "listing_url", "TEXT") is False  # idempotent
    assert db.ensure_column("no_such_table", "x", "TEXT") is False
    assert db.query_one("SELECT listing_url FROM properties WHERE id = 'p1'") == {"listing_url": None}
