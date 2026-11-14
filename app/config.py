"""Configul unic al aplicației: toate comutatoarele mock <-> real și validarea fail-fast.

Schema și valorile implicite stau aici; valorile concrete vin din `.env` (necomis).
Implicit totul rulează pe mock, fără niciun secret.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import PrivateAttr, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT_DIR / ".env"

OWNERREZ_BASE_URLS = {
    "stage": "https://apistage.ownerrez.com",
    "production": "https://api.ownerrez.com",
}


class ConfigError(RuntimeError):
    """Config invalid sau secret lipsă pentru un adaptor real."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # --- Comutatoare mock <-> real -------------------------------------------------
    pms_provider: Literal["mock", "ownerrez", "hospitable"] = "mock"
    llm_provider: Literal["mock", "openrouter"] = "mock"
    reviews_provider: Literal["mock", "apify"] = "mock"
    market_provider: Literal["mock", "inside_airbnb"] = "mock"

    # --- Local ---------------------------------------------------------------------
    database_path: Path = ROOT_DIR / "data" / "staypilot.db"
    seed_path: Path = ROOT_DIR / "seed" / "world.json"
    auto_seed: bool = True
    fixed_today: date | None = None  # îngheață „azi” (teste, demo reproductibil)
    disabled_features: list[str] = []
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- Timeout-uri și limite -----------------------------------------------------
    http_timeout_s: float = 15.0
    llm_timeout_s: float = 60.0
    handler_timeout_s: float = 180.0
    sim_wait_timeout_s: float = 120.0
    agent_max_steps: int = 12
    agent_keep_last_rounds: int = 6
    agent_tool_output_max_chars: int = 3000

    # --- LLM (OpenRouter) ----------------------------------------------------------
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str | None = None
    llm_model_utility: str | None = None

    # --- Hospitable ----------------------------------------------------------------
    hospitable_base_url: str = "https://public.api.hospitable.com/v2"
    hospitable_api_token: SecretStr | None = None

    # --- OwnerRez ------------------------------------------------------------------
    ownerrez_env: Literal["stage", "production"] = "stage"
    ownerrez_base_url: str | None = None  # implicit dedus din ownerrez_env
    ownerrez_access_token: SecretStr | None = None  # OAuth (necesar pentru mesagerie)
    ownerrez_username: str | None = None  # alternativ: email + personal access token
    ownerrez_personal_token: SecretStr | None = None
    ownerrez_user_agent: str = "StayPilot AI"

    # --- Autentificarea API-ului ---------------------------------------------------
    # Dacă e setat, orice request cere `Authorization: Bearer <token>`, în afară de
    # `/health` și `/webhooks/*` (webhook-urile au verificările lor, vezi mai jos).
    api_token: SecretStr | None = None

    # --- Webhook-uri ---------------------------------------------------------------
    webhook_token: SecretStr | None = None  # dacă e setat, /webhooks/* cere ?token=...

    # --- Apify ---------------------------------------------------------------------
    apify_token: SecretStr | None = None
    apify_reviews_actor_id: str | None = None
    apify_reviews_input_template: str = '{"startUrls": [{"url": "{listing_url}"}]}'
    apify_timeout_s: float = 120.0

    # --- Inside Airbnb -------------------------------------------------------------
    inside_airbnb_listings_csv: Path | None = None
    market_city: str = "configured market city"
    market_currency: str = "EUR"

    _env_file_used: Path | None = PrivateAttr(default=None)

    @property
    def env_file_used(self) -> Path | None:
        return self._env_file_used

    @property
    def ownerrez_api_base_url(self) -> str:
        return self.ownerrez_base_url or OWNERREZ_BASE_URLS[self.ownerrez_env]

    @model_validator(mode="after")
    def _require_secrets_for_real_adapters(self) -> Settings:
        missing: list[str] = []
        if self.pms_provider == "hospitable" and not self.hospitable_api_token:
            missing.append("HOSPITABLE_API_TOKEN (required by PMS_PROVIDER=hospitable)")
        if self.pms_provider == "ownerrez":
            has_oauth = bool(self.ownerrez_access_token)
            has_basic = bool(self.ownerrez_username and self.ownerrez_personal_token)
            if not (has_oauth or has_basic):
                missing.append(
                    "OWNERREZ_ACCESS_TOKEN, or OWNERREZ_USERNAME + OWNERREZ_PERSONAL_TOKEN "
                    "(required by PMS_PROVIDER=ownerrez)"
                )
        if self.llm_provider == "openrouter":
            if not self.openrouter_api_key:
                missing.append("OPENROUTER_API_KEY (required by LLM_PROVIDER=openrouter)")
            if not self.llm_model:
                missing.append("LLM_MODEL (required by LLM_PROVIDER=openrouter)")
        if self.reviews_provider == "apify":
            if not self.apify_token:
                missing.append("APIFY_TOKEN (required by REVIEWS_PROVIDER=apify)")
            if not self.apify_reviews_actor_id:
                missing.append("APIFY_REVIEWS_ACTOR_ID (required by REVIEWS_PROVIDER=apify)")
        if self.market_provider == "inside_airbnb":
            csv_path = self.inside_airbnb_listings_csv
            if csv_path is None:
                missing.append(
                    "INSIDE_AIRBNB_LISTINGS_CSV (required by MARKET_PROVIDER=inside_airbnb)"
                )
            elif not csv_path.exists():
                missing.append(f"INSIDE_AIRBNB_LISTINGS_CSV points to a missing file: {csv_path}")
        if missing:
            raise ValueError("missing settings for real adapters: " + "; ".join(missing))
        return self


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE, **overrides: Any) -> Settings:
    """Încarcă și validează configul. Ridică `ConfigError` cu un mesaj lizibil."""
    try:
        settings = Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]).upper()
            message = err["msg"].removeprefix("Value error, ")
            lines.append(f"  - {where + ': ' if where else ''}{message}")
        raise ConfigError(
            "Invalid configuration (check .env against .env.example):\n" + "\n".join(lines)
        ) from exc
    settings._env_file_used = env_file
    return settings
