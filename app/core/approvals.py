"""Mecanismul unic de aprobări (human-in-the-loop), folosit de toate feature-urile.

Garanții:
- `request()` e idempotent prin `dedupe_key`;
- `decide()` face tranziția atomic (`UPDATE ... WHERE status = 'pending'`), deci decizia
  se publică o singură dată chiar dacă hostul apasă de două ori;
- decizia ajunge doar la feature-ul care a cerut aprobarea (`on_decision`).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from app.core.clock import utcnow
from app.core.contracts.events import ApprovalDecided, ApprovalRequested
from app.core.contracts.models import Approval, ApprovalOption
from app.core.db import Database, from_json, to_json
from app.core.event_bus import EventBus, Handler, current_correlation_id
from app.core.ids import new_id
from app.core.notifications import NotificationService

DEFAULT_OPTION = ApprovalOption(key="approve", label="Approve")


class ApprovalNotFound(LookupError):
    pass


class ApprovalConflict(RuntimeError):
    """Aprobarea a fost deja decisă altfel."""


class InvalidDecision(ValueError):
    pass


def _to_model(row: dict[str, Any]) -> Approval:
    return Approval(
        id=row["id"],
        feature=row["feature"],
        kind=row["kind"],
        title=row["title"],
        summary=row["summary"],
        options=[ApprovalOption(**opt) for opt in from_json(row["options"], [])],
        payload=from_json(row["payload"], {}),
        status=row["status"],
        chosen_option=row["chosen_option"],
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
        booking_id=row["booking_id"],
        property_id=row["property_id"],
        correlation_id=row["correlation_id"],
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


class ApprovalService:
    def __init__(self, db: Database, bus: EventBus, notifications: NotificationService) -> None:
        self._db = db
        self._bus = bus
        self._notifications = notifications

    # --- Cerere --------------------------------------------------------------------

    async def request(
        self,
        *,
        feature: str,
        kind: str,
        title: str,
        summary: str = "",
        options: list[ApprovalOption] | None = None,
        payload: dict[str, Any] | None = None,
        booking_id: str | None = None,
        property_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> Approval:
        if dedupe_key:
            existing = self._by_dedupe_key(dedupe_key)
            if existing:
                return existing
        approval = Approval(
            id=new_id("apr"),
            feature=feature,
            kind=kind,
            title=title,
            summary=summary,
            options=options or [DEFAULT_OPTION],
            payload=payload or {},
            booking_id=booking_id,
            property_id=property_id,
            correlation_id=current_correlation_id(),
            dedupe_key=dedupe_key,
            created_at=utcnow(),
        )
        try:
            self._db.execute(
                "INSERT INTO approvals (id, feature, kind, title, summary, options, payload, "
                "status, booking_id, property_id, correlation_id, dedupe_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (
                    approval.id,
                    feature,
                    kind,
                    title,
                    summary,
                    to_json([opt.model_dump() for opt in approval.options]),
                    to_json(approval.payload),
                    booking_id,
                    property_id,
                    approval.correlation_id,
                    dedupe_key,
                    approval.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            existing = self._by_dedupe_key(dedupe_key) if dedupe_key else None
            if existing:
                return existing
            raise
        self._notifications.notify(
            feature=feature,
            level="action_required",
            title=f"Approval needed: {title}",
            body=summary,
            booking_id=booking_id,
            property_id=property_id,
            approval_id=approval.id,
            correlation_id=approval.correlation_id,
        )
        await self._bus.publish(
            ApprovalRequested(
                approval_id=approval.id,
                feature=feature,
                kind=kind,
                title=title,
                correlation_id=approval.correlation_id,
                source=f"feature:{feature}",
            )
        )
        return approval

    # --- Decizie -------------------------------------------------------------------

    async def decide(
        self,
        approval_id: str,
        *,
        decision: Literal["approve", "reject"],
        option: str | None = None,
        note: str | None = None,
        decided_by: str = "host",
    ) -> tuple[Approval, bool]:
        """Aplică decizia. Întoarce `(aprobare, applied)`; `applied=False` la o repetare."""
        approval = self.get(approval_id)
        if decision == "approve":
            keys = [opt.key for opt in approval.options]
            if option is None:
                if len(keys) != 1:
                    raise InvalidDecision(f"choose one option: {', '.join(keys)}")
                option = keys[0]
            elif option not in keys:
                raise InvalidDecision(f"unknown option '{option}'; valid: {', '.join(keys)}")
            status = "approved"
        else:
            option, status = None, "rejected"

        cursor = self._db.execute(
            "UPDATE approvals SET status = ?, chosen_option = ?, decided_by = ?, "
            "decision_note = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (status, option, decided_by, note, utcnow().isoformat(), approval_id),
        )
        current = self.get(approval_id)
        if cursor.rowcount == 0:
            if current.status == status and current.chosen_option == option:
                return current, False  # aceeași decizie repetată: nu se republică
            raise ApprovalConflict(
                f"approval {approval_id} was already {current.status}"
                + (f" ({current.chosen_option})" if current.chosen_option else "")
            )
        await self._bus.publish(
            ApprovalDecided(
                approval_id=current.id,
                feature=current.feature,
                kind=current.kind,
                decision=status,
                option=option,
                note=note,
                decided_by=decided_by,
                payload=current.payload,
                correlation_id=current.correlation_id,
                source="host",
            )
        )
        return current, True

    def on_decision(
        self, feature: str, kind: str, handler: Handler, *, timeout_s: float | None = None
    ) -> None:
        """Abonează feature-ul la deciziile pentru aprobările lui de tipul `kind`."""
        self._bus.subscribe(
            ApprovalDecided,
            handler,
            name=f"{feature}.on_decision[{kind}]",
            feature=feature,
            timeout_s=timeout_s,
            predicate=lambda event: event.feature == feature and event.kind == kind,
        )

    # --- Citire --------------------------------------------------------------------

    def get(self, approval_id: str) -> Approval:
        row = self._db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            raise ApprovalNotFound(approval_id)
        return _to_model(row)

    def list(self, *, status: str | None = "pending", limit: int = 200) -> list[Approval]:
        if status in (None, "all"):
            rows = self._db.query(
                "SELECT * FROM approvals ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._db.query(
                "SELECT * FROM approvals WHERE status = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (status, limit),
            )
        return [_to_model(row) for row in rows]

    def _by_dedupe_key(self, dedupe_key: str) -> Approval | None:
        row = self._db.query_one("SELECT * FROM approvals WHERE dedupe_key = ?", (dedupe_key,))
        return _to_model(row) if row else None
