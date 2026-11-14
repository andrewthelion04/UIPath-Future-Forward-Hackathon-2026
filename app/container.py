"""Composition root: singurul loc care cunoaște adaptoarele concrete.

Aici se alege, pe baza configului, ce adaptor stă în spatele fiecărui port și se leagă
serviciile nucleului. Feature-urile primesc totul printr-un `FeatureContext`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter

from app.adapters.mock.llm import MockLLM
from app.adapters.mock.market import MockMarketAdapter
from app.adapters.mock.pms import MockPmsAdapter
from app.adapters.mock.reviews import MockReviewsAdapter
from app.adapters.mock.world import MockWorld
from app.config import Settings
from app.core.agent.loop import AgentRunner
from app.core.agent.trace import AgentTrace
from app.core.approvals import ApprovalService
from app.core.calendar_gateway import CalendarGateway
from app.core.channel_gateway import ChannelGateway
from app.core.clock import Clock
from app.core.contracts.events import Event
from app.core.conversation_log import ConversationLog
from app.core.db import Database
from app.core.event_bus import EventBus, Subscription
from app.core.feature_context import FeatureContext
from app.core.flow_story import FlowStories
from app.core.guest_topics import GuestTopics
from app.core.feature_loader import (
    FEATURES_PACKAGE,
    FeatureStatus,
    feature_schema_files,
    load_features,
)
from app.core.id_mapper import IdMapper
from app.core.notifications import NotificationService
from app.core.sync_service import SyncService
from app.core.world_repo import WorldRepository
from app.ports.llm import LLMPort, MockPolicy
from app.ports.market import MarketDataPort
from app.ports.pms import PmsConnection, PmsRegistry
from app.ports.reviews import ReviewsPort
from app.ports.simulation import SimulationPort

logger = logging.getLogger("staypilot.container")
CORE_SCHEMA = Path(__file__).parent / "core" / "schema.sql"


class Container:
    """Leagă toată aplicația.

    `overrides` permite testelor să înlocuiască un port cu o dublură:
    `llm`, `reviews`, `market` sau `pms_connections` (listă de conexiuni în plus).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        overrides: dict[str, Any] | None = None,
        features_package: str = FEATURES_PACKAGE,
    ) -> None:
        overrides = overrides or {}
        self.settings = settings
        self.clock = Clock(settings.fixed_today)
        self.db = Database(settings.database_path)
        self._features_package = features_package

        # --- Nucleul ---------------------------------------------------------------
        self.ids = IdMapper(self.db)
        self.world = WorldRepository(self.db)
        self.conversations = ConversationLog(self.db)
        self.guest_topics = GuestTopics(self.db, self.clock)
        self.notifications = NotificationService(self.db)
        self.bus = EventBus(self.db, default_timeout_s=settings.handler_timeout_s)
        self.bus.failure_hook = self._notify_handler_failure
        self.approvals = ApprovalService(self.db, self.bus, self.notifications)

        # --- Conexiunile PMS -------------------------------------------------------
        # Conexiunea mock rămâne mereu activă (lumea simulată); cea reală i se adaugă.
        self.mock_world = MockWorld(self.db)
        mock_pms = MockPmsAdapter(self.mock_world, self.ids)
        self.simulation: SimulationPort = mock_pms
        self.pms = PmsRegistry()
        self.pms.register(mock_pms)
        real_pms = self._build_real_pms()
        if real_pms is not None:
            self.pms.register(real_pms)
        for connection in overrides.get("pms_connections", []):
            self.pms.register(connection)

        self.channel = ChannelGateway(
            self.pms, self.world, self.conversations, self.bus, self.notifications
        )
        self.calendar = CalendarGateway(self.pms, self.world)
        self.sync = SyncService(self.pms, self.world, self.ids, self.bus)

        # --- Celelalte porturi -----------------------------------------------------
        self.llm: LLMPort = overrides.get("llm") or self._build_llm()
        self.reviews: ReviewsPort = overrides.get("reviews") or self._build_reviews()
        self.market: MarketDataPort = overrides.get("market") or self._build_market()

        self.agent_trace = AgentTrace(self.db)
        self.flows = FlowStories(self.db, self.world)
        self.agent = AgentRunner(
            self.llm,
            self.agent_trace,
            max_steps=settings.agent_max_steps,
            keep_last_rounds=settings.agent_keep_last_rounds,
            tool_output_max_chars=settings.agent_tool_output_max_chars,
            llm_timeout_s=settings.llm_timeout_s,
        )

        # --- Feature-urile (auto-discovery) ----------------------------------------
        self.routers: list[APIRouter] = []
        self._quiesce_hooks: list[Callable[[], Awaitable[None]]] = []
        self.init_schema()
        self.features: list[FeatureStatus] = load_features(
            self._make_feature_context,
            package=features_package,
            disabled=settings.disabled_features,
        )
        self.init_schema()  # include și tabelele feature-urilor
        for status in self.features:
            if not status.loaded and status.error != "disabled by configuration":
                self.notifications.notify(
                    feature=status.name,
                    level="error",
                    title=f"Feature {status.name} failed to load",
                    body=f"{status.error}\n\nThe other features keep running.",
                )

    # --- Alegerea adaptoarelor din config ------------------------------------------

    def _build_real_pms(self) -> PmsConnection | None:
        s = self.settings
        if s.pms_provider == "hospitable":
            from app.adapters.hospitable.adapter import HospitableAdapter

            assert s.hospitable_api_token is not None  # garantat de validarea configului
            return HospitableAdapter(
                base_url=s.hospitable_base_url,
                token=s.hospitable_api_token.get_secret_value(),
                ids=self.ids,
                timeout_s=s.http_timeout_s,
            )
        if s.pms_provider == "ownerrez":
            from app.adapters.ownerrez.adapter import OwnerRezAdapter

            return OwnerRezAdapter(
                base_url=s.ownerrez_api_base_url,
                access_token=_secret(s.ownerrez_access_token),
                username=s.ownerrez_username,
                personal_token=_secret(s.ownerrez_personal_token),
                user_agent=s.ownerrez_user_agent,
                ids=self.ids,
                timeout_s=s.http_timeout_s,
            )
        return None

    def _build_llm(self) -> LLMPort:
        s = self.settings
        if s.llm_provider == "openrouter":
            from app.adapters.openrouter.llm import OpenRouterLLM

            assert s.openrouter_api_key is not None and s.llm_model is not None
            return OpenRouterLLM(
                api_key=s.openrouter_api_key.get_secret_value(),
                base_url=s.openrouter_base_url,
                model=s.llm_model,
                utility_model=s.llm_model_utility,
                timeout_s=s.llm_timeout_s,
            )
        return MockLLM()

    def _build_reviews(self) -> ReviewsPort:
        s = self.settings
        if s.reviews_provider == "apify":
            from app.adapters.apify.reviews import ApifyReviewsAdapter

            assert s.apify_token is not None and s.apify_reviews_actor_id is not None
            return ApifyReviewsAdapter(
                token=s.apify_token.get_secret_value(),
                actor_id=s.apify_reviews_actor_id,
                input_template=s.apify_reviews_input_template,
                ids=self.ids,
                timeout_s=s.apify_timeout_s,
            )
        return MockReviewsAdapter(self.mock_world, self.ids)

    def _build_market(self) -> MarketDataPort:
        s = self.settings
        if s.market_provider == "inside_airbnb":
            from app.adapters.inside_airbnb.market import InsideAirbnbMarketAdapter

            assert s.inside_airbnb_listings_csv is not None
            return InsideAirbnbMarketAdapter(
                db=self.db,
                csv_path=s.inside_airbnb_listings_csv,
                city=s.market_city,
                currency=s.market_currency,
            )
        return MockMarketAdapter(self.mock_world)

    # --- Feature-uri ---------------------------------------------------------------

    def _make_feature_context(self, feature: str) -> FeatureContext:
        return FeatureContext(
            feature=feature,
            settings=self.settings,
            clock=self.clock,
            db=self.db,
            bus=self.bus,
            approvals=self.approvals,
            notifications=self.notifications,
            channel=self.channel,
            calendar=self.calendar,
            world=self.world,
            conversations=self.conversations,
            guest_topics=self.guest_topics,
            ids=self.ids,
            sync=self.sync,
            pms=self.pms,
            simulation=self.simulation,
            llm=self.llm,
            reviews=self.reviews,
            market=self.market,
            agent=self.agent,
            _add_router=self.routers.append,
            _add_mock_policy=self._add_mock_policy,
            _add_quiesce_hook=self._quiesce_hooks.append,
        )

    def _add_mock_policy(self, purpose: str, policy: MockPolicy) -> None:
        register = getattr(self.llm, "register_policy", None)
        if callable(register):  # doar LLM-ul mock are politici; pe un model real nu face nimic
            register(purpose, policy)

    def _notify_handler_failure(
        self, sub: Subscription, event: Event, status: str, error: str
    ) -> None:
        self.notifications.notify(
            feature=sub.feature,
            level="error",
            title=f"{sub.feature}: handler {status} on {event.event_type()}",
            body=f"{sub.name}: {error}\n\nOther features were not affected.",
            correlation_id=event.correlation_id,
        )

    # --- Ciclul de viață -----------------------------------------------------------

    def init_schema(self) -> None:
        """Creează tabelele lipsă: nucleu, lumea mock, adaptoare, feature-uri. Idempotent."""
        self.db.script(CORE_SCHEMA.read_text(encoding="utf-8"))
        # Coloane adăugate după prima versiune: o bază locală veche le primește fără reseed.
        self.db.ensure_column("properties", "listing_url", "TEXT")
        self.mock_world.init_schema()
        for port in (self.llm, self.reviews, self.market):  # ex. tabelul adaptorului Inside Airbnb
            init = getattr(port, "init_schema", None)
            if callable(init):
                init()
        for schema_file in feature_schema_files(self._features_package):
            self.db.script(schema_file.read_text(encoding="utf-8"))

    async def startup(self) -> None:
        self.init_schema()
        if self.settings.auto_seed and self.world.get_host() is None:
            from app.seeding import seed_world

            summary = await seed_world(self, reset=False)
            logger.info("Empty database: seeded the simulated world (%s)", summary["sync"])

    async def reset_world(self) -> dict[str, Any]:
        """Șterge tot și reface lumea din seed (folosit de `POST /sim/reset`)."""
        from app.seeding import seed_world

        # Munca lumii vechi nu are voie să scrie în cea nouă: așteptăm puțin, apoi oprim
        # ce a rămas (ex. o analiză lungă) înainte să ștergem tabelele.
        if not await self.bus.join(timeout=10):
            stopped = await self.bus.cancel_running()
            logger.warning("World reset: stopped %d handler(s) still running", stopped)
        await self._quiesce()
        await self.bus.cancel_running()  # ce au mai pornit hook-urile
        return await seed_world(self, reset=True)

    async def _quiesce(self) -> None:
        """Termină munca de fundal a feature-urilor (în afara event bus-ului)."""
        for hook in self._quiesce_hooks:
            try:
                await hook()
            except Exception:
                logger.exception("A feature quiesce hook failed")

    async def shutdown(self) -> None:
        await self._quiesce()
        await self.bus.shutdown()
        for closable in (*self.pms.all(), self.llm, self.reviews, self.market):
            close = getattr(closable, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.exception("Closing %s failed", type(closable).__name__)
        self.db.close()


def _secret(value: Any) -> str | None:
    return value.get_secret_value() if value is not None else None
