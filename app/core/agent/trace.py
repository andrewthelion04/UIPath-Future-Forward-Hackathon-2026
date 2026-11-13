"""Jurnalul rulărilor de agent: fiecare pas cu mesaje, unealtă, argumente, rezultat, tokeni, cost.

Din acest jurnal se construiește timeline-ul din UI.
"""

from __future__ import annotations

from typing import Any

from app.core.clock import utcnow
from app.core.db import Database, from_json, to_json
from app.core.event_bus import current_correlation_id
from app.core.ids import new_id
from app.ports.llm import ChatMessage, Usage


class AgentTrace:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start_run(
        self,
        *,
        agent: str,
        feature: str,
        objective: str,
        booking_id: str | None = None,
        property_id: str | None = None,
    ) -> str:
        run_id = new_id("run")
        self._db.execute(
            "INSERT INTO agent_runs (id, agent, feature, objective, status, booking_id, "
            "property_id, correlation_id, started_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
            (
                run_id,
                agent,
                feature,
                objective,
                booking_id,
                property_id,
                current_correlation_id(),
                utcnow().isoformat(),
            ),
        )
        return run_id

    def add_step(
        self,
        run_id: str,
        *,
        kind: str,  # llm | tool | finish | nudge | error
        tool_name: str | None = None,
        reason: str | None = None,
        arguments: Any = None,
        result: Any = None,
        is_error: bool = False,
        messages: list[ChatMessage] | None = None,
        content: str | None = None,
        model: str | None = None,
        usage: Usage | None = None,
        duration_ms: int | None = None,
    ) -> None:
        usage = usage or Usage()
        with self._db.transaction():
            row = self._db.query_one(
                "SELECT COALESCE(MAX(idx), 0) + 1 AS next FROM agent_steps WHERE run_id = ?",
                (run_id,),
            )
            self._db.execute(
                "INSERT INTO agent_steps (run_id, idx, kind, tool_name, reason, arguments, "
                "result, is_error, messages, content, model, prompt_tokens, completion_tokens, "
                "cost, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    row["next"] if row else 1,
                    kind,
                    tool_name,
                    reason,
                    None if arguments is None else to_json(arguments),
                    None if result is None else to_json(result),
                    int(is_error),
                    None
                    if messages is None
                    else to_json([m.model_dump(exclude_none=True) for m in messages]),
                    content,
                    model,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.cost,
                    duration_ms,
                    utcnow().isoformat(),
                ),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,  # finished | max_steps | error
        steps: int,
        usage: Usage,
        model: str | None = None,
        result: str | None = None,
        evidence: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self._db.execute(
            "UPDATE agent_runs SET status = ?, result = ?, evidence = ?, error = ?, model = ?, "
            "steps = ?, prompt_tokens = ?, completion_tokens = ?, cost = ?, finished_at = ? "
            "WHERE id = ?",
            (
                status,
                result,
                to_json(evidence or []),
                error,
                model,
                steps,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.cost,
                utcnow().isoformat(),
                run_id,
            ),
        )

    # --- Citire (timeline) ---------------------------------------------------------

    def list_runs(
        self, *, limit: int = 50, feature: str | None = None, booking_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if feature:
            clauses.append("feature = ?")
            params.append(feature)
        if booking_id:
            clauses.append("booking_id = ?")
            params.append(booking_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT * FROM agent_runs {where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        )
        for row in rows:
            row["evidence"] = from_json(row["evidence"], [])
        return rows

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._db.query_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        if run is None:
            return None
        run["evidence"] = from_json(run["evidence"], [])
        steps = self._db.query("SELECT * FROM agent_steps WHERE run_id = ? ORDER BY idx", (run_id,))
        for step in steps:
            step["arguments"] = from_json(step["arguments"])
            step["result"] = from_json(step["result"])
            step["messages"] = from_json(step["messages"])
            step["is_error"] = bool(step["is_error"])
        run["steps_detail"] = steps
        return run
