"""F5: anulare -> comparabile din adaptor -> preț propus -> aprobare -> calendar actualizat."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from app.core.contracts.events import BookingCancelled
from app.core.contracts.models import Booking, Comparable, ComparableQuery, PmsProvider, Property
from app.features.f5_relisting.relisting import (
    cheapest_comparable,
    floor_price,
    is_comparable,
    propose_price,
)
from app.ports.market import MarketDataError
from app.ports.pms import PmsCapabilities
from conftest import app_client, approve, booking_by_guest, events_of, reject

STUDIO_AMENITIES = ["wifi", "kitchen", "air_conditioning", "washer", "self_check_in"]


def cancel(client, guest_name: str) -> dict:
    booking = booking_by_guest(client, guest_name)
    result = client.post(
        "/sim/cancel-booking", params={"wait": True}, json={"booking_id": booking["id"]}
    ).json()
    assert result["status"] == "cancelled" and result["completed"] is True
    return booking


def calendar(client, booking: dict) -> list[tuple[bool, float]]:
    days = client.get(
        f"/properties/{booking['property_id']}/calendar", params={"start": booking["check_in"], "days": 3}
    ).json()
    return [(d["available"], d["price"]) for d in days]


def test_last_minute_cancellation_proposes_a_price_just_under_the_cheapest_comparable(client):
    emma = cancel(client, "Emma Johnson")  # check-in peste 2 zile

    assert booking_by_guest(client, "Emma Johnson")["status"] == "cancelled"
    assert len(events_of(client, "BookingCancelled")) == 1
    (proposal,) = client.get("/f5/proposals").json()
    assert proposal["status"] == "awaiting_approval"
    # Cel mai ieftin cu ACELEAȘI facilități e 68; anunțul de 45 nu are aer condiționat.
    assert (proposal["comparable_title"], proposal["comparable_price"]) == ("Lipscani Cozy Studio", 68)
    assert proposal["proposed_price"] == 65.96  # 68 - 3%
    assert (proposal["base_price"], proposal["floor_price"]) == (70, 52.5)

    (approval,) = client.get("/approvals").json()
    assert (approval["feature"], approval["kind"]) == ("f5_relisting", "relist_price")
    assert approval["title"] == "Relist Old Town Studio at 65.96–75.85 EUR/night?"
    assert "Lipscani Cozy Studio at 68 EUR/night" in approval["summary"]
    # Până la aprobare, prețul din calendar rămâne cel vechi (miercuri, joi, vineri).
    assert calendar(client, emma) == [(True, 70.0), (True, 70.0), (True, 80.5)]


def test_approved_proposal_updates_the_calendar_through_the_adapter(client):
    emma = cancel(client, "Emma Johnson")
    (approval,) = client.get("/approvals").json()

    assert approve(client, approval["id"])["completed"] is True

    assert calendar(client, emma) == [(True, 65.96), (True, 65.96), (True, 75.85)]
    assert client.get("/f5/proposals").json()[0]["status"] == "applied"
    assert client.get("/notifications").json()[0]["title"] == "Old Town Studio relisted at 65.96–75.85 EUR/night"


def test_rejected_proposal_leaves_the_price_unchanged(client):
    emma = cancel(client, "Emma Johnson")
    (approval,) = client.get("/approvals").json()

    reject(client, approval["id"])

    assert calendar(client, emma) == [(True, 70.0), (True, 70.0), (True, 80.5)]
    assert client.get("/f5/proposals").json()[0]["status"] == "rejected"


def test_cancellation_that_is_not_last_minute_only_informs_the_host(client):
    cancel(client, "Marie Dubois")  # check-in peste 20 de zile; pragul hostului e 7

    assert client.get("/approvals").json() == [] and client.get("/f5/proposals").json() == []
    (notification,) = client.get("/notifications").json()
    assert "not last-minute" in notification["title"] and notification["level"] == "info"


class FakeMarket:
    name = "fake"
    attribution = "Test market data"

    def __init__(self, comparables: list[Comparable] | None = None, mode: str = "ok") -> None:
        self.comparables = comparables or []
        self.mode = mode
        self.queries: list[ComparableQuery] = []

    async def find_comparables(self, query: ComparableQuery) -> list[Comparable]:
        self.queries.append(query)
        if self.mode == "fail":
            raise MarketDataError("dataset not reachable")
        if self.mode == "hang":
            await asyncio.sleep(60)
        return self.comparables


def comparable(price: float, amenities: list[str] = STUDIO_AMENITIES, **kwargs: Any) -> Comparable:
    data = {"ref": f"c-{price}", "title": f"Listing {price}", "city": "Bucharest", "bedrooms": 1,
            "max_guests": 2, "amenities": amenities, "nightly_price": price, "source": "fake"}  # fmt: skip
    return Comparable(**{**data, **kwargs})


def test_host_discount_limit_is_enforced_in_code(settings):
    """Un comparabil foarte ieftin nu poate coborî prețul sub pragul hostului (25% din 70)."""
    with app_client(settings, market=FakeMarket([comparable(40)])) as client:
        emma = cancel(client, "Emma Johnson")

        (proposal,) = client.get("/f5/proposals").json()
        assert proposal["proposed_price"] == 52.5 and "maximum relisting discount" in proposal["note"]
        assert proposal["market_attribution"] == "Test market data"  # sursa se creditează în UI
        (approval,) = client.get("/approvals").json()
        approve(client, approval["id"])
        assert calendar(client, emma) == [(True, 52.5), (True, 52.5), (True, 60.38)]


@pytest.mark.parametrize(
    ("mode", "expected"), [("fail", "dataset not reachable"), ("hang", "timeout after 0.2s")]
)
def test_market_failure_or_timeout_is_an_explicit_fallback(settings, monkeypatch, mode, expected):
    monkeypatch.setenv("F5_MARKET_TIMEOUT_S", "0.2")  # setare proprie a feature-ului, din mediu
    with app_client(settings, market=FakeMarket(mode=mode)) as client:
        emma = cancel(client, "Emma Johnson")

        assert client.get("/approvals").json() == []  # nu propunem un preț inventat
        assert client.get("/f5/proposals").json()[0]["status"] == "failed"
        (notification,) = client.get("/notifications").json()
        assert notification["level"] == "warning" and expected in notification["body"]
        assert "market data unavailable" in notification["title"]
        assert calendar(client, emma) == [(True, 70.0), (True, 70.0), (True, 80.5)]
        handler = events_of(client, "BookingCancelled")[0]["handlers"][0]
        assert handler["status"] == "ok"  # eșecul e tratat în feature, nu propagat


def test_no_listing_with_the_same_amenities_means_no_proposal(settings):
    market = FakeMarket([comparable(45, ["wifi", "kitchen"]), comparable(60, bedrooms=2)])
    with app_client(settings, market=market) as client:
        cancel(client, "Emma Johnson")

        assert client.get("/approvals").json() == []
        assert client.get("/f5/proposals").json()[0]["status"] == "no_comparable"
        assert "no comparable listing" in client.get("/notifications").json()[0]["title"]
        assert market.queries[0].amenities == STUDIO_AMENITIES and market.queries[0].city == "Bucharest"


class ReadOnlyPms:
    """O conexiune reală tipică la început: fără scriere în calendar (stub)."""

    provider = "hospitable"
    capabilities = PmsCapabilities(list_properties=True, list_reservations=True, send_messages=True)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def test_pms_without_calendar_writes_degrades_to_manual_instructions(settings):
    with app_client(settings, market=FakeMarket([comparable(100)]), pms_connections=[ReadOnlyPms()]) as client:
        container = client.app.state.container
        container.world.upsert_property(
            Property(id="prop_real", name="Real Flat", city="Bucharest", bedrooms=1, max_guests=2,
                     amenities=STUDIO_AMENITIES, base_price=110, pms_provider=PmsProvider.HOSPITABLE)
        )  # fmt: skip
        booking = Booking(id="bk_real", property_id="prop_real", guest_name="Real Guest",
                          check_in=date(2026, 6, 3), check_out=date(2026, 6, 5),
                          status="cancelled", pms_provider=PmsProvider.HOSPITABLE)  # fmt: skip
        container.world.upsert_booking(booking)
        client.portal.call(
            container.bus.publish,
            BookingCancelled(booking_id=booking.id, property_id=booking.property_id,
                             check_in=booking.check_in, check_out=booking.check_out,
                             pms_provider=booking.pms_provider, source_channel=booking.source_channel),
        )  # fmt: skip
        client.portal.call(container.bus.join, 10)
        # Fără citirea calendarului nu putem inventa tarifele originale sau cere aprobarea.
        assert client.get("/approvals").json() == []
        assert client.get("/f5/proposals").json()[0]["status"] == "failed"
        notification = client.get("/notifications").json()[0]
        assert notification["level"] == "warning" and "could not be applied" in notification["title"]
        assert "read_calendar" in notification["body"] and "manually" in notification["body"]


def test_pricing_rules():
    studio = Property(id="p", name="Studio", bedrooms=1, max_guests=2, amenities=STUDIO_AMENITIES, base_price=70)

    assert is_comparable(comparable(68), studio)
    assert is_comparable(comparable(74, [*STUDIO_AMENITIES, "balcony"]), studio)  # în plus e în regulă
    assert not is_comparable(comparable(45, ["wifi", "kitchen"]), studio)  # lipsesc facilități
    assert not is_comparable(comparable(60, bedrooms=2), studio)
    assert not is_comparable(comparable(60, max_guests=1), studio)

    candidates = [comparable(74), comparable(45, ["wifi"]), comparable(68), comparable(50, currency="USD")]
    assert cheapest_comparable(candidates, studio).nightly_price == 68
    assert cheapest_comparable([comparable(45, ["wifi"])], studio) is None

    assert floor_price(70, 25) == 52.5
    assert propose_price(68, 3, 52.5) == (65.96, False)
    assert propose_price(40, 3, 52.5) == (52.5, True)  # limitat de regula hostului
