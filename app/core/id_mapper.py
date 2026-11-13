"""Tabela de corespondențe între ID-urile externe și cele interne.

Cheia unei corespondențe este (conexiune, tip de entitate, ID extern). În restul aplicației
circulă doar ID-ul intern.
"""

from __future__ import annotations

from typing import Any

from app.core.clock import utcnow
from app.core.db import Database
from app.core.ids import stable_id

# Tipuri de entități folosite în schelet.
PROPERTY = "property"
RESERVATION = "reservation"
LISTING = "listing"


class MappingConflict(RuntimeError):
    """ID-ul extern e deja legat de alt ID intern."""


class IdMapper:
    def __init__(self, db: Database) -> None:
        self._db = db

    def to_internal(self, connection: str, entity_type: str, external_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT internal_id FROM id_map "
            "WHERE connection = ? AND entity_type = ? AND external_id = ?",
            (connection, entity_type, str(external_id)),
        )
        return row["internal_id"] if row else None

    def to_external(self, connection: str, entity_type: str, internal_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT external_id FROM id_map "
            "WHERE connection = ? AND entity_type = ? AND internal_id = ?",
            (connection, entity_type, internal_id),
        )
        return row["external_id"] if row else None

    def link(self, connection: str, entity_type: str, external_id: str, internal_id: str) -> None:
        """Salvează corespondența. Repetarea aceleiași legături nu schimbă nimic."""
        existing = self.to_internal(connection, entity_type, external_id)
        if existing == internal_id:
            return
        if existing is not None:
            raise MappingConflict(
                f"{connection}/{entity_type}/{external_id} is already mapped to {existing}"
            )
        self._db.execute(
            "INSERT INTO id_map (connection, entity_type, external_id, internal_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (connection, entity_type, str(external_id), internal_id, utcnow().isoformat()),
        )

    def get_or_create(
        self, connection: str, entity_type: str, external_id: str, *, prefix: str
    ) -> tuple[str, bool]:
        """ID-ul intern pentru o entitate externă; îl creează (determinist) la prima vedere."""
        existing = self.to_internal(connection, entity_type, external_id)
        if existing is not None:
            return existing, False
        internal_id = stable_id(prefix, connection, entity_type, str(external_id))
        self.link(connection, entity_type, external_id, internal_id)
        return internal_id, True

    def unlink(self, connection: str, entity_type: str, external_id: str) -> bool:
        cursor = self._db.execute(
            "DELETE FROM id_map WHERE connection = ? AND entity_type = ? AND external_id = ?",
            (connection, entity_type, str(external_id)),
        )
        return cursor.rowcount > 0

    def list(self, connection: str | None = None) -> list[dict[str, Any]]:
        if connection:
            return self._db.query(
                "SELECT * FROM id_map WHERE connection = ? ORDER BY entity_type, external_id",
                (connection,),
            )
        return self._db.query("SELECT * FROM id_map ORDER BY connection, entity_type, external_id")
