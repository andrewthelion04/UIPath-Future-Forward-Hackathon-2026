"""Adaptorul Inside Airbnb, verificat pe un extras real și fixture-uri fără rețea."""

from __future__ import annotations

import csv
import gzip
import io
from datetime import date
from pathlib import Path

import pytest

from app.adapters.inside_airbnb.market import (
    InsideAirbnbMarketAdapter, convert_usd_price, normalize_amenities, parse_price,
)
from app.core.contracts.models import ComparableQuery
from app.core.db import Database
from app.ports.market import MarketDataError
from conftest import app_client, make_settings

FIXTURE = Path(__file__).resolve().parents[3] / "adapters/inside_airbnb/fixtures/athens_2026-09-22.sample.csv"
REAL_SAMPLE_REFS = {"361393", "401423", "484925", "469140", "60394", "536197", "33945", "691025"}
SYNTHETIC_SAMPLE_REFS = {"712390", "824102", "953140", "1045211"}


@pytest.mark.parametrize("raw, expected", [
    ('["Wifi", "Kitchen", "Central air conditioning", "Free washer – In unit", "Lockbox"]',
     {"wifi", "kitchen", "air_conditioning", "washer", "self_check_in"}),
    ('["Fast wifi – 99 Mbps", "Private patio or balcony", "Paid parking garage off premises"]',
     {"wifi", "balcony", "parking"}),
    ('["Keypad", "Smart lock", "Indoor fireplace: wood-burning", "Central heating"]',
     {"self_check_in", "fireplace", "heating"}),
    ('["AC - split type ductless system", "Window AC unit"]', {"air_conditioning"}),
    ('{"Wifi","Kitchen","Self check-in"}', {"wifi", "kitchen", "self_check_in"}),
    ('null', set()), ('"Wifi"', set()), ('{}', set()), ('', set()),
])
def test_real_amenity_variants(raw, expected):
    assert set(normalize_amenities(raw)) == expected


def test_amenity_mapping_does_not_invent_facilities():
    amenities = normalize_amenities('["Dishwasher", "Fireplace guards", "Lock on bedroom door", "No wifi", "AC power adapter"]')
    assert not {"washer", "fireplace", "self_check_in", "wifi"} & set(amenities)
    assert "air_conditioning" not in amenities


@pytest.mark.parametrize("raw", ["", "-12.00", "$-12.00", "$0.00", "NaN", "Infinity", "EUR 1.234,56", "12oops34"])
def test_invalid_prices_are_skipped(raw):
    assert parse_price(raw) is None


@pytest.mark.parametrize("compressed", [False, True])
async def test_real_sample_imports_in_both_formats(tmp_path, compressed):
    payload = FIXTURE.read_bytes()
    source = tmp_path / ("listings.csv.gz" if compressed else "listings.csv")
    source.write_bytes(gzip.compress(payload, mtime=0) if compressed else payload)
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
        adapter.init_schema()
        expected_refs = {
            row["id"] for row in csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        }
        actual_refs = {row["ref"] for row in db.query("SELECT ref FROM market_listings")}
        assert actual_refs == expected_refs
        assert expected_refs == REAL_SAMPLE_REFS | SYNTHETIC_SAMPLE_REFS
        washer = db.query_one("SELECT amenities FROM market_listings WHERE ref = '60394'")["amenities"]
        assert '"washer"' in washer
        assert '"air_conditioning"' in washer
        window_ac = db.query_one("SELECT amenities FROM market_listings WHERE ref = '33945'")["amenities"]
        assert '"air_conditioning"' in window_ac
        dishwasher_only = db.query_one("SELECT amenities FROM market_listings WHERE ref = '536197'")["amenities"]
        assert '"washer"' not in dishwasher_only
        assert "CC BY 4.0" in adapter.attribution
    finally:
        db.close()


def test_athens_prices_use_local_currency_without_inferred_dollar_conversion():
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(
            db=db, csv_path=FIXTURE, city="Athens", currency="EUR", usd_to_currency_rate=0.92,
        )
        adapter.init_schema()
        assert db.query_one("SELECT nightly_price FROM market_listings WHERE ref = '401423'")["nightly_price"] == 130.50
    finally:
        db.close()


