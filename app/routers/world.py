"""Datele hostului: proprietăți, rezervări, conversații, personal, calendar."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from app.container import Container
from app.core.contracts.models import Booking, CalendarDay, Host, Message, Property, StaffMember
from app.ports.pms import CapabilityNotSupported, PmsError


def build_router(c: Container) -> APIRouter:
    router = APIRouter(tags=["world"])

    @router.get("/host", response_model=Host)
    async def get_host() -> Host:
        host = c.world.get_host()
        if host is None:
            raise HTTPException(404, "no host yet; seed the world first")
        return host

    @router.get("/properties", response_model=list[Property])
    async def list_properties() -> list[Property]:
        return c.world.list_properties()

    @router.get("/properties/{property_id}/calendar", response_model=list[CalendarDay])
    async def property_calendar(
        property_id: str, start: date | None = None, days: int = 30
    ) -> list[CalendarDay]:
        if c.world.get_property(property_id) is None:
            raise HTTPException(404, f"unknown property {property_id}")
        first = start or c.clock.today()
        try:
            return await c.calendar.get_days(
                property_id, first, first + timedelta(days=max(1, min(days, 120)))
            )
        except CapabilityNotSupported as exc:
            raise HTTPException(501, str(exc)) from exc
        except PmsError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/bookings", response_model=list[Booking])
    async def list_bookings(
        property_id: str | None = None, status: str | None = None
    ) -> list[Booking]:
        return c.world.list_bookings(property_id=property_id, status=status)

    @router.get("/bookings/{booking_id}", response_model=Booking)
    async def get_booking(booking_id: str) -> Booking:
        booking = c.world.get_booking(booking_id)
        if booking is None:
            raise HTTPException(404, f"unknown booking {booking_id}")
        return booking

    @router.get("/bookings/{booking_id}/messages", response_model=list[Message])
    async def booking_messages(booking_id: str, limit: int = 100) -> list[Message]:
        if c.world.get_booking(booking_id) is None:
            raise HTTPException(404, f"unknown booking {booking_id}")
        return c.conversations.history(booking_id, limit=limit)

    @router.get("/staff", response_model=list[StaffMember])
    async def list_staff() -> list[StaffMember]:
        return c.world.list_staff()

    return router
