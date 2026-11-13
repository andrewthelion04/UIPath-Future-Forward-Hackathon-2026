"""Pipeline-ul de intrare al mesajelor: salvează brut, filtrează, deduplică, mapează, publică.

Doar transport, fără logică de business. Același drum îl parcurg webhook-urile unui PMS real
și mesajele din simulator, deci produc același eveniment (`GuestMessageReceived`).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from app.core.clock import utcnow
from app.core.contracts.events import GuestMessageReceived
from app.core.db import from_json, to_json
from app.core.feature_context import FeatureContext
from app.core.id_mapper import RESERVATION

logger = logging.getLogger("staypilot.f1")


class InboundOutcome(StrEnum):
    PUBLISHED = "published"
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

    async def process(self, provider: str, payload: Any, *, source: str) -> InboundResult:
        """Procesează un payload brut. Nu ridică excepții: rezultatul spune ce s-a întâmplat."""
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
        return (
            InboundResult(
                outcome=InboundOutcome.PUBLISHED,
                event_id=event.event_id,
                correlation_id=event.correlation_id,
                message_id=message.id,
            ),
            message_key,
        )

    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._ctx.db.query(
            "SELECT * FROM f1_webhook_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["payload"] = from_json(row["payload"])
        return rows
