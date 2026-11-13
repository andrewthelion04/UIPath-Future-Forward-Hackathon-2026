"""Piața simulată: anunțuri comparabile din seed."""

from __future__ import annotations

from app.core.contracts.models import Comparable, ComparableQuery
from app.adapters.mock.world import MockWorld

SOURCE = "mock"


class MockMarketAdapter:
    name = SOURCE
    attribution: str | None = None

    def __init__(self, world: MockWorld) -> None:
        self._world = world

    async def find_comparables(self, query: ComparableQuery) -> list[Comparable]:
        city = query.city.strip().lower()
        return [
            Comparable(source=SOURCE, **data)
            for data in self._world.comparables()
            if data["city"].strip().lower() == city
        ]
