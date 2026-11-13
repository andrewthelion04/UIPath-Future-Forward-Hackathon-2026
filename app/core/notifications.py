"""Mecanismul unic de notificări către host, folosit de toate feature-urile."""

from __future__ import annotations

from typing import Any

from app.core.clock import utcnow
from app.core.contracts.models import Notification, NotificationLevel
from app.core.db import Database
from app.core.event_bus import current_correlation_id
from app.core.ids import new_id


def _to_model(row: dict[str, Any]) -> Notification:
    return Notification(
        id=row["id"],
        feature=row["feature"],
        level=row["level"],
        title=row["title"],
        body=row["body"],
        booking_id=row["booking_id"],
        property_id=row["property_id"],
        approval_id=row["approval_id"],
        correlation_id=row["correlation_id"],
        read=row["read_at"] is not None,
        created_at=row["created_at"],
    )


class NotificationService:
    def __init__(self, db: Database) -> None:
        self._db = db

    def notify(
        self,
        *,
        feature: str,
        title: str,
        body: str = "",
        level: NotificationLevel = "info",
        booking_id: str | None = None,
        property_id: str | None = None,
        approval_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Notification:
        notification = Notification(
            id=new_id("ntf"),
            feature=feature,
            level=level,
            title=title,
            body=body,
            booking_id=booking_id,
            property_id=property_id,
            approval_id=approval_id,
            correlation_id=correlation_id or current_correlation_id(),
            created_at=utcnow(),
        )
        self._db.execute(
            "INSERT INTO notifications (id, feature, level, title, body, booking_id, "
            "property_id, approval_id, correlation_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notification.id,
                notification.feature,
                notification.level,
                notification.title,
                notification.body,
                notification.booking_id,
                notification.property_id,
                notification.approval_id,
                notification.correlation_id,
                notification.created_at.isoformat(),
            ),
        )
        return notification

    def list(self, *, unread_only: bool = False, limit: int = 200) -> list[Notification]:
        where = "WHERE read_at IS NULL" if unread_only else ""
        rows = self._db.query(
            f"SELECT * FROM notifications {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return [_to_model(row) for row in rows]

    def mark_read(self, notification_id: str) -> Notification | None:
        self._db.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ?",
            (utcnow().isoformat(), notification_id),
        )
        row = self._db.query_one("SELECT * FROM notifications WHERE id = ?", (notification_id,))
        return _to_model(row) if row else None

    def mark_all_read(self) -> int:
        cursor = self._db.execute(
            "UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (utcnow().isoformat(),)
        )
        return cursor.rowcount
