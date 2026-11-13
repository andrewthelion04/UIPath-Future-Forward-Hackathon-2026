"""Generarea ID-urilor interne."""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("6f1d0c1e-5b7a-4c53-9b61-57a9f0c0a001")


def new_id(prefix: str) -> str:
    """ID intern nou, aleator (ex. `evt_3fa9c1d2b7e4`)."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stable_id(prefix: str, *parts: str) -> str:
    """ID intern determinist, derivat din componente (aceleași intrări -> același ID)."""
    return f"{prefix}_{uuid.uuid5(_NAMESPACE, '|'.join(parts)).hex[:12]}"
