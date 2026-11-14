"""Setările de autentificare proprii F1; se încarcă din același `.env` ca aplicația."""

from __future__ import annotations

import re
from ipaddress import ip_network

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.core.feature_context import FeatureSettings


class F1Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F1_")

    ownerrez_webhook_username: str | None = None
    ownerrez_webhook_password: SecretStr | None = None
    hospitable_webhook_allow_cidrs: str | None = None

    @model_validator(mode="after")
    def complete_basic_credentials(self) -> F1Settings:
        if (self.ownerrez_webhook_username is None) != (self.ownerrez_webhook_password is None):
            raise ValueError(
                "F1_OWNERREZ_WEBHOOK_USERNAME and F1_OWNERREZ_WEBHOOK_PASSWORD must be set together"
            )
        return self

    @field_validator("hospitable_webhook_allow_cidrs")
    @classmethod
    def valid_allow_cidrs(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = [part for part in re.split(r"[,\s]+", value.strip()) if part]
        if not parts:
            raise ValueError("F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS must contain at least one CIDR")
        try:
            return ",".join(str(ip_network(part, strict=False)) for part in parts)
        except ValueError as exc:
            raise ValueError("F1_HOSPITABLE_WEBHOOK_ALLOW_CIDRS contains an invalid CIDR") from exc
