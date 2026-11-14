"""Setările proprii ale F2 (prefix `F2_`), citite din același `.env` ca restul aplicației.

Valorile implicite sunt alese ca demo-ul să meargă singur și ca testele din afara F2 să treacă:
simularea e activă, fără întârzieri, iar oaspetele simulat acceptă relocarea.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from app.core.feature_context import FeatureSettings


class F2Settings(FeatureSettings):
    model_config = SettingsConfigDict(env_prefix="F2_", extra="ignore", env_ignore_empty=True)

    # Regula „reparație la timp” (impusă în cod, nu în LLM).
    fix_margin_hours: float = Field(2.0, ge=0)
    inhouse_tolerable_hours: float = Field(3.0, ge=0)

    # Timeouturi pentru oamenii care nu răspund.
    staff_reply_timeout_min: float = Field(30.0, gt=0)
    guest_answer_timeout_min: float = Field(60.0, gt=0)

    # Simularea personalului și a oaspetelui (demo fără nicio intervenție).
    sim_enabled: bool = True
    sim_staff_delay_s: float = Field(0.0, ge=0, le=600)
    sim_task_delay_s: float = Field(0.0, ge=0, le=600)
    sim_guest_answer: Literal["accept", "stay", "off"] = "accept"
    sim_guest_delay_s: float = Field(0.0, ge=0, le=600)
