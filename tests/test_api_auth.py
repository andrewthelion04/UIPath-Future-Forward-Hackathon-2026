"""Autentificarea opțională a API-ului (`API_TOKEN`).

Fără token setat, totul merge ca înainte. Cu token, orice endpoint cere
`Authorization: Bearer <token>`, în afară de `/health` și `/webhooks/*`.
"""

from __future__ import annotations

import pytest

from conftest import app_client, make_settings
from ui import api_client

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def secured(tmp_path):
    with app_client(make_settings(tmp_path, api_token=TOKEN)) as client:
        yield client


def test_without_a_configured_token_the_api_stays_open(client):
    assert client.get("/properties").status_code == 200
    assert client.get("/approvals").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_requests_without_the_token_are_rejected(secured, headers):
    for method, path in (("GET", "/properties"), ("GET", "/approvals"), ("POST", "/sim/reset"),
                         ("POST", "/approvals/apr_x/decision"), ("GET", "/incidents")):  # fmt: skip
        response = secured.request(method, path, headers=headers, json={})
        assert response.status_code == 401, (method, path)
        assert response.headers["www-authenticate"] == "Bearer"


def test_requests_with_the_token_work(secured):
    assert len(secured.get("/properties", headers=AUTH).json()) == 6
    assert secured.get("/incidents", headers=AUTH).status_code == 200  # și routerele feature-urilor


def test_health_and_webhooks_do_not_need_the_api_token(secured):
    assert secured.get("/health").json()["status"] == "ok"
    webhook = secured.post("/webhooks/mock", json={"event": "reservation.updated"})
    assert webhook.status_code == 200 and webhook.json()["status"] == "ignored_not_message"


def test_a_rejected_request_still_carries_cors_headers_for_the_extension(secured):
    origin = {"Origin": "chrome-extension://abcdefghijklmnop"}
    preflight = secured.options(
        "/approvals", headers={**origin, "Access-Control-Request-Method": "GET",
                               "Access-Control-Request-Headers": "authorization"},
    )  # fmt: skip
    assert preflight.status_code == 200
    rejected = secured.get("/approvals", headers=origin)
    assert rejected.status_code == 401
    assert rejected.headers["access-control-allow-origin"] == origin["Origin"]


def test_the_streamlit_client_sends_the_token(tmp_path, monkeypatch):
    monkeypatch.delenv("STAYPILOT_API_TOKEN", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(api_client, "ENV_FILE", env_file)
    assert api_client.auth_headers() == {}  # fără fișier, fără token

    env_file.write_text('PMS_PROVIDER=mock\nAPI_TOKEN="from-file"  # local\nWEBHOOK_TOKEN=\n', encoding="utf-8")
    assert api_client.auth_headers() == {"Authorization": "Bearer from-file"}
    env_file.write_text("API_TOKEN=\n", encoding="utf-8")
    assert api_client.auth_headers() == {}

    monkeypatch.setenv("API_TOKEN", "from-env")
    assert api_client.auth_headers() == {"Authorization": "Bearer from-env"}
    monkeypatch.setenv("STAYPILOT_API_TOKEN", "explicit")
    assert api_client.auth_headers() == {"Authorization": "Bearer explicit"}
