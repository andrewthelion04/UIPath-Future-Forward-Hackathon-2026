"""Sincronizarea proprietăților și rezervărilor dintr-o conexiune PMS.

Citește prin `PropertiesPort` și `ReservationsPort`, face upsert în SQLite și salvează
corespondențele prin `IdMapper`. Sincronizările repetate actualizează aceleași înregistrări.
Când o rezervare cunoscută trece în `cancelled`, publică `BookingCancelled`.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.core.contracts.events import BookingCancelled
from app.core.contracts.models import Booking, BookingStatus, PmsProvider, Property
from app.core.event_bus import EventBus
from app.core.id_mapper import PROPERTY, RESERVATION, IdMapper
from app.core.world_repo import WorldRepository
from app.ports.pms import PmsRegistry

logger = logging.getLogger("staypilot.sync")


class UnknownProvider(LookupError):
    pass


class SyncReport(BaseModel):
    provider: str
    properties_created: int = 0
    properties_updated: int = 0
    bookings_created: int = 0
    bookings_updated: int = 0
    cancellations_detected: int = 0
    skipped: list[str] = Field(default_factory=list)


class SyncService:
    def __init__(
        self, connections: PmsRegistry, world: WorldRepository, ids: IdMapper, bus: EventBus
    ) -> None:
        self._connections = connections
        self._world = world
        self._ids = ids
        self._bus = bus

    async def sync(self, provider: str, *, publish_events: bool = True) -> SyncReport:
        connection = self._connections.get(provider)
        if connection is None:
            raise UnknownProvider(
                f"PMS connection '{provider}' is not enabled "
                f"(enabled: {', '.join(self._connections.providers())})"
            )
        report = SyncReport(provider=provider)
        pms_provider = PmsProvider(provider)

        # 1. Proprietățile, ca rezervările să aibă la ce se lega.
        for item in await connection.list_properties():
            property_id, created = self._ids.get_or_create(
                provider, PROPERTY, item.external_id, prefix="prop"
            )
            self._world.upsert_property(
                Property(
                    id=property_id,
                    name=item.name,
                    city=item.city,
                    address=item.address,
                    bedrooms=item.bedrooms,
                    max_guests=item.max_guests,
                    amenities=item.amenities,
                    base_price=item.base_price,
                    currency=item.currency,
                    pms_provider=pms_provider,
                    listing_url=item.listing_url,
                )
            )
            if created:
                report.properties_created += 1
            else:
                report.properties_updated += 1

        # 2. Rezervările.
        for item in await connection.list_reservations():
            property_id = self._ids.to_internal(provider, PROPERTY, item.property_external_id)
            if property_id is None:
                report.skipped.append(f"reservation {item.external_id}: unknown property")
                continue
            booking_id, created = self._ids.get_or_create(
                provider, RESERVATION, item.external_id, prefix="bk"
            )
            previous = self._world.get_booking(booking_id)
            booking = Booking(
                id=booking_id,
                property_id=property_id,
                guest_name=item.guest_name,
                guest_count=item.guest_count,
                check_in=item.check_in,
                check_out=item.check_out,
                status=item.status,
                pms_provider=pms_provider,
                source_channel=item.source_channel,
                total_price=item.total_price,
                currency=item.currency,
            )
            self._world.upsert_booking(booking)
            if created:
                report.bookings_created += 1
            else:
                report.bookings_updated += 1

            became_cancelled = (
                previous is not None
                and previous.status != BookingStatus.CANCELLED
                and booking.status == BookingStatus.CANCELLED
            )
            if became_cancelled:
                report.cancellations_detected += 1
                if publish_events:
                    await self._bus.publish(
                        BookingCancelled(
                            booking_id=booking.id,
                            property_id=booking.property_id,
                            check_in=booking.check_in,
                            check_out=booking.check_out,
                            pms_provider=booking.pms_provider,
                            source_channel=booking.source_channel,
                            source=f"sync:{provider}",
                        )
                    )
        logger.info("Sync %s: %s", provider, report.model_dump(exclude={"provider"}))
        return report