def test_mapping_upgrade_reimports_an_unchanged_source(tmp_path, monkeypatch):
    from app.adapters.inside_airbnb import market

    source = tmp_path / "listings.csv"
    source.write_text(
        'id,name,bedrooms,accommodates,amenities,price\n'
        'one,Old mapping,1,2,"[""AC - split type ductless system""]",$50.00\n',
        encoding="utf-8",
    )
    db = Database(":memory:")
    try:
        with monkeypatch.context() as older:
            older.setattr(market, "NORMALIZATION_VERSION", 1)
            older.setitem(market.AMENITY_PATTERNS, "air_conditioning", r"^air conditioning$")
            adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
            adapter.init_schema()
            assert '"air_conditioning"' not in db.query_one("SELECT amenities FROM market_listings")["amenities"]
        adapter.init_schema()
        assert '"air_conditioning"' in db.query_one("SELECT amenities FROM market_listings")["amenities"]
    finally:
        db.close()


@pytest.mark.parametrize("price, rate", [
    (0, 1), (-1, 1), (float("nan"), 1), (float("inf"), 1),
    (1, 0), (1, -1), (1, float("inf")), (1, float("nan")), (1e100, 1), (0.01, 0.01),
])
def test_conversion_rejects_invalid_or_unrepresentable_amounts(price, rate):
    with pytest.raises(ValueError):
        convert_usd_price(price, rate)


def test_out_of_range_converted_row_does_not_discard_valid_listings(tmp_path):
    source = tmp_path / "listings.csv"
    source.write_text(
        'id,name,bedrooms,accommodates,amenities,price\n'
        'valid,Normal,1,2,[],"$50.00"\n'
        'invalid,Outlier,1,2,[],"$' + '9' * 100 + '.00"\n', encoding="utf-8",
    )
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
        adapter.init_schema()
        assert [row["ref"] for row in db.query("SELECT ref FROM market_listings")] == ["valid"]
    finally:
        db.close()


def test_startup_reimports_changed_csv_and_conversion_settings(tmp_path):
    source = tmp_path / "listings.csv"
    db = Database(":memory:")

    def write_rows(rows):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "name", "bedrooms", "accommodates", "amenities", "price"])
        writer.writerows(rows)
        source.write_text(buffer.getvalue(), encoding="utf-8")

    try:
        write_rows([["one", "First listing", 1, 2, '["Wifi"]', "USD 50.00"]])
        first = InsideAirbnbMarketAdapter(
            db=db, csv_path=source, city="Athens", usd_to_currency_rate=0.92
        )
        first.init_schema()
        assert db.query_one("SELECT COUNT(*) AS n FROM market_listings")["n"] == 1
        assert db.query_one("SELECT nightly_price FROM market_listings WHERE ref = 'one'")["nightly_price"] == 46

        write_rows([
            ["one", "First listing", 1, 2, '["Wifi"]', "USD 50.00"],
            ["two", "Added listing", 1, 2, '["Wifi"]', "USD 25.00"],
        ])
        updated = InsideAirbnbMarketAdapter(
            db=db, csv_path=source, city="Athens", usd_to_currency_rate=0.92
        )
        updated.init_schema()
        assert db.query_one("SELECT COUNT(*) AS n FROM market_listings")["n"] == 2

        changed_rate = InsideAirbnbMarketAdapter(
            db=db, csv_path=source, city="Athens", usd_to_currency_rate=0.8
        )
        changed_rate.init_schema()
        prices = db.query("SELECT ref, nightly_price FROM market_listings ORDER BY ref")
        assert [(row["ref"], row["nightly_price"]) for row in prices] == [
            ("one", 40), ("two", 20),
        ]
    finally:
        db.close()


async def test_fixture_provides_real_comparable_without_reinterpreting_local_prices_as_usd():
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(
            db=db, csv_path=FIXTURE, city="Athens", currency="EUR",
            usd_to_currency_rate=0.92,
        )
        adapter.init_schema()
        query = ComparableQuery(
            city="Bucharest", bedrooms=1, max_guests=2,
            amenities=["wifi", "kitchen", "air_conditioning", "washer", "self_check_in"],
            check_in=date(2026, 6, 3), check_out=date(2026, 6, 6),
        )
        matches = await adapter.find_comparables(query)
        assert [(item.title, item.nightly_price, item.currency, item.city) for item in matches
                if {"wifi", "kitchen", "air_conditioning", "washer", "self_check_in"}
                <= set(item.amenities)] == [
            ("Apartment with Leafy Backyard Steps from the Acropolis", 130.50, "EUR", "Athens")
        ]
    finally:
        db.close()


