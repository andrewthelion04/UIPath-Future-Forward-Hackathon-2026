"""Expirarea folosește ziua proprietății, inclusiv lângă miezul nopții UTC."""

from datetime import date, datetime

import pytest

from app.features.f5_relisting.tests.test_relisting import calendar, cancel
from conftest import app_client, approve


@pytest.mark.parametrize("timezone, instant, expected", [
    ("Europe/Bucharest", "2026-06-03T22:30:00+00:00", "expired"),
    ("America/Los_Angeles", "2026-06-03T01:00:00+00:00", "awaiting_approval"),
])
def test_server_date_cannot_delay_or_advance_expiration(settings, monkeypatch, timezone, instant, expected):
    monkeypatch.setenv("F5_TIMEZONE", timezone)
    with app_client(settings) as client:
        booking = cancel(client, "Emma Johnson")
        proposal = client.get("/f5/proposals").json()[0]
        container = client.app.state.container
        monkeypatch.setattr(container.settings, "fixed_today", None)
        monkeypatch.setattr(container.clock, "today", lambda: date(2026, 6, 3))
        monkeypatch.setattr(container.clock, "now", lambda: datetime.fromisoformat(instant))
        before = calendar(client, booking)
        assert client.get("/f5/proposals").json()[0]["status"] == expected
        approve(client, proposal["approval_id"])
        if expected == "expired":
            assert calendar(client, booking) == before
            assert client.get("/f5/proposals").json()[0]["status"] == "expired"
        else:
            assert client.get("/f5/proposals").json()[0]["status"] == "applied"
