"""Leagă proprietățile demo de anunțurile Airbnb reale din `listings.json` (pentru Apify).

Rulează cu API-ul pornit:

    .venv/Scripts/python -m app.features.f4_review_analysis.link_listings

Folosește doar API-ul REST (`GET /properties`, `PUT /integrations/mappings`), ca UI-ul.
E idempotent: o legătură existentă rămâne neschimbată. Rulează-l din nou după
`python manage.py seed` sau după reset-ul lumii, care șterg corespondențele.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

LISTINGS_FILE = Path(__file__).parent / "listings.json"
CONNECTION, ENTITY_TYPE = "airbnb", "listing"


def load_listings(path: Path = LISTINGS_FILE) -> dict[str, str]:
    """Numele proprietății demo -> URL-ul anunțului (fără parametri de query)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: url.split("?")[0].strip() for name, url in data["listings"].items()}


def link_all(client: httpx.Client, listings: dict[str, str]) -> list[str]:
    """Creează legăturile. Întoarce câte un rând de raport per proprietate."""
    properties = {p["name"]: p["id"] for p in client.get("/properties").raise_for_status().json()}
    report = []
    for name, url in listings.items():
        prop_id = properties.get(name)
        if prop_id is None:
            report.append(f"MISSING  {name}: no such property")
            continue
        response = client.put(
            "/integrations/mappings",
            json={"connection": CONNECTION, "entity_type": ENTITY_TYPE,
                  "external_id": url, "internal_id": prop_id},
        )  # fmt: skip
        if response.status_code == 409:
            report.append(f"CONFLICT {name}: {response.json().get('detail')}")
        else:
            response.raise_for_status()
            report.append(f"LINKED   {name} ({prop_id}) -> {url}")
    for name in sorted(set(properties) - set(listings)):
        report.append(f"UNLINKED {name}: no listing; review analysis will use the fallback")
    return report


def main() -> int:
    api_url = os.environ.get("STAYPILOT_API_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        # Tokenul opțional al API-ului (`API_TOKEN` în `.env`-ul backendului), dat prin mediu.
        token = os.environ.get("STAYPILOT_API_TOKEN") or os.environ.get("API_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(base_url=api_url, timeout=10, headers=headers) as client:
            lines = link_all(client, load_listings())
    except httpx.HTTPError as exc:
        print(f"Cannot reach the API at {api_url}: {exc}. Start it with: python manage.py api")
        return 1
    print("\n".join(lines))
    return 1 if any(line.startswith(("MISSING", "CONFLICT")) for line in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
