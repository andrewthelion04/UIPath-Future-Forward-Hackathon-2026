"""Portul pentru recenzii. Primește ID-ul intern al proprietății; adaptorul îl traduce."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.contracts.models import ReviewBatch


class ReviewsError(RuntimeError):
    pass


@runtime_checkable
class ReviewsPort(Protocol):
    name: str

    async def fetch_reviews(self, property_id: str) -> ReviewBatch: ...
