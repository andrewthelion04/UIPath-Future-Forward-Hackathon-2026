"""F1 — Integrare PMS (doar transport). Punctul de intrare descoperit automat."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps

from app.core.feature_context import FeatureContext
from app.core.sync_service import SyncReport
from app.features.f1_pms_integration.inbound import InboundPipeline
from app.features.f1_pms_integration.router import build_router
from app.features.f1_pms_integration.settings import F1Settings


def register(ctx: FeatureContext) -> None:
    settings = ctx.load_settings(F1Settings)
    pipeline = InboundPipeline(ctx)
    ctx.include_router(build_router(ctx, pipeline, settings))

    def install_hooks() -> None:
        original_sync = ctx.sync.sync
        original_link = ctx.ids.link
        syncing: ContextVar[bool] = ContextVar("f1_syncing", default=False)

        @wraps(original_sync)
        async def sync(provider: str, *, publish_events: bool = True) -> SyncReport:
            async with pipeline.sync_lock(provider):
                token = syncing.set(True)
                try:
                    report = await original_sync(provider, publish_events=publish_events)
                finally:
                    syncing.reset(token)
                # Numai după succes: mapările dintr-un sync parțial nu livrează mesaje.
                await pipeline.reprocess_unmapped(provider)
                return report

        @wraps(original_link)
        def link(connection: str, entity_type: str, external_id: str, internal_id: str) -> None:
            original_link(connection, entity_type, external_id, internal_id)
            if not syncing.get():
                pipeline.schedule_reprocess(connection)

        ctx.sync.sync = sync
        ctx.ids.link = link

    # Hookurile se instalează numai la commit, după validarea întregii înregistrări.
    ctx._pending.append(install_hooks)
