"""Auto-discovery pentru feature-uri.

Orice subpachet din `app/features/` care are un modul `feature.py` cu funcția
`register(ctx)` este încărcat automat. Adăugarea unui feature nu cere editarea niciunui
fișier central. Un feature care nu se încarcă este raportat, iar celelalte pornesc normal.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.feature_context import FeatureContext

logger = logging.getLogger("staypilot.features")

FEATURES_PACKAGE = "app.features"


@dataclass
class FeatureStatus:
    name: str
    loaded: bool
    error: str | None = None


def discover_features(package: str = FEATURES_PACKAGE) -> list[str]:
    pkg = importlib.import_module(package)
    return sorted(
        module.name
        for module in pkgutil.iter_modules(pkg.__path__)
        if module.ispkg and not module.name.startswith("_")
    )


def feature_schema_files(package: str = FEATURES_PACKAGE) -> list[Path]:
    """Fișierele `schema.sql` ale feature-urilor (tabelele stau în zona fiecăruia)."""
    pkg = importlib.import_module(package)
    files: list[Path] = []
    for root in pkg.__path__:
        files.extend(sorted(Path(root).glob("*/schema.sql")))
    return files


def load_features(
    make_context: Callable[[str], FeatureContext],
    *,
    package: str = FEATURES_PACKAGE,
    disabled: tuple[str, ...] | list[str] = (),
) -> list[FeatureStatus]:
    statuses: list[FeatureStatus] = []
    for name in discover_features(package):
        if name in disabled:
            statuses.append(FeatureStatus(name, False, "disabled by configuration"))
            continue
        try:
            module = importlib.import_module(f"{package}.{name}.feature")
            ctx = make_context(name)
            module.register(ctx)
            ctx.commit()
            statuses.append(FeatureStatus(name, True))
        except Exception as exc:
            logger.exception("Feature %s failed to load; the others continue", name)
            statuses.append(FeatureStatus(name, False, f"{type(exc).__name__}: {exc}"))
    return statuses
