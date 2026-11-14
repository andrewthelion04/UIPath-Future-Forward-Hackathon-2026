"""Evenimente private ale F4.

Stau aici cât timp le emite și le consumă doar F4. Dacă alt feature are nevoie de ele, se mută
în `app/core/contracts/events.py` (contractele comune), prin adăugare.
"""

from __future__ import annotations

from app.core.contracts.events import Event


class PropertyReviewAnalysisRequested(Event):
    """Analiza recenziilor pentru O SINGURĂ proprietate.

    `ReviewAnalysisRequested` (comun) se desface în câte un astfel de eveniment per proprietate,
    ca fiecare analiză să ruleze în handlerul ei, cu timeoutul ei: o proprietate lentă sau care
    cade nu le blochează pe celelalte și nu consumă timeoutul întregii cereri.
    """

    property_id: str
    requested_by: str = "host"
