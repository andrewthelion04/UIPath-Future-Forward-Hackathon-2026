"""Auto-discovery: un feature nou se încarcă singur, iar unul stricat nu le oprește pe celelalte."""

from __future__ import annotations

import sys
import textwrap

import pytest

from app.container import Container
from app.core.contracts.events import ReviewAnalysisRequested
from conftest import make_settings

GOOD_FEATURE = """
    from fastapi import APIRouter
    from app.core.contracts.events import ReviewAnalysisRequested

    def register(ctx):
        async def on_request(event):
            ctx.notify("good feature handled the event")

        router = APIRouter()

        @router.get("/good/ping")
        async def ping():
            return {"feature": ctx.feature}

        ctx.subscribe(ReviewAnalysisRequested, on_request)
        ctx.include_router(router)
"""

BROKEN_FEATURE = """
    from app.core.contracts.events import ReviewAnalysisRequested

    def register(ctx):
        async def on_request(event):
            ctx.notify("broken feature must never run")

        ctx.subscribe(ReviewAnalysisRequested, on_request)
        raise RuntimeError("cannot start")
"""


@pytest.fixture
def fake_features(tmp_path, monkeypatch) -> str:
    package = tmp_path / "fake_features"
    for name, source in (("good", GOOD_FEATURE), ("broken", BROKEN_FEATURE)):
        folder = package / name
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text("", encoding="utf-8")
        (folder / "feature.py").write_text(textwrap.dedent(source), encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "good" / "schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS good_things (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "fake_features"
    for module in [m for m in sys.modules if m.startswith("fake_features")]:
        del sys.modules[module]


async def test_broken_feature_is_reported_and_the_rest_keeps_working(tmp_path, fake_features):
    container = Container(make_settings(tmp_path), features_package=fake_features)
    await container.startup()
    try:
        statuses = {f.name: f for f in container.features}
        assert statuses["good"].loaded is True
        assert statuses["broken"].loaded is False and "cannot start" in statuses["broken"].error

        # Feature-ul bun are routerul, tabelele și abonarea; cel stricat nu a lăsat nimic în urmă.
        assert [r.path for router in container.routers for r in router.routes] == ["/good/ping"]
        assert "good_things" in container.db.table_names()
        assert [s.name for s in container.bus.subscriptions()] == ["good.on_request"]

        await container.bus.publish(ReviewAnalysisRequested())
        await container.bus.join(timeout=5)
        titles = [n.title for n in container.notifications.list()]
        assert "good feature handled the event" in titles
        assert "broken feature must never run" not in titles
        assert any("broken failed to load" in title for title in titles)  # hostul află
    finally:
        await container.shutdown()


async def test_feature_can_be_disabled_from_config(tmp_path):
    settings = make_settings(tmp_path, disabled_features=["f4_review_analysis"])
    container = Container(settings)
    try:
        statuses = {f.name: f for f in container.features}
        assert statuses["f4_review_analysis"].loaded is False
        assert all(f.loaded for name, f in statuses.items() if name != "f4_review_analysis")
        assert not any(s.feature == "f4_review_analysis" for s in container.bus.subscriptions())
    finally:
        await container.shutdown()
