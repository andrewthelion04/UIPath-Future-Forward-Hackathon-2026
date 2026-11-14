"""Tot ce primește un feature prin dependency injection.

Un feature vede doar servicii din core și interfețe (porturi), niciodată adaptoare concrete.
Înregistrările (abonări, routere, politici mock) se aplică abia după ce `register(ctx)` a
reușit, deci un feature care cade la încărcare nu lasă nimic pe jumătate înregistrat.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from fastapi import APIRouter
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings
from app.core.agent.loop import AgentRunner
from app.core.approvals import ApprovalService
from app.core.channel_gateway import ChannelGateway
from app.core.clock import Clock
from app.core.contracts.events import Event, GuestOfferAnswered
from app.core.contracts.models import Notification
from app.core.conversation_log import ConversationLog
from app.core.db import Database
from app.core.event_bus import EventBus, Handler
from app.core.guest_topics import GuestTopics
from app.core.id_mapper import IdMapper
from app.core.notifications import NotificationService
from app.core.sync_service import SyncService
from app.core.world_repo import WorldRepository
from app.core.host_answers import HostAnswers
from app.ports.calendar import CalendarPort
from app.ports.llm import LLMPort, MockPolicy
from app.ports.market import MarketDataPort
from app.ports.pms import PmsRegistry
from app.ports.reviews import ReviewsPort
from app.ports.simulation import SimulationPort


class FeatureSettings(BaseSettings):
    """Baza setărilor proprii ale unui feature.

    Subclasa își alege prefixul, ex. `model_config = SettingsConfigDict(env_prefix="F5_")`,
    și se încarcă prin `ctx.load_settings(...)`. Valorile vin din același `.env`.
    """

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)


S = TypeVar("S", bound=BaseSettings)


@dataclass
class FeatureContext:
    feature: str
    settings: Settings
    clock: Clock
    db: Database
    bus: EventBus
    approvals: ApprovalService
    notifications: NotificationService
    channel: ChannelGateway
    host_answers: HostAnswers
    calendar: CalendarPort
    world: WorldRepository
    conversations: ConversationLog
    guest_topics: GuestTopics
    ids: IdMapper
    sync: SyncService
    pms: PmsRegistry
    simulation: SimulationPort
    llm: LLMPort
    reviews: ReviewsPort
    market: MarketDataPort
    agent: AgentRunner
    _add_router: Callable[[APIRouter], None]
    _add_mock_policy: Callable[[str, MockPolicy], None]
    _add_quiesce_hook: Callable[[Callable[[], Awaitable[None]]], None] = lambda hook: None
    _pending: list[Callable[[], None]] = field(default_factory=list)

    # --- Înregistrări (aplicate de loader după un `register` reușit) ---------------

    def subscribe(
        self, event_cls: type[Event], handler: Handler, *, timeout_s: float | None = None
    ) -> None:
        """Abonează un handler la un tip de eveniment."""
        name = f"{self.feature}.{getattr(handler, '__name__', 'handler')}"
        self._pending.append(
            lambda: self.bus.subscribe(
                event_cls, handler, name=name, feature=self.feature, timeout_s=timeout_s
            )
        )

    def on_decision(self, kind: str, handler: Handler, *, timeout_s: float | None = None) -> None:
        """Primește deciziile hostului pentru aprobările acestui feature de tipul `kind`."""
        self._pending.append(
            lambda: self.approvals.on_decision(self.feature, kind, handler, timeout_s=timeout_s)
        )

    def on_guest_offer_answered(self, handler: Handler, *, timeout_s: float | None = None) -> None:
        """Primește răspunsurile oaspeților la ofertele publicate de acest feature."""
        name = f"{self.feature}.{getattr(handler, '__name__', 'handler')}"
        self._pending.append(
            lambda: self.bus.subscribe(
                GuestOfferAnswered,
                handler,
                name=name,
                feature=self.feature,
                timeout_s=timeout_s,
                predicate=lambda event: event.feature == self.feature,
            )
        )

    def include_router(self, router: APIRouter) -> None:
        """Adaugă endpointurile proprii ale feature-ului în API."""
        self._pending.append(lambda: self._add_router(router))

    def register_mock_llm_policy(self, purpose: str, policy: MockPolicy) -> None:
        """Comportamentul determinist al LLM-ului mock pentru agentul `purpose`."""
        self._pending.append(lambda: self._add_mock_policy(purpose, policy))

    def on_quiesce(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Munca de fundal a feature-ului (sarcini `asyncio` proprii, în afara event bus-ului).

        Hook-ul e apelat înainte ca baza să fie închisă (shutdown) sau recreată (resetul
        lumii) și trebuie să își termine ori să își oprească sarcinile. Poate fi apelat de
        mai multe ori.
        """
        self._pending.append(lambda: self._add_quiesce_hook(hook))

    def commit(self) -> None:
        pending, self._pending = self._pending, []
        for apply in pending:
            apply()

    # --- Ajutoare ------------------------------------------------------------------

    def load_settings(self, settings_cls: type[S]) -> S:
        """Încarcă setările proprii ale feature-ului din același `.env` ca restul aplicației."""
        return settings_cls(_env_file=self.settings.env_file_used)  # type: ignore[call-arg]

    def notify(self, title: str, body: str = "", **kwargs: Any) -> Notification:
        """Notificare către host în numele acestui feature."""
        return self.notifications.notify(feature=self.feature, title=title, body=body, **kwargs)
