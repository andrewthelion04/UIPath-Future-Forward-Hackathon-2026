"""Regulile de arhitectură, verificate automat pe codul sursă.

- un feature nu importă alt feature;
- un feature nu importă adaptoare concrete;
- nucleul și porturile nu importă feature-uri sau adaptoare;
- paginile de UI consumă doar API-ul REST (nu importă backend-ul);
- fiecare feature are fișierele cerute în zona lui.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.config import ROOT_DIR
from app.core.feature_loader import discover_features

FEATURES_DIR = ROOT_DIR / "app" / "features"
FEATURES = discover_features()


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def python_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*.py") if "__pycache__" not in p.parts]


def test_the_five_features_are_discovered():
    assert FEATURES == [
        "f1_pms_integration",
        "f2_incidents",
        "f3_guest_copilot",
        "f4_review_analysis",
        "f5_relisting",
    ]


@pytest.mark.parametrize("feature", FEATURES)
def test_feature_imports_no_other_feature_and_no_concrete_adapter(feature):
    own_prefix = f"app.features.{feature}"
    for path in python_files(FEATURES_DIR / feature):
        if path.name == "ui_page.py":
            continue
        for module in imported_modules(path):
            if module.startswith("app.features"):
                assert module == own_prefix or module.startswith(own_prefix + "."), (
                    f"{path.relative_to(ROOT_DIR)} imports another feature: {module}"
                )
            if "tests" not in path.parts:  # doar testele pot construi aplicația întreagă
                assert not module.startswith(("app.adapters", "app.container", "app.main", "app.routers")), (
                    f"{path.relative_to(ROOT_DIR)} imports {module}"
                )


@pytest.mark.parametrize("layer", ["core", "ports"])
def test_core_and_ports_do_not_depend_on_features_or_adapters(layer):
    for path in python_files(ROOT_DIR / "app" / layer):
        for module in imported_modules(path):
            assert not module.startswith(("app.features", "app.adapters", "app.container")), (
                f"{path.relative_to(ROOT_DIR)} imports {module}"
            )


def test_adapters_do_not_depend_on_features():
    for path in python_files(ROOT_DIR / "app" / "adapters"):
        for module in imported_modules(path):
            assert not module.startswith("app.features"), f"{path.relative_to(ROOT_DIR)} imports {module}"


def test_ui_talks_only_to_the_rest_api():
    pages = python_files(ROOT_DIR / "ui") + list(FEATURES_DIR.glob("*/ui_page.py"))
    assert pages
    for path in pages:
        for module in imported_modules(path):
            assert not module.startswith("app"), (
                f"{path.relative_to(ROOT_DIR)} imports backend code: {module}"
            )


@pytest.mark.parametrize("feature", FEATURES)
def test_feature_owns_its_files(feature):
    folder = FEATURES_DIR / feature
    for required in ("feature.py", "README.md", "CLAUDE.md", "ui_page.py"):
        assert (folder / required).exists(), f"{feature} is missing {required}"
    assert list((folder / "tests").glob("test_*.py")), f"{feature} has no tests"
    notes = (folder / "CLAUDE.md").read_text(encoding="utf-8")
    for section in ("## Scop", "## Contract", "## Stare curentă", "## Decizii", "## Plan faza 1",
                    "## Reguli pentru implementare (one-shot)",
                    "## Probleme deschise / TODO", "## Note pentru Claude"):  # fmt: skip
        assert section in notes, f"{feature}/CLAUDE.md is missing '{section}'"
