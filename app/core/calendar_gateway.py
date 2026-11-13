"""Calendarul văzut de feature-uri: rutează după conexiunea PMS a proprietății."""

from __future__ import annotations

from datetime import date

from app.core.contracts.models import CalendarDay
from app.core.world_repo import WorldRepository
from app.ports.pms import CapabilityNotSupported, PmsConnection, PmsError, PmsRegistry


class CalendarGateway:
    """Implementează `CalendarPort` peste conexiunile PMS înregistrate."""

    def __init__(self, connections: PmsRegistry, world: WorldRepository) -> None:
        self._connections = connections
        self._world = world

    def _connection(self, property_id: str, capability: str) -> PmsConnection:
        prop = self._world.get_property(property_id)
        if prop is None:
            raise PmsError(f"unknown property {property_id}")
        connection = self._connections.get(prop.pms_provider)
        if connection is None:
            raise PmsError(f"PMS connection '{prop.pms_provider}' is not enabled")
        if not getattr(connection.capabilities, capability):
            raise CapabilityNotSupported(
                f"PMS connection '{prop.pms_provider}' does not support {capability}"
            )
        return connection

    async def get_days(self, property_id: str, start: date, end: date) -> list[CalendarDay]:
        connection = self._connection(property_id, "read_calendar")
        return await connection.get_days(property_id, start, end)

    async def set_price(self, property_id: str, start: date, end: date, price: float) -> None:
        connection = self._connection(property_id, "write_prices")
        await connection.set_price(property_id, start, end, price)

    async def set_availability(
        self, property_id: str, start: date, end: date, available: bool
    ) -> None:
        connection = self._connection(property_id, "write_availability")
        await connection.set_availability(property_id, start, end, available)
