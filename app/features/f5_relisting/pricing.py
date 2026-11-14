"""Reguli pure de comparabilitate, validare și distribuție proporțională pe nopți."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from math import isfinite

from pydantic import BaseModel

from app.core.contracts.models import CalendarDay, Comparable, Property


class NightlyPrice(BaseModel):
    day: date
    original_price: float
    floor_price: float
    proposed_price: float


def positive_price(value: float) -> bool:
    return isfinite(value) and value > 0


def _money(value: Decimal, rounding: str = ROUND_HALF_UP) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=rounding))


def _percentage(value: float) -> Decimal:
    if not isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Percentage must be finite and between 0 and 100.")
    return Decimal(str(value)) / 100


def is_comparable(candidate: Comparable, prop: Property) -> bool:
    return (
        positive_price(candidate.nightly_price)
        and candidate.currency == prop.currency
        and set(prop.amenities) <= set(candidate.amenities)
        and candidate.bedrooms == prop.bedrooms
        and candidate.max_guests >= prop.max_guests
    )


def cheapest_comparable(candidates: list[Comparable], prop: Property) -> Comparable | None:
    matching = [c for c in candidates if is_comparable(c, prop)]
    return min(matching, key=lambda c: (c.nightly_price, c.ref)) if matching else None


def floor_price(base_price: float, max_discount_pct: float) -> float:
    if not positive_price(base_price):
        raise ValueError("Regular price must be finite and positive.")
    # Rotunjirea în sus păstrează limita hostului inclusiv la fracțiuni de cent.
    return _money(Decimal(str(base_price)) * (1 - _percentage(max_discount_pct)), ROUND_CEILING)


def propose_price(comparable_price: float, undercut_pct: float, floor: float) -> tuple[float, bool]:
    if not positive_price(comparable_price) or not isfinite(floor) or floor < 0:
        raise ValueError("Market price must be positive and the host floor non-negative.")
    price = max(0.01, _money(Decimal(str(comparable_price)) * (1 - _percentage(undercut_pct))))
    return (floor, True) if price < floor else (price, False)


def cap_price(price: float, original_price: float) -> float:
    if not positive_price(original_price) or not isfinite(price) or price < 0:
        raise ValueError("Invalid proposed or original price.")
    return min(price, original_price)


def validate_calendar(
    days: list[CalendarDay], property_id: str, start: date, end: date, currency: str
) -> list[CalendarDay]:
    """Un snapshot incomplet, duplicat sau cu altă monedă nu justifică scrieri."""
    expected = [start + timedelta(days=i) for i in range((end - start).days)]
    ordered = sorted(days, key=lambda d: d.day)
    if not expected or [d.day for d in ordered] != expected:
        raise ValueError("Calendar must contain every requested night exactly once.")
    if any(d.property_id != property_id or d.currency != currency for d in ordered):
        raise ValueError("Calendar property or currency does not match the proposal.")
    if any(not positive_price(d.price) or round(d.price, 2) != d.price for d in ordered):
        raise ValueError("Calendar prices must be positive, finite amounts in whole cents.")
    return ordered


def distribute_prices(
    days: list[CalendarDay], base_price: float, comparable_price: float,
    undercut_pct: float, max_discount_pct: float,
) -> list[NightlyPrice]:
    """Aplică același factor tuturor nopților, cu prag și plafon individual."""
    floor_price(base_price, max_discount_pct)  # validează ancora și regula hostului
    reference, _ = propose_price(comparable_price, undercut_pct, 0)
    factor = min(Decimal(1), max(
        1 - _percentage(max_discount_pct),
        Decimal(str(reference)) / Decimal(str(base_price)),
    ))
    result = []
    for day in days:
        floor = floor_price(day.price, max_discount_pct)
        price = _money(Decimal(str(day.price)) * factor)
        result.append(NightlyPrice(
            day=day.day, original_price=day.price, floor_price=floor,
            # Cel puțin un cent: o noapte cu preț pozitiv nu devine gratuită prin rotunjire.
            proposed_price=cap_price(max(price, floor, 0.01), day.price),
        ))
    return result
