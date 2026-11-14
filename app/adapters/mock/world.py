"""Lumea simulată: modelele de seed (validate) și stocarea ei în tabelele `mock_*`.

Lumea se păstrează în SQLite, nu în memorie, ca modificările (un preț schimbat, o rezervare
anulată) să supraviețuiască repornirii API-ului.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.contracts.models import BookingStatus, SourceChannel
from app.core.db import Database, from_json, to_json

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
CALENDAR_PAST_DAYS = 14
CALENDAR_FUTURE_DAYS = 90
WEEKEND_FACTOR = 1.15  # vineri și sâmbătă


# --- Modelele de seed ---------------------------------------------------------------


class SeedProperty(BaseModel):
    external_id: str
    name: str
    city: str
    address: str = ""
    bedrooms: int = Field(ge=0)
    max_guests: int = Field(ge=1)
    amenities: list[str] = Field(default_factory=list)
    base_price: float = Field(gt=0)
    currency: str = "EUR"
    listing_url: str | None = None


class SeedReservation(BaseModel):
    external_id: str
    property_external_id: str
    guest_name: str
    guest_count: int = Field(ge=1)
    check_in_offset_days: int  # față de ziua curentă; negativ = în trecut
    nights: int = Field(ge=1)
    status: BookingStatus = BookingStatus.CONFIRMED
    source_channel: SourceChannel
    total_price: float | None = None

    def dates(self, today: date) -> tuple[date, date]:
        check_in = today + timedelta(days=self.check_in_offset_days)
        return check_in, check_in + timedelta(days=self.nights)


class SeedReview(BaseModel):
    external_id: str
    property_external_id: str
    rating: float = Field(ge=1, le=5)
    text: str
    author: str = ""
    days_ago: int = Field(ge=0)


class SeedComparable(BaseModel):
    ref: str
    title: str
    city: str
    bedrooms: int = Field(ge=0)
    max_guests: int = Field(ge=1)
    amenities: list[str] = Field(default_factory=list)
    nightly_price: float = Field(gt=0)
    currency: str = "EUR"
    url: str | None = None


class MockWorldSeed(BaseModel):
    """Partea de seed care descrie sistemele externe simulate."""

    properties: list[SeedProperty]
    reservations: list[SeedReservation]
    reviews: list[SeedReview] = Field(default_factory=list)
    comparables: list[SeedComparable] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_coherence(self) -> MockWorldSeed:
        problems: list[str] = []
        properties = {p.external_id: p for p in self.properties}

        def duplicates(kind: str, ids: list[str]) -> None:
            seen: set[str] = set()
            for item in ids:
                if item in seen:
                    problems.append(f"duplicate {kind} id: {item}")
                seen.add(item)

        duplicates("property", [p.external_id for p in self.properties])
        duplicates("reservation", [r.external_id for r in self.reservations])
        duplicates("review", [r.external_id for r in self.reviews])
        duplicates("comparable", [c.ref for c in self.comparables])

        by_property: dict[str, list[SeedReservation]] = {}
        for res in self.reservations:
            prop = properties.get(res.property_external_id)
            if prop is None:
                problems.append(
                    f"reservation {res.external_id}: unknown property {res.property_external_id}"
                )
                continue
            if res.guest_count > prop.max_guests:
                problems.append(
                    f"reservation {res.external_id}: {res.guest_count} guests exceed "
                    f"{prop.external_id} capacity ({prop.max_guests})"
                )
            if res.status == BookingStatus.CONFIRMED:
                by_property.setdefault(res.property_external_id, []).append(res)

        for prop_id, items in by_property.items():
            items.sort(key=lambda r: r.check_in_offset_days)
            for first, second in pairwise(items):
                if second.check_in_offset_days < first.check_in_offset_days + first.nights:
                    problems.append(
                        f"overlapping confirmed reservations on {prop_id}: "
                        f"{first.external_id} and {second.external_id}"
                    )

        for review in self.reviews:
            if review.property_external_id not in properties:
                problems.append(
                    f"review {review.external_id}: unknown property {review.property_external_id}"
                )

        if not any(r.source_channel == SourceChannel.AIRBNB for r in self.reservations):
            problems.append("at least one reservation must have source_channel=airbnb")

        if problems:
            raise ValueError("incoherent seed: " + "; ".join(problems))
        return self


# --- Stocarea lumii -----------------------------------------------------------------


class MockWorld:
    def __init__(self, db: Database) -> None:
        self._db = db

    def init_schema(self) -> None:
        self._db.script(SCHEMA_FILE.read_text(encoding="utf-8"))

    def load(self, seed: MockWorldSeed, today: date) -> None:
        """Înlocuiește lumea simulată cu cea din seed, cu date relative la `today`."""
        with self._db.transaction():
            for table in (
                "mock_properties",
                "mock_reservations",
                "mock_calendar_days",
                "mock_messages",
                "mock_reviews",
                "mock_comparables",
            ):
                self._db.execute(f"DELETE FROM {table}")

            for prop in seed.properties:
                self._db.execute(
                    "INSERT INTO mock_properties (external_id, data) VALUES (?, ?)",
                    (prop.external_id, to_json(prop.model_dump())),
                )
                for offset in range(-CALENDAR_PAST_DAYS, CALENDAR_FUTURE_DAYS):
                    day = today + timedelta(days=offset)
                    factor = WEEKEND_FACTOR if day.weekday() in (4, 5) else 1.0
                    self._db.execute(
                        "INSERT INTO mock_calendar_days (property_external_id, day, available, "
                        "price) VALUES (?, ?, 1, ?)",
                        (prop.external_id, day.isoformat(), round(prop.base_price * factor, 2)),
                    )

            for res in seed.reservations:
                check_in, check_out = res.dates(today)
                data = res.model_dump(mode="json", exclude={"check_in_offset_days", "nights"})
                self._db.execute(
                    "INSERT INTO mock_reservations (external_id, property_external_id, check_in, "
                    "check_out, status, data) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        res.external_id,
                        res.property_external_id,
                        check_in.isoformat(),
                        check_out.isoformat(),
                        res.status.value,
                        to_json(data),
                    ),
                )
                if res.status == BookingStatus.CONFIRMED:
                    self.set_availability(res.property_external_id, check_in, check_out, False)

            for review in seed.reviews:
                data = review.model_dump(exclude={"days_ago"})
                data["reviewed_on"] = (today - timedelta(days=review.days_ago)).isoformat()
                self._db.execute(
                    "INSERT INTO mock_reviews (external_id, property_external_id, data) "
                    "VALUES (?, ?, ?)",
                    (review.external_id, review.property_external_id, to_json(data)),
                )

            for comparable in seed.comparables:
                self._db.execute(
                    "INSERT INTO mock_comparables (ref, data) VALUES (?, ?)",
                    (comparable.ref, to_json(comparable.model_dump())),
                )

    # --- Proprietăți și rezervări --------------------------------------------------

    def properties(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT data FROM mock_properties ORDER BY external_id")
        return [from_json(row["data"]) for row in rows]

    def reservations(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM mock_reservations ORDER BY external_id")
        return [self._reservation(row) for row in rows]

    def reservation(self, external_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM mock_reservations WHERE external_id = ?", (external_id,)
        )
        return self._reservation(row) if row else None

    @staticmethod
    def _reservation(row: dict[str, Any]) -> dict[str, Any]:
        data = from_json(row["data"])
        data.update(check_in=row["check_in"], check_out=row["check_out"], status=row["status"])
        return data

    def cancel_reservation(self, external_id: str) -> bool:
        """Anulează rezervarea și eliberează nopțile. False dacă era deja anulată."""
        res = self.reservation(external_id)
        if res is None:
            raise LookupError(f"unknown mock reservation {external_id}")
        if res["status"] == BookingStatus.CANCELLED.value:
            return False
        with self._db.transaction():
            self._db.execute(
                "UPDATE mock_reservations SET status = ? WHERE external_id = ?",
                (BookingStatus.CANCELLED.value, external_id),
            )
            self.set_availability(
                res["property_external_id"],
                date.fromisoformat(res["check_in"]),
                date.fromisoformat(res["check_out"]),
                True,
            )
        return True

    # --- Calendar ------------------------------------------------------------------

    def calendar_days(self, property_external_id: str, start: date, end: date) -> list[dict]:
        return self._db.query(
            "SELECT day, available, price FROM mock_calendar_days "
            "WHERE property_external_id = ? AND day >= ? AND day < ? ORDER BY day",
            (property_external_id, start.isoformat(), end.isoformat()),
        )

    def set_availability(
        self, property_external_id: str, start: date, end: date, available: bool
    ) -> None:
        self._db.execute(
            "UPDATE mock_calendar_days SET available = ? "
            "WHERE property_external_id = ? AND day >= ? AND day < ?",
            (int(available), property_external_id, start.isoformat(), end.isoformat()),
        )

    def set_price(self, property_external_id: str, start: date, end: date, price: float) -> None:
        self._db.execute(
            "UPDATE mock_calendar_days SET price = ? "
            "WHERE property_external_id = ? AND day >= ? AND day < ?",
            (price, property_external_id, start.isoformat(), end.isoformat()),
        )

    # --- Mesaje --------------------------------------------------------------------

    def add_message(
        self, external_id: str, reservation_external_id: str, sender_type: str, body: str, sent_at: str
    ) -> None:
        self._db.execute(
            "INSERT INTO mock_messages (external_id, reservation_external_id, sender_type, body, "
            "sent_at) VALUES (?, ?, ?, ?, ?)",
            (external_id, reservation_external_id, sender_type, body, sent_at),
        )

    # --- Recenzii și piață ---------------------------------------------------------

    def reviews(self, property_external_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT data FROM mock_reviews WHERE property_external_id = ? ORDER BY external_id",
            (property_external_id,),
        )
        return [from_json(row["data"]) for row in rows]

    def comparables(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT data FROM mock_comparables ORDER BY ref")
        return [from_json(row["data"]) for row in rows]