def test_f5_uses_fixture_local_currency_even_with_legacy_conversion_setting(tmp_path):
    settings = make_settings(
        tmp_path,
        market_provider="inside_airbnb",
        inside_airbnb_listings_csv=FIXTURE,
        market_city="Athens",
        market_currency="EUR",
        market_usd_to_currency_rate=0.92,
    )
    from app.features.f5_relisting.tests.test_relisting import cancel

    with app_client(settings) as client:
        cancel(client, "Emma Johnson")
        (proposal,) = client.get("/f5/proposals").json()
        assert proposal["comparable_title"] == "Apartment with Leafy Backyard Steps from the Acropolis"
        assert proposal["comparable_price"] == 130.50
        assert proposal["currency"] == "EUR"
        assert [item["proposed_price"] for item in proposal["nightly_prices"]] == [70, 70, 80.5]


def test_explicit_currency_metadata_controls_conversion_and_rejects_conflicts(tmp_path):
    source = tmp_path / "listings.csv"
    source.write_text(
        'id,name,bedrooms,accommodates,amenities,price,currency\n'
        'local,Local,1,2,[],$50.00,EUR\n'
        'eurosign,Euro sign,1,2,[],€50.00,\n'
        'usd,Explicit USD,1,2,[],$50.00,USD\n'
        'unknown,Unsupported,1,2,[],GBP 50.00,GBP\n'
        'poundsign,Unsupported pound,1,2,[],£50.00,\n'
        'conflict,Conflicting,1,2,[],USD 50.00,EUR\n', encoding="utf-8",
    )
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(
            db=db, csv_path=source, city="Athens", currency="EUR", usd_to_currency_rate=0.92,
        )
        adapter.init_schema()
        assert [(row["ref"], row["nightly_price"]) for row in db.query(
            "SELECT ref, nightly_price FROM market_listings ORDER BY ref"
        )] == [("eurosign", 50), ("local", 50), ("usd", 46)]
    finally:
        db.close()


def test_corrupt_import_keeps_previous_snapshot(tmp_path):
    source = tmp_path / "listings.csv.gz"
    source.write_bytes(gzip.compress(FIXTURE.read_bytes()))
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
        adapter.init_schema()
        original_count = db.query_one("SELECT COUNT(*) AS n FROM market_listings")["n"]
        source.write_bytes(b"broken gzip")
        with pytest.raises(MarketDataError, match="Cannot read"):
            adapter.import_listings()
        assert db.query_one("SELECT COUNT(*) AS n FROM market_listings")["n"] == original_count
    finally:
        db.close()


def test_summary_csv_is_rejected_clearly(tmp_path):
    source = tmp_path / "listings.csv"
    source.write_text("id,name,price\n1,Room,$50\n", encoding="utf-8")
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
        with pytest.raises(MarketDataError, match="Detailed listings CSV required"):
            adapter.init_schema()
    finally:
        db.close()


async def test_incomparable_cheap_rows_cannot_hide_the_cheapest_match(tmp_path):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "name", "bedrooms", "accommodates", "amenities", "price"])
    for i in range(301):
        writer.writerow([str(i), "Bare room", 1, 2, '["Wifi"]', "$10"])
    writer.writerow(["match", "Comparable", 1, 2, '["Wifi", "Kitchen"]', "$50"])
    writer.writerow(["invalid", "Bad bedroom", "nan", 2, '[]', "$25"])
    source = tmp_path / "listings.csv"
    source.write_text(buffer.getvalue(), encoding="utf-8")
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
        adapter.init_schema()
        query = ComparableQuery(city="Athens", bedrooms=1, max_guests=2, amenities=["wifi", "kitchen"],
                                check_in=date(2026, 6, 3), check_out=date(2026, 6, 6))
        assert [c.ref for c in await adapter.find_comparables(query)] == ["match"]
    finally:
        db.close()


def test_ui_displays_source_and_nightly_schedule_after_provider_switch(settings, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from ui import api_client
    from app.features.f5_relisting.tests.test_relisting import FakeMarket, cancel, comparable

    with app_client(settings, market=FakeMarket([comparable(68)])) as client:
        cancel(client, "Emma Johnson")
        db = client.app.state.container.db
        db.execute("UPDATE f5_relist_proposals SET market_attribution = ?",
                   ("Market data: Inside Airbnb (insideairbnb.com), licensed CC BY 4.0",))

        def request(method, path, *, params=None, json=None):
            return client.request(method, path, params=params, json=json).json()

        monkeypatch.setattr(api_client, "request", request)
        page_path = Path(__file__).resolve().parents[1] / "ui_page.py"
        page = AppTest.from_file(str(page_path), default_timeout=30).run()
        assert not page.exception
        assert any("Inside Airbnb" in c.value and "CC BY 4.0" in c.value for c in page.caption)
        assert len(page.dataframe[0].value) == 3
        assert list(page.dataframe[0].value["Proposed price"]) == [65.96, 65.96, 75.85]
