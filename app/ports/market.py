"""Portul pentru date de piață (anunțuri comparabile)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.contracts.models import Comparable, ComparableQuery


class MarketDataError(RuntimeError):
    pass


@runtime_checkable
class MarketDataPort(Protocol):
    name: str
    attribution: str | None  # creditul sursei, afișat în UI (ex. Inside Airbnb, CC BY 4.0)

    async def find_comparables(self, query: ComparableQuery) -> list[Comparable]:
        """Candidați din piață pentru interogare. Regula de comparabilitate o aplică feature-ul."""
        ...
