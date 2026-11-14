"""Scriptul care leagă proprietățile demo de anunțurile Airbnb reale (prin API-ul REST)."""

from __future__ import annotations

from app.features.f4_review_analysis.link_listings import CONNECTION, link_all, load_listings
from conftest import property_by_name


def test_listings_file_maps_known_demo_properties_to_clean_room_urls(client):
    listings = load_listings()
    names = {p["name"] for p in client.get("/properties").json()}
    assert listings and set(listings) <= names
    assert all(url.startswith("https://www.airbnb.com/rooms/") and "?" not in url for url in listings.values())
    assert len(set(listings.values())) == len(listings)  # un anunț per proprietate


def test_link_all_creates_mappings_idempotently_and_reports_unlinked(client):
    listings = {"Old Town Studio": "https://www.airbnb.com/rooms/1", "Nowhere": "https://www.airbnb.com/rooms/2"}
    report = link_all(client, listings)
    assert link_all(client, listings) == report  # a doua rulare nu schimbă nimic

    studio = property_by_name(client, "Old Town Studio")
    (mapping,) = client.get("/integrations/mappings", params={"connection": CONNECTION}).json()
    assert (mapping["external_id"], mapping["internal_id"]) == ("https://www.airbnb.com/rooms/1", studio["id"])
    assert any(line.startswith("LINKED   Old Town Studio") for line in report)
    assert any(line.startswith("MISSING  Nowhere") for line in report)
    # Proprietățile care nu apar în fișierul dat rămân nelegate și sunt raportate ca atare.
    unlinked = {line.split(":")[0].removeprefix("UNLINKED ") for line in report if line.startswith("UNLINKED")}
    assert unlinked == {p["name"] for p in client.get("/properties").json()} - {"Old Town Studio"}


def test_a_url_already_linked_elsewhere_is_reported_as_a_conflict(client):
    link_all(client, {"Old Town Studio": "https://www.airbnb.com/rooms/1"})
    report = link_all(client, {"Herastrau Family Flat": "https://www.airbnb.com/rooms/1"})
    assert report[0].startswith("CONFLICT Herastrau Family Flat")
