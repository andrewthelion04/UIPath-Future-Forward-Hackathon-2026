"""Pipeline-ul de intrare al mesajelor: salvează brut, filtrează, deduplică, mapează, publică.

Doar transport, fără logică de business. Același drum îl parcurg webhook-urile unui PMS real
și mesajele din simulator, deci produc același eveniment (`GuestMessageReceived`).
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.core.clock import utcnow
from app.core.contracts.events import GuestMessageReceived
from app.core.contracts.models import Booking
from app.core.db import from_json, to_json
from app.core.feature_context import FeatureContext
from app.core.id_mapper import RESERVATION
from app.ports.pms import InboundMessage

logger = logging.getLogger("staypilot.f1")


class InboundOutcome(StrEnum):
    PUBLISHED = "published"
    SYNCED = "synced"
    IGNORED_NOT_MESSAGE = "ignored_not_message"  # alt tip de payload
    IGNORED_NOT_GUEST = "ignored_not_guest"  # filtrul anti-buclă
    DUPLICATE = "duplicate"
    UNMAPPED = "unmapped"
    ERROR = "error"


class InboundResult(BaseModel):
    outcome: InboundOutcome
    detail: str = ""
    event_id: str | None = None
    correlation_id: str | None = None
    message_id: str | None = None


class InboundPipeline:
    def __init__(self, ctx: FeatureContext) -> None:
        self._ctx = ctx
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._reprocess_locks: dict[str, asyncio.Lock] = {}
        self._reprocess_tasks: dict[str, asyncio.Task[None]] = {}
        self._reprocess_dirty: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def sync_lock(self, provider: str) -> asyncio.Lock:
        """Același lock pentru sincronizarea manuală și cea pornită de webhook."""
        self._loop = asyncio.get_running_loop()
        return self._sync_locks.setdefault(provider, asyncio.Lock())

    def schedule_reprocess(self, provider: str) -> None:
        """Punte pentru `IdMapper.link`, al cărui contract rămâne sincron."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self.schedule_reprocess, provider)
            else:
                async def replay_and_drain() -> None:
                    await self.reprocess_unmapped(provider)
                    await self._ctx.bus.join(timeout=self._ctx.settings.sim_wait_timeout_s)

                asyncio.run(replay_and_drain())
            return
        # Mai multe mapări din același request se reunesc într-o singură reprocesare.
        existing = self._reprocess_tasks.get(provider)
        if existing is not None and not existing.done():
            self._reprocess_dirty.add(provider)
            return

        async def replay() -> None:
            try:
                while True:
                    self._reprocess_dirty.discard(provider)
                    await self.reprocess_unmapped(provider)
                    if provider not in self._reprocess_dirty:
                        break
            except Exception:
                logger.exception("Scheduled message replay failed for %s", provider)
                self._ctx.notify(
                    "Unmapped message replay failed",
                    f"Messages on '{provider}' remain in the inbound log. Retry replay after "
                    "checking the connection.",
                    level="error",
                )

        task = loop.create_task(replay(), name=f"f1-reprocess:{provider}")
        self._reprocess_tasks[provider] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._reprocess_tasks.get(provider) is done:
                self._reprocess_tasks.pop(provider, None)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Scheduled replay task failed for %s", provider, exc_info=done.exception())

        task.add_done_callback(finished)

    async def close(self) -> None:
        """Terminăm reprocesările programate înainte ca aplicația să închidă baza."""
        if self._reprocess_tasks:
            await asyncio.gather(*list(self._reprocess_tasks.values()), return_exceptions=True)

    async def process(self, provider: str, payload: Any, *, source: str) -> InboundResult:
        """Procesează un payload brut. Nu ridică excepții: rezultatul spune ce s-a întâmplat."""
        self._loop = asyncio.get_running_loop()
        # 1. Salvăm payload-ul brut înainte de orice, ca să nu se piardă nimic.
        log_id = self._ctx.db.execute(
            "INSERT INTO f1_webhook_log (provider, source, payload, received_at) VALUES (?, ?, ?, ?)",
            (provider, source, to_json(payload), utcnow().isoformat()),
        ).lastrowid
        external_message_id: str | None = None
        try:
            result, external_message_id = await self._process(provider, payload, source)
        except Exception as exc:
            logger.exception("Inbound pipeline failed for provider %s", provider)
            self._ctx.notify(
                "PMS webhook processing failed",
                f"The payload from '{provider}' was logged. Check the connection and retry.",
                level="error",
            )
            result = InboundResult(
                outcome=InboundOutcome.ERROR, detail=f"{type(exc).__name__}: {exc}"
            )
        self._ctx.db.execute(
            "UPDATE f1_webhook_log SET outcome = ?, detail = ?, external_message_id = ?, "
            "event_id = ? WHERE id = ?",
            (result.outcome.value, result.detail, external_message_id, result.event_id, log_id),
        )
        return result

    async def _process(
        self, provider: str, payload: Any, source: str
    ) -> tuple[InboundResult, str | None]:
        ctx = self._ctx
        connection = ctx.pms.get(provider)
        if connection is None:
            detail = f"PMS connection '{provider}' is not enabled"
            return InboundResult(outcome=InboundOutcome.ERROR, detail=detail), None
        if not isinstance(payload, dict):
            return (
                InboundResult(
                    outcome=InboundOutcome.IGNORED_NOT_MESSAGE, detail="payload is not a JSON object"
                ),
                None,
            )

        # 2. Adaptorul conexiunii înțelege forma payload-ului.
        inbound = connection.parse_webhook(payload)
        if inbound is None:
            is_reservation = getattr(connection, "is_reservation_webhook", None)
            if callable(is_reservation) and is_reservation(payload):
                # Wrapperul instalat de F1 ține lockul per provider și reprocesează
                # mesajele numai după sincronizarea reușită, inclusiv la apeluri manuale.
                await ctx.sync.sync(provider)
                return (
                    InboundResult(
                        outcome=InboundOutcome.SYNCED, detail="reservation sync completed"
                    ),
                    None,
                )
            return (
                InboundResult(
                    outcome=InboundOutcome.IGNORED_NOT_MESSAGE, detail="not a message event"
                ),
                None,
            )
        message_key = inbound.external_message_id

        # 3. Filtrul anti-buclă: PMS-ul ne trimite și mesajele trimise de noi.
        if inbound.sender_type != "guest":
            detail = f"sender_type={inbound.sender_type or 'unknown'}"
            return InboundResult(outcome=InboundOutcome.IGNORED_NOT_GUEST, detail=detail), message_key

        # 4. Deduplicare după ID-ul mesajului (PMS-ul retrimite la orice răspuns non-200).
        cursor = ctx.db.execute(
            "INSERT OR IGNORE INTO f1_seen_messages (provider, external_message_id, first_seen_at) "
            "VALUES (?, ?, ?)",
            (provider, message_key, utcnow().isoformat()),
        )
        if cursor.rowcount == 0:
            return InboundResult(outcome=InboundOutcome.DUPLICATE), message_key

        # 5. Maparea rezervării externe la ID-ul intern.
        booking_id = (
            ctx.ids.to_internal(provider, RESERVATION, inbound.external_reservation_id)
            if inbound.external_reservation_id
            else None
        )
        booking = ctx.world.get_booking(booking_id) if booking_id else None
        if booking is None:
            ctx.notify(
                "Guest message on an unmapped reservation",
                f"A message arrived through '{provider}' for a reservation StayPilot does not "
                "know. It was logged but not processed. Sync this PMS connection or map the "
                f"reservation manually.\n\nMessage: {inbound.body[:500]}",
                level="warning",
            )
            return (
                InboundResult(
                    outcome=InboundOutcome.UNMAPPED, detail="reservation is not mapped"
                ),
                message_key,
            )

        # 6. Publicăm evenimentul intern.
        return await self._publish(booking, inbound, source), message_key

    async def _publish(
        self, booking: Booking, inbound: InboundMessage, source: str
    ) -> InboundResult:
        ctx = self._ctx
        message = ctx.conversations.record(
            booking_id=booking.id,
            direction="inbound",
            sender="guest",
            body=inbound.body,
            pms_provider=booking.pms_provider,
            status="received",
        )
        event = await ctx.bus.publish(
            GuestMessageReceived(
                message_id=message.id,
                booking_id=booking.id,
                property_id=booking.property_id,
                text=inbound.body,
                pms_provider=booking.pms_provider,
                source_channel=booking.source_channel,
                source=source,
            )
        )
        return InboundResult(
            outcome=InboundOutcome.PUBLISHED,
            event_id=event.event_id,
            correlation_id=event.correlation_id,
            message_id=message.id,
        )

    async def reprocess_unmapped(self, provider: str | None = None) -> dict[str, Any]:
        """Reîncearcă doar maparea mesajelor deja deduplicate, în ordinea primirii."""
        self._loop = asyncio.get_running_loop()
        ctx = self._ctx
        providers = (
            [provider]
            if provider is not None
            else [row["provider"] for row in ctx.db.query(
                "SELECT DISTINCT provider FROM f1_webhook_log WHERE outcome = 'unmapped' "
                "ORDER BY provider"
            )]
        )
        published = 0
        for name in providers:
            lock = self._reprocess_locks.setdefault(name, asyncio.Lock())
            async with lock:
                connection = ctx.pms.get(name)
                if connection is None:
                    continue
                rows = ctx.db.query(
                    "SELECT * FROM f1_webhook_log WHERE provider = ? AND outcome = 'unmapped' "
                    "ORDER BY id ASC", (name,),
                )
                for row in rows:
                    try:
                        inbound = connection.parse_webhook(from_json(row["payload"]))
                        if inbound is None or inbound.sender_type != "guest":
                            continue
                        booking_id = (
                            ctx.ids.to_internal(name, RESERVATION, inbound.external_reservation_id)
                            if inbound.external_reservation_id else None
                        )
                        booking = ctx.world.get_booking(booking_id) if booking_id else None
                        if booking is None:
                            continue
                        # Nu repetăm inserarea în f1_seen_messages: cheia există deja.
                        result = await self._publish(booking, inbound, row["source"])
                        ctx.db.execute(
                            "UPDATE f1_webhook_log SET outcome = 'published', "
                            "detail = 'reprocessed after sync', event_id = ? "
                            "WHERE id = ? AND outcome = 'unmapped'",
                            (result.event_id, row["id"]),
                        )
                        published += 1
                    except Exception as exc:
                        logger.exception("Unmapped message replay failed for log %s", row["id"])
                        ctx.db.execute(
                            "UPDATE f1_webhook_log SET detail = ? WHERE id = ? "
                            "AND outcome = 'unmapped'",
                            (f"reprocess failed: {type(exc).__name__}", row["id"]),
                        )
                        ctx.notify(
                            "Unmapped message replay failed",
                            f"Inbound log entry {row['id']} on '{name}' is still pending. "
                            "Check the connection and retry replay.",
                            level="error",
                        )
        where, params = (" AND provider = ?", (provider,)) if provider is not None else ("", ())
        remaining = ctx.db.query_one(
            "SELECT COUNT(*) AS total FROM f1_webhook_log WHERE outcome = 'unmapped'" + where,
            params,
        )
        return {"provider": provider, "reprocessed": published, "remaining": remaining["total"]}

    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._ctx.db.query(
            "SELECT * FROM f1_webhook_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["payload"] = from_json(row["payload"])
        return rows
