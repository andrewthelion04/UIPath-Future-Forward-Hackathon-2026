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
import json
import logging
import math
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
AMENITY_PATTERNS = {
    "wifi": r"^(?:fast |pocket )?wi[ -]?fi\b",
    "kitchen": r"^kitchen$",
    "air_conditioning": r"^(?:(?:central|portable|window|split[ -]type) )?air conditioning\b",
    "washer": r"^(?:(?:free|paid) )?washer\b",
    "self_check_in": r"^(?:self check[ -]in|lockbox|keypad|smart lock)$",
    "balcony": r"^(?:(?:private|shared) )?(?:patio or balcony|balcony)\b",
    "parking": r"^(?:(?:free|paid) )?(?:(?:street|driveway|valet) )?parking\b",
    "heating": r"^(?:(?:central|radiant|portable) )?heating\b",
    "fireplace": r"^(?:(?:indoor|outdoor) )?fireplace(?:$|:)",
}


def normalize_amenities(raw: str) -> list[str]:
    try:
        names = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        names = next(csv.reader([raw.strip("{}[]")], skipinitialspace=True)) if raw else []
    if not isinstance(names, list):
        return []  # un scalar sau un obiect JSON nu descrie o listă de facilități
    found: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            continue
        lowered = name.strip().lower().replace("–", "-").replace("‑", "-")
        matched = [ours for ours, pattern in AMENITY_PATTERNS.items() if re.search(pattern, lowered)]
        found.update(matched or [re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")])
    return sorted(item for item in found if item)


def parse_price(raw: str) -> float | None:
    match = re.fullmatch(r"\s*(?:[A-Z]{3}\s*|[$€£])?(\d+(?:,\d{3})*(?:\.\d{1,2})?)\s*", raw or "")
    if match is None:
        return None
    price = float(match[1].replace(",", ""))
    return price if math.isfinite(price) and price > 0 else None


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
            opener = gzip.open if self._csv_path.suffix.lower() == ".gz" else open
            with opener(self._csv_path, "rt", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                required = {"id", "bedrooms", "accommodates", "amenities", "price"}
                if not required <= set(reader.fieldnames or []):
                    raise MarketDataError("Detailed listings CSV required: missing " + ", ".join(sorted(required - set(reader.fieldnames or []))))
                return self._import_records(reader)
        except (OSError, EOFError, UnicodeError, csv.Error) as exc:
            raise MarketDataError(f"Cannot read Inside Airbnb dataset {self._csv_path}: {exc}") from exc

    def _import_records(self, reader: csv.DictReader) -> int:
        rows: list[tuple[Any, ...]] = []
        for record in reader:
            price = parse_price(record.get("price", ""))
            if price is None or not record.get("id"):
                continue  # anunțurile fără preț nu ajută la comparație
            try:
                bedrooms = float(record.get("bedrooms") or 0)
                accommodates = float(record.get("accommodates") or 1)
                if not (math.isfinite(bedrooms) and math.isfinite(accommodates)
                        and bedrooms.is_integer() and accommodates.is_integer()
                        and bedrooms >= 0 and accommodates > 0):
                    continue
            except ValueError:
                continue
            rows.append(
                (
                    str(record["id"]),
                    record.get("name") or "Listing",
                    int(bedrooms),
                    int(accommodates),
                    to_json(normalize_amenities(record.get("amenities", ""))),
                    price,
                    record.get("listing_url") or None,
                )
            )
        if not rows:
            raise MarketDataError("Inside Airbnb dataset has no usable listings with positive prices.")
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
            "ORDER BY nightly_price, ref",
            (query.bedrooms, query.max_guests),
        )
        # Păstrăm candidații de depanare și includem comparabilele înaintea plafonului.
        # Altfel 300 de camere ieftine fără facilități pot ascunde singurul comparabil.
        if len(rows) > MAX_RESULTS:
            required = set(query.amenities)
            matching = [row for row in rows if required <= set(from_json(row["amenities"], []))]
            rows = matching[:MAX_RESULTS] if matching else rows[:MAX_RESULTS]
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
