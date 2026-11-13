"""Adaptorul real de recenzii: un actor Apify rulat sincron. Prima versiune, NETESTATĂ live.

- `POST /v2/acts/{actor_id}/run-sync-get-dataset-items` cu inputul JSON al actorului;
- ID-ul actorului și forma inputului vin din config, pentru că actorii diferă între ei;
  `{listing_url}` din șablon e înlocuit cu URL-ul anunțului;
- anunțul demo e fictiv, deci proprietatea se mapează la URL-ul unui anunț real comparabil:
  corespondență cu `connection="airbnb"`, `entity_type="listing"` (vezi
  `PUT /integrations/mappings`);
- tokenul merge în antetul `Authorization`, nu în URL, ca să nu ajungă în loguri.

Normalizarea câmpurilor e permisivă: fiecare actor își numește câmpurile altfel.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx

from app.core.clock import utcnow
from app.core.contracts.models import Review, ReviewBatch
from app.core.id_mapper import LISTING, IdMapper
from app.core.ids import stable_id
from app.ports.reviews import ReviewsError

SOURCE = "apify"
LISTING_CONNECTION = "airbnb"
PLACEHOLDER = "{listing_url}"
BASE_URL = "https://api.apify.com"


def render_input(template: Any, listing_url: str) -> Any:
    """Înlocuiește `{listing_url}` oriunde apare în șablonul de input."""
    if isinstance(template, str):
        return template.replace(PLACEHOLDER, listing_url)
    if isinstance(template, list):
        return [render_input(item, listing_url) for item in template]
    if isinstance(template, dict):
        return {key: render_input(value, listing_url) for key, value in template.items()}
    return template


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def normalize_review(item: dict[str, Any], property_id: str) -> Review | None:
    text = _first(item, "text", "comments", "review", "localizedText", "localizedReview", "comment")
    rating = _first(item, "rating", "stars", "score")
    if not isinstance(text, str) or not isinstance(rating, int | float) or not 1 <= rating <= 5:
        return None
    reviewer = item.get("reviewer") if isinstance(item.get("reviewer"), dict) else {}
    author = _first(reviewer, "firstName", "name") or _first(item, "author", "reviewerName") or ""
    key = str(_first(item, "id", "reviewId") or f"{author}|{text[:80]}")
    return Review(
        id=stable_id("rev", SOURCE, key),
        property_id=property_id,
        rating=float(rating),
        text=text,
        author=str(author),
        reviewed_on=_date(_first(item, "createdAt", "date", "reviewDate", "localizedDate")),
        source=SOURCE,
    )


class ApifyReviewsAdapter:
    name = SOURCE

    def __init__(
        self,
        *,
        token: str,
        actor_id: str,
        input_template: str,
        ids: IdMapper,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            self._template = json.loads(input_template)
        except json.JSONDecodeError as exc:
            raise ValueError(f"APIFY_REVIEWS_INPUT_TEMPLATE is not valid JSON: {exc}") from exc
        self._actor_id = actor_id
        self._ids = ids
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout_s + 10),  # peste timeout-ul rulării actorului
            transport=transport,
        )

    async def fetch_reviews(self, property_id: str) -> ReviewBatch:
        listing_url = self._ids.to_external(LISTING_CONNECTION, LISTING, property_id)
        if listing_url is None:
            raise ReviewsError(
                f"property {property_id} has no listing URL mapped "
                f"(connection={LISTING_CONNECTION}, entity_type={LISTING})"
            )
        try:
            response = await self._http.post(
                f"/v2/acts/{self._actor_id}/run-sync-get-dataset-items",
                params={"timeout": int(self._timeout_s)},
                json=render_input(self._template, listing_url),
            )
        except httpx.HTTPError as exc:
            raise ReviewsError(f"Apify actor run failed: {exc}") from exc
        if response.status_code >= 400:
            raise ReviewsError(f"Apify actor run -> HTTP {response.status_code}: {response.text[:300]}")
        items = response.json()
        if not isinstance(items, list):
            raise ReviewsError("Apify returned an unexpected payload (expected a list of items)")
        reviews = [
            review
            for item in items
            if isinstance(item, dict) and (review := normalize_review(item, property_id))
        ]
        return ReviewBatch(
            property_id=property_id, source=SOURCE, fetched_at=utcnow(), reviews=reviews
        )

    async def aclose(self) -> None:
        await self._http.aclose()
