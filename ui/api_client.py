"""Client HTTP subțire pentru API-ul REST StayPilot.

UI-ul nu importă nimic din backend: tot ce afișează vine de aici, la fel ca în extensia Chrome.
Adresa API-ului se poate schimba cu variabila de mediu `STAYPILOT_API_URL`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

API_URL = os.environ.get("STAYPILOT_API_URL", "http://127.0.0.1:8000").rstrip("/")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def api_token() -> str | None:
    """Tokenul API-ului: `STAYPILOT_API_TOKEN`, apoi `API_TOKEN` din mediu sau din `.env`.

    Fișierul `.env` e citit direct (nu prin backend), ca același token să fie setat o singură
    dată pentru API și pentru simulator.
    """
    for name in ("STAYPILOT_API_TOKEN", "API_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == "API_TOKEN":
            value = value.split(" #")[0].strip().strip("\"'")
            return value or None
    return None


def auth_headers() -> dict[str, str]:
    token = api_token()
    return {"Authorization": f"Bearer {token}"} if token else {}
TIMEOUT_S = 150.0  # cu ?wait=true, un request durează cât fluxul pe care îl pornește


class ApiError(RuntimeError):
    pass


def request(
    method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None
) -> Any:
    query = {key: value for key, value in (params or {}).items() if value is not None}
    try:
        response = httpx.request(
            method, f"{API_URL}{path}", params=query, json=json, timeout=TIMEOUT_S,
            headers=auth_headers(),
        )
    except httpx.HTTPError as exc:
        raise ApiError(
            f"Cannot reach the API at {API_URL} ({type(exc).__name__}). "
            "Start it with: python manage.py api"
        ) from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        if response.status_code == 401:
            detail = f"{detail}. Set API_TOKEN in .env (or STAYPILOT_API_TOKEN) to the API's token."
        raise ApiError(f"{response.status_code}: {detail}")
    return response.json() if response.content else None


def get(path: str, **params: Any) -> Any:
    return request("GET", path, params=params)


def post(path: str, json: Any = None, **params: Any) -> Any:
    return request("POST", path, params=params, json=json)


def put(path: str, json: Any = None, **params: Any) -> Any:
    return request("PUT", path, params=params, json=json)
