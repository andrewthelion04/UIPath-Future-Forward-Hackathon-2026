"""Event bus în proces, cu handleri izolați.

- `publish()` persistă evenimentul și pornește câte un task per handler, fără să aștepte.
- Fiecare handler rulează cu timeout și `try/except`: o eroare sau un timeout se
  înregistrează și se raportează, dar nu atinge ceilalți handleri.
- `correlation_id` se propagă automat de la evenimentul în curs la cele publicate din handler.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.core.clock import utcnow
from app.core.contracts.events import Event
from app.core.db import Database, from_json, to_json

logger = logging.getLogger("staypilot.bus")

Handler = Callable[[Any], Awaitable[None]]
Predicate = Callable[[Any], bool]

_current_event: ContextVar[Event | None] = ContextVar("staypilot_current_event", default=None)


def current_correlation_id() -> str | None:
    """Corelarea fluxului în curs (None în afara unui handler)."""
    event = _current_event.get()
    return event.correlation_id if event else None


@dataclass(frozen=True)
class Subscription:
    event_cls: type[Event]
    handler: Handler
    name: str
    feature: str
    timeout_s: float
    predicate: Predicate | None = None


FailureHook = Callable[[Subscription, Event, str, str], None]


class EventBus:
    def __init__(self, db: Database, *, default_timeout_s: float = 180.0) -> None:
        self._db = db
        self._default_timeout_s = default_timeout_s
        self._subs: dict[type[Event], list[Subscription]] = defaultdict(list)
        self._tasks: set[asyncio.Task[None]] = set()
        self.failure_hook: FailureHook | None = None  # setat de container: notifică hostul

    # --- Abonare -------------------------------------------------------------------

    def subscribe(
        self,
        event_cls: type[Event],
        handler: Handler,
        *,
        name: str | None = None,
        feature: str = "core",
        timeout_s: float | None = None,
        predicate: Predicate | None = None,
    ) -> Subscription:
        sub = Subscription(
            event_cls=event_cls,
            handler=handler,
            name=name or f"{feature}.{getattr(handler, '__name__', 'handler')}",
            feature=feature,
            timeout_s=timeout_s or self._default_timeout_s,
            predicate=predicate,
        )
        self._subs[event_cls].append(sub)
        return sub

    def subscriptions(self) -> list[Subscription]:
        return [sub for subs in self._subs.values() for sub in subs]

    # --- Publicare -----------------------------------------------------------------

    async def publish(self, event: Event) -> Event:
        parent = _current_event.get()
        update: dict[str, Any] = {}
        if parent is not None and event.causation_id is None:
            update["causation_id"] = parent.event_id
        if event.correlation_id is None:
            inherited = parent.correlation_id if parent is not None else None
            update["correlation_id"] = inherited or event.event_id
        if update:
            event = event.model_copy(update=update)

        self._persist(event)
        loop = asyncio.get_running_loop()
        for sub in list(self._subs.get(type(event), ())):
            if not self._matches(sub, event):
                continue
            task = loop.create_task(self._run(sub, event), name=f"{sub.name}:{event.event_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return event

    async def join(self, timeout: float | None = None) -> bool:
        """Așteaptă toți handlerii în curs, inclusiv cei porniți în lanț. False la timeout.

        Nu se apelează din interiorul unui handler (s-ar aștepta pe sine).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._tasks:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(set(self._tasks), timeout=remaining)
        return True

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- Citire (UI de depanare) ---------------------------------------------------

    def list_events(self, limit: int = 100, correlation_id: str | None = None) -> list[dict]:
        if correlation_id:
            rows = self._db.query(
                "SELECT * FROM events WHERE correlation_id = ? "
                "ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                (correlation_id, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM events ORDER BY occurred_at DESC, rowid DESC LIMIT ?", (limit,)
            )
        events = []
        for row in rows:
            row["payload"] = from_json(row["payload"], {})
            row["handlers"] = self._db.query(
                "SELECT handler, feature, status, error, started_at, duration_ms "
                "FROM event_handler_runs WHERE event_id = ? ORDER BY id",
                (row["id"],),
            )
            events.append(row)
        return events

    # --- Intern --------------------------------------------------------------------

    @staticmethod
    def _matches(sub: Subscription, event: Event) -> bool:
        if sub.predicate is None:
            return True
        try:
            return bool(sub.predicate(event))
        except Exception:
            logger.exception("Predicate of %s failed; handler skipped", sub.name)
            return False

    def _persist(self, event: Event) -> None:
        payload = event.model_dump(
            mode="json",
            exclude={"event_id", "occurred_at", "correlation_id", "causation_id", "source"},
        )
        self._db.execute(
            "INSERT INTO events (id, type, payload, source, correlation_id, causation_id, "
            "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.event_type(),
                to_json(payload),
                event.source,
                event.correlation_id,
                event.causation_id,
                event.occurred_at.isoformat(),
            ),
        )

    async def _run(self, sub: Subscription, event: Event) -> None:
        _current_event.set(event)  # vizibil doar în contextul acestui task
        run_id = self._db.execute(
            "INSERT INTO event_handler_runs (event_id, handler, feature, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (event.event_id, sub.name, sub.feature, utcnow().isoformat()),
        ).lastrowid
        started = time.perf_counter()
        status, error = "ok", None
        try:
            await asyncio.wait_for(sub.handler(event), timeout=sub.timeout_s)
        except TimeoutError:
            status, error = "timeout", f"handler exceeded {sub.timeout_s:g}s"
        except asyncio.CancelledError:
            self._finish(run_id, "cancelled", "cancelled at shutdown", started)
            raise
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
            logger.exception("Handler %s failed on %s", sub.name, event.event_type())
        self._finish(run_id, status, error, started)
        if status != "ok" and self.failure_hook is not None:
            try:
                self.failure_hook(sub, event, status, error or "")
            except Exception:
                logger.exception("Failure hook raised for %s", sub.name)

    def _finish(self, run_id: int | None, status: str, error: str | None, started: float) -> None:
        self._db.execute(
            "UPDATE event_handler_runs SET status = ?, error = ?, duration_ms = ? WHERE id = ?",
            (status, error, int((time.perf_counter() - started) * 1000), run_id),
        )
