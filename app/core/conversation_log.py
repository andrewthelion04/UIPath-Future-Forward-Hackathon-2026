"""Jurnalul conversațiilor cu oaspeții (ambele direcții), citit de orice feature."""

from __future__ import annotations

from typing import Any, Literal

from app.core.clock import utcnow
from app.core.contracts.models import Message, PmsProvider
from app.core.db import Database
from app.core.ids import new_id


def _to_model(row: dict[str, Any]) -> Message:
    return Message(**row)


class ConversationLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        booking_id: str,
        direction: Literal["inbound", "outbound"],
        sender: Literal["guest", "host", "copilot"],
        body: str,
        pms_provider: PmsProvider,
        status: Literal["received", "sent", "failed"],
    ) -> Message:
        message = Message(
            id=new_id("msg"),
            booking_id=booking_id,
            direction=direction,
            sender=sender,
            body=body,
            status=status,
            pms_provider=pms_provider,
            created_at=utcnow(),
        )
        self._db.execute(
            "INSERT INTO messages (id, booking_id, direction, sender, body, status, "
            "pms_provider, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                booking_id,
                direction,
                sender,
                body,
                status,
                pms_provider.value,
                message.created_at.isoformat(),
            ),
        )
        return message

    def history(self, booking_id: str, limit: int = 50) -> list[Message]:
        """Ultimele `limit` mesaje ale rezervării, în ordine cronologică."""
        rows = self._db.query(
            "SELECT * FROM (SELECT *, rowid AS _rowid FROM messages WHERE booking_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, _rowid",
            (booking_id, limit),
        )
        for row in rows:
            row.pop("_rowid", None)
        return [_to_model(row) for row in rows]
