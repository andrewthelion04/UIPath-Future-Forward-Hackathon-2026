"""Accesul la tabelele F2. Nimic din afara feature-ului nu citește aceste tabele."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.clock import utcnow
from app.core.db import Database, from_json, to_json
from app.core.ids import new_id
from app.features.f2_incidents.models import (
    CLOSED_STATUSES,
    GuestDrafts,
    Incident,
    IncidentReport,
    IncidentView,
    StaffMessage,
    StaffTask,
)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def ensure_columns(db: Database) -> None:
    """Adaugă coloanele noi din `schema.sql` pe o bază creată de o versiune mai veche.

    `CREATE TABLE IF NOT EXISTS` nu modifică un tabel existent; fără asta, o bază de date
    locală veche ar cere `python manage.py seed` după actualizare.
    """
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
        existing = {row["name"] for row in db.query(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # tabelul nu există încă; îl creează `init_schema`
        for line in body.splitlines():
            line = line.split("--")[0].strip().rstrip(",")
            if not line:
                continue
            name, definition = line.split(None, 1)
            if name in existing or "PRIMARY KEY" in definition or "UNIQUE" in definition:
                continue
            definition = definition.replace("NOT NULL", "") if "DEFAULT" not in definition else definition
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _now() -> str:
    return utcnow().isoformat()


def _incident(row: dict[str, Any]) -> Incident:
    data = dict(row)
    drafts = from_json(data.pop("drafts", None), None)
    data["drafts"] = GuestDrafts(**drafts) if drafts else None
    data["in_house"] = None if data.get("in_house") is None else bool(data["in_house"])
    return Incident(**data)


class IncidentRepository:
    def __init__(self, db: Database) -> None:
        self._db = db
        # Apelat după fiecare scriere a unui incident (serviciul își ține la zi starea publică).
        self.on_change: Callable[[Incident], None] | None = None

    def _changed(self, incident: Incident) -> Incident:
        if self.on_change is not None:
            self.on_change(incident)
        return incident

    # --- Incidente ---------------------------------------------------------------------

    def list(self) -> list[Incident]:
        rows = self._db.query("SELECT * FROM f2_incidents ORDER BY created_at DESC, rowid DESC")
        return [_incident(row) for row in rows]

    def get(self, incident_id: str) -> Incident | None:
        row = self._db.query_one("SELECT * FROM f2_incidents WHERE id = ?", (incident_id,))
        return _incident(row) if row else None

    def open_for_property(self, property_id: str) -> list[Incident]:
        placeholders = ", ".join("?" for _ in CLOSED_STATUSES)
        rows = self._db.query(
            f"SELECT * FROM f2_incidents WHERE property_id = ? AND status NOT IN ({placeholders}) "
            "ORDER BY created_at, rowid",
            (property_id, *sorted(CLOSED_STATUSES)),
        )
        return [_incident(row) for row in rows]

    def create(
        self,
        *,
        property_id: str,
        booking_id: str | None,
        description: str,
        reported_via: str,
        reporter: str,
        severity: str,
        correlation_id: str | None,
    ) -> Incident:
        incident_id, now = new_id("inc"), _now()
        self._db.execute(
            "INSERT INTO f2_incidents (id, property_id, booking_id, description, reported_via, "
            "reporter, severity, status, correlation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_staff', ?, ?, ?)",
            (
                incident_id,
                property_id,
                booking_id,
                description,
                reported_via,
                reporter,
                severity,
                correlation_id,
                now,
                now,
            ),
        )
        incident = self.get(incident_id)
        assert incident is not None
        return self._changed(incident)

    def awaiting_guest_for_booking(self, booking_id: str) -> list[Incident]:
        rows = self._db.query(
            "SELECT * FROM f2_incidents WHERE booking_id = ? AND status = 'awaiting_guest'",
            (booking_id,),
        )
        return [_incident(row) for row in rows]

    def update(self, incident_id: str, **fields: Any) -> Incident:
        if "drafts" in fields and isinstance(fields["drafts"], GuestDrafts):
            fields["drafts"] = to_json(fields["drafts"].model_dump())
        for key, value in list(fields.items()):
            if isinstance(value, datetime):
                fields[key] = value.isoformat()
            elif isinstance(value, bool):
                fields[key] = int(value)
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._db.execute(
            f"UPDATE f2_incidents SET {assignments} WHERE id = ?", (*fields.values(), incident_id)
        )
        incident = self.get(incident_id)
        assert incident is not None
        return self._changed(incident)

    def with_deadline_before(self, column: str, status: str, moment: datetime) -> list[Incident]:
        assert column in {"staff_deadline", "guest_deadline"}
        rows = self._db.query(
            f"SELECT * FROM f2_incidents WHERE status = ? AND {column} IS NOT NULL AND {column} <= ?",
            (status, moment.isoformat()),
        )
        return [_incident(row) for row in rows]

    def view(self, incident: Incident) -> IncidentView:
        return IncidentView(
            **incident.model_dump(),
            reports=self.reports(incident.id),
            staff_messages=self.staff_messages(incident.id),
            tasks=self.tasks(incident.id),
        )

    # --- Rapoarte ----------------------------------------------------------------------

    def add_report(
        self,
        incident_id: str,
        *,
        description: str,
        reported_via: str,
        reporter: str,
        severity: str,
        booking_id: str | None,
    ) -> IncidentReport:
        report_id = new_id("rep")
        self._db.execute(
            "INSERT INTO f2_incident_reports (id, incident_id, description, reported_via, reporter, "
            "severity, booking_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (report_id, incident_id, description, reported_via, reporter, severity, booking_id, _now()),
        )
        row = self._db.query_one("SELECT * FROM f2_incident_reports WHERE id = ?", (report_id,))
        return IncidentReport(**row)

    def reports(self, incident_id: str) -> list[IncidentReport]:
        rows = self._db.query(
            "SELECT * FROM f2_incident_reports WHERE incident_id = ? ORDER BY created_at, rowid",
            (incident_id,),
        )
        return [IncidentReport(**row) for row in rows]

    # --- Conversația cu personalul -----------------------------------------------------

    def add_staff_message(
        self,
        incident_id: str,
        *,
        direction: str,
        body: str,
        staff_id: str | None,
        staff_name: str,
        simulated: bool = False,
    ) -> StaffMessage:
        message_id = new_id("smsg")
        self._db.execute(
            "INSERT INTO f2_staff_messages (id, incident_id, staff_id, staff_name, direction, body, "
            "simulated, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, incident_id, staff_id, staff_name, direction, body, int(simulated), _now()),
        )
        return self.staff_message(message_id)  # type: ignore[return-value]

    def staff_message(self, message_id: str) -> StaffMessage | None:
        row = self._db.query_one("SELECT * FROM f2_staff_messages WHERE id = ?", (message_id,))
        return StaffMessage(**row) if row else None

    def staff_messages(self, incident_id: str) -> list[StaffMessage]:
        rows = self._db.query(
            "SELECT * FROM f2_staff_messages WHERE incident_id = ? ORDER BY created_at, rowid",
            (incident_id,),
        )
        return [StaffMessage(**row) for row in rows]

    # --- Sarcini -----------------------------------------------------------------------

    def create_task(
        self, incident_id: str, *, description: str, staff_id: str | None, staff_name: str | None
    ) -> StaffTask:
        # Fără `await` între citire și inserare: în bucla de evenimente nu există cursă.
        row = self._db.query_one("SELECT COALESCE(MAX(seq), 0) AS last FROM f2_tasks")
        task_id = f"T-{(row['last'] if row else 0) + 1}"
        self._db.execute(
            "INSERT INTO f2_tasks (id, incident_id, staff_id, staff_name, description, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (task_id, incident_id, staff_id, staff_name, description, _now()),
        )
        return self.task(task_id)  # type: ignore[return-value]

    def task(self, task_id: str) -> StaffTask | None:
        row = self._db.query_one("SELECT * FROM f2_tasks WHERE id = ?", (task_id,))
        return _task(row) if row else None

    def tasks(self, incident_id: str) -> list[StaffTask]:
        rows = self._db.query(
            "SELECT * FROM f2_tasks WHERE incident_id = ? ORDER BY seq", (incident_id,)
        )
        return [_task(row) for row in rows]

    def complete_task(self, task_id: str, *, note: str | None, completed_by: str) -> bool:
        """Marchează sarcina terminată. False dacă era deja terminată (idempotent)."""
        cursor = self._db.execute(
            "UPDATE f2_tasks SET status = 'done', completion_note = ?, completed_by = ?, "
            "completed_at = ? WHERE id = ? AND status = 'open'",
            (note, completed_by, _now(), task_id),
        )
        return cursor.rowcount == 1


def _task(row: dict[str, Any]) -> StaffTask:
    data = dict(row)
    data.pop("seq", None)
    return StaffTask(**data)
