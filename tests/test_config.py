"""Fail-fast la pornire: config invalid sau secret lipsă pentru un adaptor real."""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings
from app.main import create_app


def test_defaults_use_mock_integrations_and_local_market_fixture_without_secrets(tmp_path):
    settings = load_settings(env_file=None, database_path=tmp_path / "db.sqlite")
    assert (
        settings.pms_provider,
        settings.llm_provider,
        settings.reviews_provider,
        settings.market_provider,
    ) == ("mock", "mock", "mock", "inside_airbnb")
    assert settings.inside_airbnb_listings_csv == settings.inside_airbnb_listings_csv.resolve()
    assert settings.inside_airbnb_listings_csv.name == "athens_2026-09-22.sample.csv"
    assert settings.market_city == "Athens"
    assert settings.market_currency == "EUR"
    assert settings.market_usd_to_currency_rate == 0.92


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"pms_provider": "hospitable"}, "HOSPITABLE_API_TOKEN"),
        ({"pms_provider": "ownerrez"}, "OWNERREZ_ACCESS_TOKEN"),
        ({"pms_provider": "ownerrez", "ownerrez_username": "me@example.com"}, "OWNERREZ_PERSONAL_TOKEN"),
        ({"llm_provider": "openrouter"}, "OPENROUTER_API_KEY"),
        ({"llm_provider": "openrouter", "openrouter_api_key": "sk-or-x"}, "LLM_MODEL"),
        ({"reviews_provider": "apify"}, "APIFY_TOKEN"),
        ({"reviews_provider": "apify", "apify_token": "t"}, "APIFY_REVIEWS_ACTOR_ID"),
        ({"market_provider": "inside_airbnb", "inside_airbnb_listings_csv": None}, "INSIDE_AIRBNB_LISTINGS_CSV"),
        ({"market_provider": "inside_airbnb", "inside_airbnb_listings_csv": "missing.csv"}, "missing file"),
    ],
)
def test_real_adapter_without_its_secret_stops_with_a_clear_message(overrides, expected):
    with pytest.raises(ConfigError) as error:
        load_settings(env_file=None, **overrides)
    assert expected in str(error.value)


def test_all_missing_secrets_are_listed_together():
    with pytest.raises(ConfigError) as error:
        load_settings(env_file=None, pms_provider="hospitable", llm_provider="openrouter")
    message = str(error.value)
    assert "HOSPITABLE_API_TOKEN" in message and "OPENROUTER_API_KEY" in message


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError) as error:
        load_settings(env_file=None, pms_provider="guesty")
    assert "PMS_PROVIDER" in str(error.value)


def test_real_adapters_are_accepted_when_their_secrets_exist(tmp_path):
    csv_file = tmp_path / "listings.csv"
    csv_file.write_text("id,name\n", encoding="utf-8")
    settings = load_settings(
        env_file=None,
        pms_provider="hospitable",
        hospitable_api_token="hsp-secret-value",
        llm_provider="openrouter",
        openrouter_api_key="sk-or-x",
        llm_model="some/model",
        reviews_provider="apify",
        apify_token="t",
        apify_reviews_actor_id="user~actor",
        market_provider="inside_airbnb",
        inside_airbnb_listings_csv=csv_file,
    )
    assert settings.pms_provider == "hospitable"
    assert "hsp-secret-value" not in repr(settings)  # secretele nu apar în loguri


@pytest.mark.parametrize("rate", [0, -0.5, float("inf"), float("nan")])
def test_market_currency_conversion_rate_must_be_finite_and_positive(rate):
    with pytest.raises(ConfigError, match="MARKET_USD_TO_CURRENCY_RATE"):
        load_settings(env_file=None, market_usd_to_currency_rate=rate)


def test_switching_is_done_from_the_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PMS_PROVIDER=ownerrez   # comentariu\nOWNERREZ_ACCESS_TOKEN=at_123\nOPENROUTER_API_KEY=\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file)
    assert settings.pms_provider == "ownerrez"
    assert settings.ownerrez_api_base_url == "https://apistage.ownerrez.com"
    assert settings.openrouter_api_key is None  # valoarea goală înseamnă „nesetat”

    env_file.write_text("LLM_PROVIDER=openrouter\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(env_file=env_file)


def test_the_app_refuses_to_start_with_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PMS_PROVIDER", "hospitable")
    monkeypatch.delenv("HOSPITABLE_API_TOKEN", raising=False)
    monkeypatch.setattr("app.config.DEFAULT_ENV_FILE", tmp_path / "no.env")
    with pytest.raises(ConfigError, match="HOSPITABLE_API_TOKEN"):
        create_app()
