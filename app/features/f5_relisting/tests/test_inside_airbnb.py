"""Adaptorul Inside Airbnb, verificat pe un extras real și fixture-uri fără rețea."""

from __future__ import annotations

import csv
import gzip
import io
from datetime import date
from pathlib import Path

import pytest

from app.adapters.inside_airbnb.market import (
    InsideAirbnbMarketAdapter, normalize_amenities, parse_price,
)
from app.core.contracts.models import ComparableQuery
from app.core.db import Database
from app.ports.market import MarketDataError
from conftest import app_client

FIXTURE = Path(__file__).resolve().parents[3] / "adapters/inside_airbnb/fixtures/athens_2026-09-22.sample.csv"


@pytest.mark.parametrize("raw, expected", [
    ('["Wifi", "Kitchen", "Central air conditioning", "Free washer – In unit", "Lockbox"]',
     {"wifi", "kitchen", "air_conditioning", "washer", "self_check_in"}),
    ('["Fast wifi – 99 Mbps", "Private patio or balcony", "Paid parking garage off premises"]',
     {"wifi", "balcony", "parking"}),
    ('["Keypad", "Smart lock", "Indoor fireplace: wood-burning", "Central heating"]',
     {"self_check_in", "fireplace", "heating"}),
    ('{"Wifi","Kitchen","Self check-in"}', {"wifi", "kitchen", "self_check_in"}),
    ('null', set()), ('"Wifi"', set()), ('{}', set()), ('', set()),
])
def test_real_amenity_variants(raw, expected):
    assert set(normalize_amenities(raw)) == expected


def test_amenity_mapping_does_not_invent_facilities():
    amenities = normalize_amenities('["Dishwasher", "Fireplace guards", "Lock on bedroom door", "No wifi"]')
    assert not {"washer", "fireplace", "self_check_in", "wifi"} & set(amenities)


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
        assert db.query_one("SELECT COUNT(*) AS n FROM market_listings")["n"] == 8
        washer = db.query_one("SELECT amenities FROM market_listings WHERE ref = '60394'")["amenities"]
        assert '"washer"' in washer
        dishwasher_only = db.query_one("SELECT amenities FROM market_listings WHERE ref = '536197'")["amenities"]
        assert '"washer"' not in dishwasher_only
        assert "CC BY 4.0" in adapter.attribution
    finally:
        db.close()


def test_corrupt_import_keeps_previous_snapshot(tmp_path):
    source = tmp_path / "listings.csv.gz"
    source.write_bytes(gzip.compress(FIXTURE.read_bytes()))
    db = Database(":memory:")
    try:
        adapter = InsideAirbnbMarketAdapter(db=db, csv_path=source, city="Athens")
        adapter.init_schema()
        source.write_bytes(b"broken gzip")
        with pytest.raises(MarketDataError, match="Cannot read"):
            adapter.import_listings()
        assert db.query_one("SELECT COUNT(*) AS n FROM market_listings")["n"] == 8
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
