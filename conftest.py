"""Fixture-uri comune pentru toate testele (nucleu și feature-uri).

Totul rulează pe mock, cu o bază de date temporară și o dată fixă, fără rețea și fără `.env`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.container import Container
from app.main import create_app

TODAY = date(2026, 6, 1)  # luni


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_path": tmp_path / "test.db",
        "pms_provider": "mock",
        "llm_provider": "mock",
        "reviews_provider": "mock",
        "market_provider": "mock",
        "fixed_today": TODAY,
        "handler_timeout_s": 10,
        "sim_wait_timeout_s": 20,
        "webhook_token": None,
        "disabled_features": [],
    }
    values.update(overrides)
    return load_settings(env_file=None, **values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def container(settings) -> AsyncIterator[Container]:
    """Aplicația legată complet (cu feature-uri), fără stratul HTTP."""
    c = Container(settings)
    await c.startup()
    try:
        yield c
    finally:
        await c.shutdown()


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    """API-ul complet, cu lumea seed-uită. `client.app.state.container` dă acces la servicii."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@contextmanager
def app_client(settings: Settings, **overrides) -> Iterator[TestClient]:
    """Ca fixture-ul `client`, dar cu porturi înlocuite: `llm`, `reviews`, `market`, `pms_connections`."""
    with TestClient(create_app(settings, overrides=overrides)) as test_client:
        yield test_client


def drain(client: TestClient, timeout: float = 15) -> bool:
    """Așteaptă handlerii porniți în background de un request fără `?wait=true`."""
    return client.portal.call(client.app.state.container.bus.join, timeout)


def events_of(client: TestClient, event_type: str) -> list[dict]:
    return [e for e in client.get("/events").json() if e["type"] == event_type]


def approve(client: TestClient, approval_id: str, option: str | None = None) -> dict:
    body = {"decision": "approve", "option": option}
    return client.post(f"/approvals/{approval_id}/decision", params={"wait": True}, json=body).json()


def reject(client: TestClient, approval_id: str) -> dict:
    return client.post(
        f"/approvals/{approval_id}/decision", params={"wait": True}, json={"decision": "reject"}
    ).json()


def booking_by_guest(client: TestClient, guest_name: str) -> dict:
    return next(b for b in client.get("/bookings").json() if b["guest_name"] == guest_name)


def property_by_name(client: TestClient, name: str) -> dict:
    return next(p for p in client.get("/properties").json() if p["name"] == name)
