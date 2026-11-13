"""Adaptorul real de date de piață: un `listings.csv` de la Inside Airbnb, importat local.

- Sunt snapshot-uri trimestriale, nu prețuri live: le folosim ca referință de piață.
- Licența e CC BY 4.0, deci sursa se creditează în UI (`attribution`).
- Inside Airbnb nu acoperă orașe din România: orașul de piață e cel al fișierului configurat
  (`MARKET_CITY`), indiferent de orașul proprietății.

Fișierul (`listings.csv` sau `listings.csv.gz`) se importă o singură dată în tabelul
`market_listings`; după un reset al lumii se reimportă singur.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.core.contracts.models import Comparable, ComparableQuery
from app.core.db import Database, from_json, to_json
from app.ports.market import MarketDataError

logger = logging.getLogger("staypilot.inside_airbnb")

SOURCE = "inside_airbnb"
ATTRIBUTION = "Market data: Inside Airbnb (insideairbnb.com), licensed CC BY 4.0"
MAX_RESULTS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_listings (
    ref           TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    bedrooms      INTEGER NOT NULL,
    max_guests    INTEGER NOT NULL,
    amenities     TEXT NOT NULL,
    nightly_price REAL NOT NULL,
    url           TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_listings_size ON market_listings (bedrooms, nightly_price);
"""

# Facilitățile din CSV sunt text liber; le aducem la vocabularul nostru.
AMENITY_KEYWORDS = {
    "wifi": "wifi",
    "kitchen": "kitchen",
    "air conditioning": "air_conditioning",
    "washer": "washer",
    "self check-in": "self_check_in",
    "balcony": "balcony",
    "parking": "parking",
    "heating": "heating",
    "fireplace": "fireplace",
}


def normalize_amenities(raw: str) -> list[str]:
    try:
        names = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        names = [part.strip(' "') for part in raw.strip("{}[]").split(",")]
    found: set[str] = set()
    for name in names:
        lowered = str(name).lower()
        matched = [ours for keyword, ours in AMENITY_KEYWORDS.items() if keyword in lowered]
        found.update(matched or [re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")])
    return sorted(item for item in found if item)


def parse_price(raw: str) -> float | None:
    cleaned = re.sub(r"[^0-9.]", "", raw or "")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class InsideAirbnbMarketAdapter:
    name = SOURCE
    attribution: str | None = ATTRIBUTION

    def __init__(self, *, db: Database, csv_path: Path, city: str, currency: str = "EUR") -> None:
        self._db = db
        self._csv_path = Path(csv_path)
        self._city = city
        self._currency = currency

    def init_schema(self) -> None:
        """Creează tabelul și importă CSV-ul dacă tabelul e gol."""
        self._db.script(SCHEMA)
        row = self._db.query_one("SELECT COUNT(*) AS n FROM market_listings")
        if row and row["n"] == 0:
            imported = self.import_listings()
            logger.info("Imported %s Inside Airbnb listings from %s", imported, self._csv_path)

    def import_listings(self) -> int:
        try:
            raw = self._csv_path.read_bytes()
        except OSError as exc:
            raise MarketDataError(f"cannot read {self._csv_path}: {exc}") from exc
        if self._csv_path.suffix == ".gz":
            raw = gzip.decompress(raw)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        rows: list[tuple[Any, ...]] = []
        for record in reader:
            price = parse_price(record.get("price", ""))
            if price is None or not record.get("id"):
                continue  # anunțurile fără preț nu ajută la comparație
            rows.append(
                (
                    str(record["id"]),
                    record.get("name") or "Listing",
                    int(float(record.get("bedrooms") or 0)),
                    int(float(record.get("accommodates") or 1)),
                    to_json(normalize_amenities(record.get("amenities", ""))),
                    price,
                    record.get("listing_url") or None,
                )
            )
        with self._db.transaction():
            self._db.execute("DELETE FROM market_listings")
            for row in rows:
                self._db.execute(
                    "INSERT OR REPLACE INTO market_listings (ref, title, bedrooms, max_guests, "
                    "amenities, nightly_price, url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
        return len(rows)

    async def find_comparables(self, query: ComparableQuery) -> list[Comparable]:
        """Anunțurile cu același număr de dormitoare și loc suficient, cele mai ieftine întâi."""
        rows = self._db.query(
            "SELECT * FROM market_listings WHERE bedrooms = ? AND max_guests >= ? "
            "ORDER BY nightly_price LIMIT ?",
            (query.bedrooms, query.max_guests, MAX_RESULTS),
        )
        return [
            Comparable(
                ref=row["ref"],
                title=row["title"],
                city=self._city,
                bedrooms=row["bedrooms"],
                max_guests=row["max_guests"],
                amenities=from_json(row["amenities"], []),
                nightly_price=row["nightly_price"],
                currency=self._currency,
                source=SOURCE,
                url=row["url"],
            )
            for row in rows
        ]
