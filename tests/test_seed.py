"""Seed-ul lumii simulate: se validează la încărcare și e coerent relațional."""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from app.config import ROOT_DIR
from app.core.contracts.models import BookingStatus
from app.seeding import SeedError, load_seed, parse_seed
from conftest import TODAY

SEED_FILE = ROOT_DIR / "seed" / "world.json"


@pytest.fixture
def seed_data() -> dict:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def test_shipped_seed_is_valid():
    seed = load_seed(SEED_FILE)
    assert len(seed.properties) >= 3 and seed.staff and seed.reviews and seed.comparables
    assert seed.host.rules.max_relist_discount_pct > 0


def break_seed(data: dict, mutate) -> dict:
    broken = copy.deepcopy(data)
    mutate(broken)
    return broken


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["reservations"][0].update(property_external_id="NOPE"), "unknown property NOPE"),
        (lambda d: d["reservations"][4].update(check_in_offset_days=3), "overlapping confirmed reservations"),
        (lambda d: d["reservations"][0].update(guest_count=9), "exceed"),
        (lambda d: d["reviews"][0].update(property_external_id="NOPE"), "unknown property NOPE"),
        (lambda d: d["staff"][0]["property_external_ids"].append("NOPE"), "staff staff_maria"),
        (lambda d: d["properties"].append(d["properties"][0]), "duplicate property id"),
        (lambda d: [r.update(source_channel="direct") for r in d["reservations"]], "source_channel=airbnb"),
        (lambda d: d["reviews"][0].update(rating=9), "rating"),
        (lambda d: d.pop("host"), "host"),
    ],
)
def test_incoherent_seed_is_rejected_with_the_reason(seed_data, mutate, expected):
    with pytest.raises(SeedError) as error:
        parse_seed(break_seed(seed_data, mutate))
    assert expected in str(error.value)


def test_unreadable_seed_file(tmp_path):
    with pytest.raises(SeedError):
        load_seed(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SeedError):
        load_seed(bad)


async def test_seeded_world_is_relationally_coherent(container):
    world = container.world
    properties = {p.id: p for p in world.list_properties()}
    bookings = world.list_bookings()

    assert world.get_host() is not None and len(properties) == 6 and len(bookings) == 7
    assert all(b.property_id in properties for b in bookings)
    assert all(pid in properties for member in world.list_staff() for pid in member.property_ids)
    assert all(b.guest_count <= properties[b.property_id].max_guests for b in bookings)

    # Datele sunt relative la „azi”: Emma sosește peste două zile.
    emma = next(b for b in bookings if b.guest_name == "Emma Johnson")
    assert emma.check_in == TODAY + timedelta(days=2)

    # Calendarul reflectă rezervările confirmate.
    for booking in bookings:
        if booking.check_out <= TODAY - timedelta(days=14):
            continue
        days = await container.calendar.get_days(booking.property_id, booking.check_in, booking.check_out)
        expected_free = booking.status == BookingStatus.CANCELLED
        assert days and all(day.available is expected_free for day in days), booking.guest_name

    # Recenzii și comparabile există pentru fiecare proprietate / oraș.
    for prop in properties.values():
        batch = await container.reviews.fetch_reviews(prop.id)
        assert batch.reviews and all(r.property_id == prop.id for r in batch.reviews)


async def test_reset_restores_the_seeded_world(container):
    emma = next(b for b in container.world.list_bookings() if b.guest_name == "Emma Johnson")
    await container.simulation.cancel_reservation(emma.id)
    await container.sync.sync("mock")
    container.notifications.notify(feature="test", title="noise")

    await container.reset_world()

    assert container.world.get_booking(emma.id).status == BookingStatus.CONFIRMED  # același ID intern
    assert container.notifications.list() == []
    assert container.bus.list_events() == []
