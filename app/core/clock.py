"""Ceasul aplicației. `today()` poate fi înghețat din config pentru teste și demo."""

from __future__ import annotations

from datetime import UTC, date, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


class Clock:
    def __init__(self, fixed_today: date | None = None) -> None:
        self._fixed_today = fixed_today

    def now(self) -> datetime:
        return utcnow()

    def today(self) -> date:
        return self._fixed_today or datetime.now().date()
