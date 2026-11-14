"""Autentificarea F1, cu setări locale și clienți HTTP fără rețea."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.features.f1_pms_integration.settings import F1Settings
from app.main import create_app
from conftest import app_client, make_settings


def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


@pytest.fixture
def ownerrez_credentials(monkeypatch):
    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_USERNAME", "hook-user")
    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_PASSWORD", "hook-pass")


@pytest.mark.parametrize("authorization", [
    None,
    "Bearer hook-pass",
    "Basic !!!!",
    "Basic " + base64.b64encode(b"missing-colon").decode(),
    basic("wrong", "hook-pass"),
    basic("hook-user", "wrong"),
    "Basic " + base64.b64encode(b"\xff:\xfe").decode(),
])
def test_ownerrez_rejects_invalid_basic_auth(settings, ownerrez_credentials, authorization):
    with app_client(settings) as client:
        headers = {"Authorization": authorization} if authorization is not None else {}
        response = client.post("/webhooks/ownerrez", json={}, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Basic"
        assert client.get("/f1/webhook-log").json() == []


def test_ownerrez_accepts_basic_auth_without_affecting_other_providers(settings, ownerrez_credentials):
    with app_client(settings) as client:
        accepted = client.post(
            "/webhooks/ownerrez", json={}, headers={"Authorization": basic("hook-user", "hook-pass")}
        )
        assert accepted.status_code == 200
        # Autentificarea a trecut; conexiunea OwnerRez nu e activată în lumea mock.
        assert accepted.json()["status"] == "error"
        assert client.post("/webhooks/mock", json={}).status_code == 200
        assert client.post("/webhooks/hospitable", json={}).status_code == 200


def test_ownerrez_basic_auth_and_common_token_are_both_required(tmp_path, ownerrez_credentials):
    settings = make_settings(tmp_path, webhook_token="shared-token")
    with app_client(settings) as client:
        headers = {"Authorization": basic("hook-user", "hook-pass")}
        assert client.post("/webhooks/ownerrez", json={}, headers=headers).status_code == 403
        assert client.post(
            "/webhooks/ownerrez", json={}, params={"token": "shared-token"}
        ).status_code == 401
        assert client.post(
            "/webhooks/ownerrez", json={}, params={"token": "shared-token"}, headers=headers
        ).status_code == 200


def test_ownerrez_basic_auth_handles_unicode_and_password_colons(settings, monkeypatch):
    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_USERNAME", "utilizător")
    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_PASSWORD", "parolă:secretă")
    with app_client(settings) as client:
        response = client.post(
            "/webhooks/ownerrez", json={},
            headers={"Authorization": basic("utilizător", "parolă:secretă")},
        )
        assert response.status_code == 200


@pytest.mark.parametrize("client_ip, expected", [
    ("38.80.170.0", 200),
    ("38.80.170.255", 200),
    ("38.80.171.1", 403),
    ("127.0.0.1", 403),
    ("2001:db8::42", 200),
    ("2001:db9::42", 403),
    ("testclient", 403),
])
def test_hospitable_filters_real_client_ip(settings, monkeypatch, client_ip, expected):
    monkeypatch.setenv("F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS", "38.80.170.0/24, 2001:db8::/32")
    with TestClient(create_app(settings), client=(client_ip, 50000)) as client:
        response = client.post("/webhooks/hospitable", json={})
        assert response.status_code == expected
        assert len(client.get("/f1/webhook-log").json()) == (1 if expected == 200 else 0)
        # Filtrul nu se aplică simulatorului sau altei conexiuni.
        assert client.post("/webhooks/mock", json={}).status_code == 200
        assert client.post("/webhooks/ownerrez", json={}).status_code == 200


def test_hospitable_does_not_trust_forwarded_ip_headers(settings, monkeypatch):
    monkeypatch.setenv("F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS", "38.80.170.0/24")
    with TestClient(create_app(settings), client=("192.0.2.10", 50000)) as client:
        response = client.post(
            "/webhooks/hospitable", json={},
            headers={"X-Forwarded-For": "38.80.170.5", "Forwarded": "for=38.80.170.5"},
        )
        assert response.status_code == 403


def test_hospitable_ip_filter_is_disabled_by_default(client):
    assert client.post("/webhooks/hospitable", json={}).status_code == 200


@pytest.mark.parametrize("values", [
    {"ownerrez_webhook_username": "user"},
    {"ownerrez_webhook_password": "secret"},
    {"hospitable_webhook_allow_cidrs": "not-a-network"},
    {"hospitable_webhook_allow_cidrs": ", ,"},
])
def test_invalid_f1_auth_configuration_fails_validation(values):
    with pytest.raises(ValidationError):
        F1Settings(_env_file=None, **values)


def test_f1_settings_load_prefix_and_hide_password(monkeypatch):
    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_USERNAME", "hook-user")
    monkeypatch.setenv("F1_OWNERREZ_WEBHOOK_PASSWORD", "unique-test-secret")
    monkeypatch.setenv("F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS", "38.80.170.4/24 2001:db8::/32")
    settings = F1Settings(_env_file=None)
    assert settings.ownerrez_webhook_username == "hook-user"
    assert settings.ownerrez_webhook_password.get_secret_value() == "unique-test-secret"
    assert "unique-test-secret" not in repr(settings)
    assert settings.hospitable_webhook_allow_cidrs == "38.80.170.0/24,2001:db8::/32"
