"""Maparea ID-urilor și sincronizarea idempotentă."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.core.contracts.models import BookingStatus, SourceChannel
from app.core.id_mapper import PROPERTY, RESERVATION, MappingConflict
from app.core.sync_service import UnknownProvider
from app.ports.pms import PmsCapabilities, PmsProperty, PmsReservation


def test_get_or_create_is_stable_and_idempotent(container):
    first, created_first = container.ids.get_or_create("hospitable", PROPERTY, "uuid-1", prefix="prop")
    second, created_second = container.ids.get_or_create("hospitable", PROPERTY, "uuid-1", prefix="prop")

    assert (created_first, created_second) == (True, False)
    assert first == second and first.startswith("prop_")
    assert container.ids.to_internal("hospitable", PROPERTY, "uuid-1") == first
    assert container.ids.to_external("hospitable", PROPERTY, first) == "uuid-1"


def test_the_same_external_id_on_another_connection_is_another_entity(container):
    one, _ = container.ids.get_or_create("hospitable", PROPERTY, "42", prefix="prop")
    two, _ = container.ids.get_or_create("ownerrez", PROPERTY, "42", prefix="prop")
    three, _ = container.ids.get_or_create("ownerrez", RESERVATION, "42", prefix="bk")
    assert len({one, two, three}) == 3


def test_relinking_to_another_internal_id_is_refused(container):
    container.ids.link("airbnb", "listing", "https://airbnb.com/rooms/1", "prop_a")
    container.ids.link("airbnb", "listing", "https://airbnb.com/rooms/1", "prop_a")  # repetare: ok
    with pytest.raises(MappingConflict):
        container.ids.link("airbnb", "listing", "https://airbnb.com/rooms/1", "prop_b")
    assert container.ids.to_internal("airbnb", "listing", "missing") is None


def test_seeded_world_has_a_mapped_mock_airbnb_booking(container):
    """Maparea demo: o rezervare cu pms_provider=mock și source_channel=airbnb."""
    bookings = [
        b
        for b in container.world.list_bookings()
        if b.pms_provider == "mock" and b.source_channel == SourceChannel.AIRBNB
    ]
    assert bookings
    booking = next(b for b in bookings if b.guest_name == "Emma Johnson")
    external_id = container.ids.to_external("mock", RESERVATION, booking.id)
    assert external_id == "MOCK-RES-1001"
    assert container.ids.to_internal("mock", RESERVATION, external_id) == booking.id
    # ID-urile externe nu apar în modelele interne.
    assert "MOCK" not in booking.model_dump_json()
    assert all("MOCK" not in p.model_dump_json() for p in container.world.list_properties())


async def test_repeated_sync_updates_the_same_records(container):
    before_properties = {p.id for p in container.world.list_properties()}
    before_bookings = {b.id for b in container.world.list_bookings()}
    mappings = len(container.ids.list())

    report = await container.sync.sync("mock")

    assert (report.properties_created, report.bookings_created) == (0, 0)
    assert (report.properties_updated, report.bookings_updated) == (4, 7)
    assert {p.id for p in container.world.list_properties()} == before_properties
    assert {b.id for b in container.world.list_bookings()} == before_bookings
    assert len(container.ids.list()) == mappings


async def test_sync_emits_booking_cancelled_once_on_the_status_transition(container):
    booking = next(b for b in container.world.list_bookings() if b.guest_name == "Emma Johnson")
    await container.simulation.cancel_reservation(booking.id)

    first = await container.sync.sync("mock")
    second = await container.sync.sync("mock")
    await container.bus.join(timeout=10)

    assert (first.cancellations_detected, second.cancellations_detected) == (1, 0)
    assert container.world.get_booking(booking.id).status == BookingStatus.CANCELLED
    cancelled = [e for e in container.bus.list_events() if e["type"] == "BookingCancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["payload"]["booking_id"] == booking.id
    assert cancelled[0]["source"] == "sync:mock"


class FakePms:
    """O conexiune PMS minimală, ca să dovedim că sync-ul nu depinde de adaptorul mock."""

    provider = "hospitable"
    capabilities = PmsCapabilities(list_properties=True, list_reservations=True)

    def __init__(self) -> None:
        self.properties = [PmsProperty(external_id="H-P1", name="Real Flat", city="Vienna")]
        self.reservations = [
            PmsReservation(
                external_id="H-R1",
                property_external_id="H-P1",
                guest_name="Real Guest",
                check_in=date(2026, 7, 1),
                check_out=date(2026, 7, 4),
                source_channel=SourceChannel.AIRBNB,
            ),
            PmsReservation(
                external_id="H-R2",
                property_external_id="H-UNKNOWN",
                guest_name="Orphan",
                check_in=date(2026, 7, 1),
                check_out=date(2026, 7, 2),
            ),
        ]

    async def list_properties(self) -> list[PmsProperty]:
        return self.properties

    async def list_reservations(self) -> list[PmsReservation]:
        return self.reservations

    def __getattr__(self, name: str) -> Any:  # restul porturilor nu sunt folosite aici
        raise AttributeError(name)


async def test_sync_normalizes_another_connection_and_skips_orphans(container):
    container.pms.register(FakePms())

    report = await container.sync.sync("hospitable")

    assert (report.properties_created, report.bookings_created) == (1, 1)
    assert report.skipped == ["reservation H-R2: unknown property"]
    property_id = container.ids.to_internal("hospitable", PROPERTY, "H-P1")
    booking = container.world.get_booking(container.ids.to_internal("hospitable", RESERVATION, "H-R1"))
    assert booking.property_id == property_id
    assert (booking.pms_provider, booking.source_channel) == ("hospitable", "airbnb")
    assert len(container.world.list_properties()) == 5  # cele 4 din lumea mock rămân neatinse


async def test_sync_of_a_disabled_connection_is_refused(container):
    with pytest.raises(UnknownProvider):
        await container.sync.sync("ownerrez")
