"""Acces minimal la SQLite: o conexiune, SQL scris de mână, fără ORM."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def to_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def from_json(text: str | None, default: Any = None) -> Any:
    return default if text is None or text == "" else json.loads(text)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit; tranzacțiile se deschid explicit.
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Tranzacție atomică. Nu trebuie să conțină `await`."""
        with self._lock:
            if self._tx_depth > 0:  # tranzacție imbricată: se alătură celei exterioare
                self._tx_depth += 1
                try:
                    yield
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._tx_depth = 0

    def ensure_column(self, table: str, column: str, definition: str) -> bool:
        """Adaugă o coloană lipsă pe o bază existentă (`CREATE TABLE IF NOT EXISTS` nu o face).

        Doar pentru adăugări: coloana trebuie să fie opțională sau să aibă o valoare implicită.
        Întoarce True dacă a adăugat-o.
        """
        with self._lock:
            existing = {row["name"] for row in self._conn.execute(f'PRAGMA table_info("{table}")')}
            if not existing or column in existing:
                return False  # tabelul nu există încă (îl creează schema) sau coloana e deja acolo
            self._conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {definition}')
            return True

    def table_names(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return [row["name"] for row in rows]

    def drop_all_tables(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = OFF")
            for name in self.table_names():
                self._conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
