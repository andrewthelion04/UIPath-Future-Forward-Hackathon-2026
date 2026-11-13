"""Evenimente private ale F2.

Stau aici cât timp le emite doar F2 și nu le consumă nimeni. Dacă alt feature are nevoie de
ele, se mută în `app/core/contracts/events.py` (contractele comune), prin adăugare.
"""

from __future__ import annotations

from typing import Literal

from app.core.contracts.events import Event


class IncidentResolved(Event):
    incident_id: str
    property_id: str
    booking_id: str | None = None
    resolution: Literal["remediation", "relocation", "dismissed"]
