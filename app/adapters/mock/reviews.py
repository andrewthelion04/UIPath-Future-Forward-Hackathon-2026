"""Sursa de recenzii simulată."""

from __future__ import annotations

from app.core.clock import utcnow
from app.core.contracts.models import Review, ReviewBatch
from app.core.id_mapper import PROPERTY, IdMapper
from app.core.ids import stable_id
from app.adapters.mock.world import MockWorld
from app.ports.reviews import ReviewsError

SOURCE = "mock"


class MockReviewsAdapter:
    name = SOURCE

    def __init__(self, world: MockWorld, ids: IdMapper) -> None:
        self._world = world
        self._ids = ids

    async def fetch_reviews(self, property_id: str) -> ReviewBatch:
        external_id = self._ids.to_external("mock", PROPERTY, property_id)
        if external_id is None:
            raise ReviewsError(f"property {property_id} has no listing in the mock review source")
        reviews = [
            Review(
                id=stable_id("rev", SOURCE, data["external_id"]),
                property_id=property_id,
                rating=data["rating"],
                text=data["text"],
                author=data.get("author", ""),
                reviewed_on=data.get("reviewed_on"),
                source=SOURCE,
            )
            for data in self._world.reviews(external_id)
        ]
        return ReviewBatch(
            property_id=property_id, source=SOURCE, fetched_at=utcnow(), reviews=reviews
        )
