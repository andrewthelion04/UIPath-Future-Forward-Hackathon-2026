"""Portul pentru calendar și prețuri. Intervalele sunt [start, end) pe nopți."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from app.core.contracts.models import CalendarDay


@runtime_checkable
class CalendarPort(Protocol):
    async def get_days(self, property_id: str, start: date, end: date) -> list[CalendarDay]: ...

    async def set_price(self, property_id: str, start: date, end: date, price: float) -> None: ...

    async def set_availability(
        self, property_id: str, start: date, end: date, available: bool
    ) -> None: ...
