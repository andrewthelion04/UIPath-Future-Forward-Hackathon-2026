"""Povestea unui flux, citită din jurnalele nucleului: ce s-a întâmplat, cine a decis și de ce.

Un flux = tot ce împarte un `correlation_id`: declanșatorul, evenimentele, rulările agenților
cu fiecare pas și motivul lui (`reason`), aprobările cerute hostului și decizia lui,
notificările și mesajele trimise oaspetelui. Nimic nu se stochează în plus: totul se
reconstituie din tabelele comune, deci dashboardul arată exact ce a rulat.

Nu citește tabelele feature-urilor și nu importă niciun feature.
"""

from __future__ import annotations

import re
from datetime import datetime
from statistics import median
from typing import Any

from app.core.db import Database, from_json
from app.core.world_repo import WorldRepository

# Evenimentele comune care spun singure ceva unui om; restul sunt pași interni ai unui feature.
HEADLINE_EVENTS = {
    "GuestMessageReceived",
    "GuestMessageSent",
    "IssueReported",
    "BookingCancelled",
    "ReviewAnalysisRequested",
    "GuestOfferAnswered",
    "ApprovalRequested",
    "ApprovalDecided",
}
FLOW_KINDS = {
    "IssueReported": "incident",
    "BookingCancelled": "cancellation",
    "ReviewAnalysisRequested": "reviews",
    "GuestMessageReceived": "conversation",
}
MAX_TEXT = 600


def _short(value: Any, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _words(name: str) -> str:
    """`IncidentTaskCompleted` -> `Incident task completed`; `send_guest_reply` -> `Send guest reply`."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:].lower() if spaced else name


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)
    except ValueError:
        return None


class FlowStories:
    def __init__(self, db: Database, world: WorldRepository) -> None:
        self._db = db
        self._world = world

    # --- Nume lizibile -------------------------------------------------------------

    def _names(self) -> tuple[dict[str, str], dict[str, str]]:
        guests = {booking.id: booking.guest_name for booking in self._world.list_bookings()}
        properties = {prop.id: prop.name for prop in self._world.list_properties()}
        return guests, properties

    @staticmethod
    def _title(event_type: str, payload: dict[str, Any], guests: dict[str, str], properties: dict[str, str]) -> str:
        guest = guests.get(payload.get("booking_id") or "", "a guest")
        place = properties.get(payload.get("property_id") or "", "")
        if event_type == "GuestMessageReceived":
            return f"Guest message · {guest} · “{_short(payload.get('text'), 70)}”"
        if event_type == "IssueReported":
            return f"Incident · {place or 'a property'} · “{_short(payload.get('description'), 70)}”"
        if event_type == "BookingCancelled":
            return f"Cancellation · {guest} · {place}"
        if event_type == "ReviewAnalysisRequested":
            return f"Review analysis · {place or 'all properties'}"
        return _words(event_type)

    # --- Lista fluxurilor ----------------------------------------------------------

    def list_flows(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fluxurile, cele mai recente primele, cu ce le-a pornit și în ce stare sunt."""
        guests, properties = self._names()
        heads = self._db.query(
            "SELECT correlation_id, COUNT(*) AS events, MIN(occurred_at) AS started_at, "
            "MAX(occurred_at) AS last_at FROM events WHERE correlation_id IS NOT NULL "
            "GROUP BY correlation_id ORDER BY last_at DESC LIMIT ?",
            (limit,),
        )
        return [self._summary(row, guests, properties) for row in heads]

    def _summary(
        self, head: dict[str, Any], guests: dict[str, str], properties: dict[str, str]
    ) -> dict[str, Any]:
        flow = head["correlation_id"]
        events = self._db.query(
            "SELECT id, type, payload, source, occurred_at FROM events WHERE correlation_id = ? "
            "ORDER BY occurred_at, rowid",
            (flow,),
        )
        for event in events:
            event["payload"] = from_json(event["payload"], {})
        trigger = events[0]
        types = [event["type"] for event in events]
        # Un mesaj al oaspetelui care raportează o problemă pornește, de fapt, un incident.
        headline = next((e for e in events if e["type"] == "IssueReported"), trigger)
        kind = FLOW_KINDS.get(headline["type"], FLOW_KINDS.get(trigger["type"], "other"))
        context = next((e["payload"] for e in events if e["payload"].get("booking_id")), {})
        context = context or next((e["payload"] for e in events if e["payload"].get("property_id")), {})

        runs = self._db.query_one(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(cost), 0) AS cost, "
            "COALESCE(SUM(status = 'running'), 0) AS running, "
            "COALESCE(SUM(status NOT IN ('finished', 'running')), 0) AS failed "
            "FROM agent_runs WHERE correlation_id = ?",
            (flow,),
        ) or {}
        steps = self._db.query_one(
            "SELECT COUNT(*) AS tool_calls, COALESCE(SUM(s.is_error), 0) AS corrected "
            "FROM agent_steps s JOIN agent_runs r ON r.id = s.run_id "
            "WHERE r.correlation_id = ? AND s.kind = 'tool'",
            (flow,),
        ) or {}
        approvals = self._db.query(
            "SELECT status FROM approvals WHERE correlation_id = ?", (flow,)
        )
        handler_problems = self._db.query_one(
            "SELECT COUNT(*) AS total FROM event_handler_runs h JOIN events e ON e.id = h.event_id "
            "WHERE e.correlation_id = ? AND h.status NOT IN ('ok', 'running')",
            (flow,),
        ) or {}
        pending = sum(1 for approval in approvals if approval["status"] == "pending")
        if handler_problems.get("total") or runs.get("failed"):
            status = "needs_attention"
        elif pending:
            status = "waiting_for_host"
        elif runs.get("running"):
            status = "running"
        else:
            status = "done"
        return {
            "correlation_id": flow,
            "kind": kind,
            "title": self._title(headline["type"], headline["payload"], guests, properties),
            "trigger": trigger["type"],
            "source": trigger["source"],
            "status": status,
            "started_at": head["started_at"],
            "last_at": head["last_at"],
            "duration_s": _seconds(head["started_at"], head["last_at"]),
            "booking_id": context.get("booking_id"),
            "guest_name": guests.get(context.get("booking_id") or ""),
            "property_id": context.get("property_id"),
            "property_name": properties.get(context.get("property_id") or ""),
            "events": head["events"],
            "agent_runs": runs.get("runs", 0),
            "tool_calls": steps.get("tool_calls", 0),
            "corrected_by_code": steps.get("corrected", 0),
            "approvals": len(approvals),
            "approvals_pending": pending,
            "guest_messages_sent": types.count("GuestMessageSent"),
            "cost": round(runs.get("cost") or 0, 6),
        }

    # --- Povestea unui flux ----------------------------------------------------------

    def story(self, correlation_id: str) -> dict[str, Any] | None:
        head = self._db.query_one(
            "SELECT correlation_id, COUNT(*) AS events, MIN(occurred_at) AS started_at, "
            "MAX(occurred_at) AS last_at FROM events WHERE correlation_id = ?",
            (correlation_id,),
        )
        if head is None or not head["events"]:
            return None
        guests, properties = self._names()
        summary = self._summary(head, guests, properties)
        items: list[dict[str, Any]] = []

        events = self._db.query(
            "SELECT * FROM events WHERE correlation_id = ? ORDER BY occurred_at, rowid", (correlation_id,)
        )
        graph = []
        for event in events:
            payload = from_json(event["payload"], {})
            handlers = self._db.query(
                "SELECT handler, feature, status, error, duration_ms FROM event_handler_runs "
                "WHERE event_id = ? ORDER BY id",
                (event["id"],),
            )
            graph.append({
                "id": event["id"], "type": event["type"], "source": event["source"],
                "causation_id": event["causation_id"], "occurred_at": event["occurred_at"],
                "handlers": [{"handler": h["handler"], "status": h["status"]} for h in handlers],
            })  # fmt: skip
            items.append(self._event_item(event, payload, guests, properties))
            for handler in handlers:
                if handler["status"] not in ("ok", "running"):
                    items.append({
                        "at": event["occurred_at"], "kind": "problem", "actor": "code",
                        "feature": handler["feature"], "minor": False,
                        "title": f"{handler['handler']} {handler['status']}",
                        "detail": _short(handler["error"]),
                    })  # fmt: skip

        for run in self._db.query(
            "SELECT * FROM agent_runs WHERE correlation_id = ? ORDER BY started_at, rowid", (correlation_id,)
        ):
            items.append(self._run_item(run))

        for note in self._db.query(
            "SELECT * FROM notifications WHERE correlation_id = ? AND approval_id IS NULL "
            "ORDER BY created_at, rowid",
            (correlation_id,),
        ):
            items.append({
                "at": note["created_at"], "kind": "notification", "actor": "code",
                "feature": note["feature"], "minor": note["level"] in ("info", "success"),
                "title": note["title"], "detail": _short(note["body"]), "level": note["level"],
            })  # fmt: skip

        items.sort(key=lambda item: item["at"])
        return {**summary, "items": items, "graph": graph, "linked": self._linked(correlation_id, guests, properties)}

    def _event_item(
        self, event: dict[str, Any], payload: dict[str, Any], guests: dict[str, str], properties: dict[str, str]
    ) -> dict[str, Any]:
        kind = event["type"]
        source = event["source"] or ""
        feature = source.removeprefix("feature:") if source.startswith("feature:") else None
        item: dict[str, Any] = {
            "at": event["occurred_at"], "kind": "event", "event": kind, "actor": "code",
            "feature": feature, "minor": kind not in HEADLINE_EVENTS, "title": _words(kind), "detail": "",
        }  # fmt: skip
        guest = guests.get(payload.get("booking_id") or "", "The guest")
        if kind == "GuestMessageReceived":
            item.update(actor="guest", title=f"{guest} wrote", detail=_short(payload.get("text")))
        elif kind == "GuestMessageSent":
            approved = payload.get("sender") == "host"
            item.update(
                actor="host" if approved else "agent",
                title=f"Message to {guest}" + (" (text approved by the host)" if approved else " (sent by the copilot)"),
                detail=_short(payload.get("text")),
            )
        elif kind == "IssueReported":
            via = payload.get("reported_via")
            item.update(
                actor="guest" if via == "guest" else "staff",
                title=f"{payload.get('reporter') or via} reported a problem at "
                f"{properties.get(payload.get('property_id') or '', 'a property')}",
                detail=_short(payload.get("description")),
            )
        elif kind == "BookingCancelled":
            item.update(actor="guest", title=f"{guest} cancelled",
                        detail=f"{payload.get('check_in')} to {payload.get('check_out')}")  # fmt: skip
        elif kind == "ReviewAnalysisRequested":
            item.update(actor="host", title="Review analysis requested",
                        detail=properties.get(payload.get("property_id") or "", "All properties"))  # fmt: skip
        elif kind == "GuestOfferAnswered":
            item.update(actor="guest", title=f"{guest} answered the offer: {payload.get('choice')}")
        elif kind == "ApprovalRequested":
            approval = self._db.query_one("SELECT * FROM approvals WHERE id = ?", (payload.get("approval_id"),))
            item.update(kind="approval_requested", actor="agent", title=f"Asked the host: {payload.get('title')}")
            if approval:
                item.update(
                    detail=_short(approval["summary"], 2500),
                    options=[
                        {"key": o.get("key"), "label": o.get("label"), "description": _short(o.get("description"), 1500)}
                        for o in from_json(approval["options"], [])
                    ],
                    status=approval["status"],
                    chosen_option=approval["chosen_option"],
                )
        elif kind == "ApprovalDecided":
            decision = payload.get("decision")
            option = f" · {payload.get('option')}" if payload.get("option") else ""
            item.update(kind="approval_decided", actor="host",
                        title=f"Host {decision}{option}", detail=_short(payload.get("note")))  # fmt: skip
        return item

    def _run_item(self, run: dict[str, Any]) -> dict[str, Any]:
        steps = self._db.query(
            "SELECT idx, kind, tool_name, reason, arguments, result, is_error, content, duration_ms "
            "FROM agent_steps WHERE run_id = ? AND kind IN ('tool', 'finish', 'error') ORDER BY idx",
            (run["id"],),
        )
        decisions = []
        for step in steps:
            arguments = from_json(step["arguments"]) or {}
            arguments = {k: v for k, v in arguments.items() if k != "reason"} if isinstance(arguments, dict) else {}
            result = from_json(step["result"])
            decisions.append({
                "tool": step["tool_name"] or step["kind"],
                "label": _words(step["tool_name"] or step["kind"]),
                "reason": step["reason"] or "",
                "is_error": bool(step["is_error"]),
                "arguments": _short(arguments, 900) if arguments else "",
                "result": _short(result if isinstance(result, str) else result or step["content"], 900),
            })  # fmt: skip
        short = run["agent"].split(".")[-1]
        # Convenție: un agent `sim_*` joacă un om într-o simulare; nu e o decizie a copilotului.
        simulated = short.startswith("sim_")
        title = (
            f"Simulated {short[4:].replace('_', ' ')} (played by the model)"
            if simulated
            else f"Agent {short.replace('_', ' ')}"
        )
        return {
            "at": run["started_at"], "kind": "agent_run", "actor": "staff" if simulated else "agent",
            "feature": run["feature"], "minor": False, "title": title, "simulated": simulated,
            "agent": run["agent"], "run_id": run["id"], "status": run["status"],
            "detail": _short(run["result"] or run["error"]),
            "objective": _short(run["objective"], 900),
            "evidence": from_json(run["evidence"], []),
            "steps": decisions, "model": run["model"], "cost": run["cost"] or 0,
            "tokens": (run["prompt_tokens"] or 0) + (run["completion_tokens"] or 0),
            "duration_s": _seconds(run["started_at"], run["finished_at"]),
        }  # fmt: skip

    def _linked(
        self, correlation_id: str, guests: dict[str, str], properties: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Fluxurile legate cauzal: ex. mesajul din chat care a acceptat oferta unui incident."""
        rows = self._db.query(
            "SELECT DISTINCT cause.correlation_id AS other, 'caused_by' AS relation, effect.type AS via "
            "FROM events effect JOIN events cause ON cause.id = effect.causation_id "
            "WHERE effect.correlation_id = ? AND cause.correlation_id != effect.correlation_id "
            "UNION "
            "SELECT DISTINCT effect.correlation_id AS other, 'led_to' AS relation, effect.type AS via "
            "FROM events effect JOIN events cause ON cause.id = effect.causation_id "
            "WHERE cause.correlation_id = ? AND cause.correlation_id != effect.correlation_id",
            (correlation_id, correlation_id),
        )
        linked = []
        for row in rows:
            first = self._db.query_one(
                "SELECT type, payload FROM events WHERE correlation_id = ? ORDER BY occurred_at, rowid LIMIT 1",
                (row["other"],),
            )
            if first:
                linked.append({
                    "correlation_id": row["other"], "relation": row["relation"], "via": _words(row["via"]),
                    "title": self._title(first["type"], from_json(first["payload"], {}), guests, properties),
                })  # fmt: skip
        return linked

    # --- Indicatori și jurnalul deciziilor -------------------------------------------

    def overview(self) -> dict[str, Any]:
        one = self._db.query_one
        flows = one("SELECT COUNT(DISTINCT correlation_id) AS n FROM events")["n"]
        messages = {
            row["bucket"]: row["n"]
            for row in self._db.query(
                "SELECT CASE WHEN direction = 'inbound' THEN 'from_guest' "
                "WHEN status != 'sent' THEN 'failed' WHEN sender = 'copilot' THEN 'autonomous' "
                "ELSE 'approved' END AS bucket, COUNT(*) AS n FROM messages GROUP BY bucket"
            )
        }
        approvals = {
            row["status"]: row["n"]
            for row in self._db.query("SELECT status, COUNT(*) AS n FROM approvals GROUP BY status")
        }
        runs = one(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(status = 'finished'), 0) AS finished, "
            "COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(prompt_tokens), 0) AS tokens_in, "
            "COALESCE(SUM(completion_tokens), 0) AS tokens_out FROM agent_runs"
        )
        steps = one(
            "SELECT COUNT(*) AS tool_calls, COALESCE(SUM(is_error), 0) AS corrected "
            "FROM agent_steps WHERE kind = 'tool'"
        )
        problems = one(
            "SELECT COUNT(*) AS n FROM event_handler_runs WHERE status NOT IN ('ok', 'running')"
        )["n"]
        models = [row["model"] for row in self._db.query(
            "SELECT DISTINCT model FROM agent_runs WHERE model IS NOT NULL ORDER BY model"
        )]  # fmt: skip
        return {
            "flows": flows,
            "guest_messages": messages.get("from_guest", 0),
            "replies_autonomous": messages.get("autonomous", 0),
            "messages_host_approved": messages.get("approved", 0),
            "messages_failed": messages.get("failed", 0),
            "median_reply_s": self._median_reply_seconds(),
            "approvals": {name: approvals.get(name, 0) for name in ("pending", "approved", "rejected", "withdrawn")},
            "agent_runs": runs["runs"],
            "agent_runs_finished": runs["finished"],
            "tool_calls": steps["tool_calls"],
            "corrected_by_code": steps["corrected"],
            "cost": round(runs["cost"], 6),
            "tokens_in": runs["tokens_in"],
            "tokens_out": runs["tokens_out"],
            "handler_problems": problems,
            "models": models,
        }

    def _median_reply_seconds(self) -> float | None:
        """Timpul median de la un mesaj al oaspetelui până la primul răspuns pe acea rezervare."""
        rows = self._db.query(
            "SELECT booking_id, direction, created_at FROM messages WHERE status != 'failed' "
            "ORDER BY booking_id, created_at, rowid"
        )
        waits: list[float] = []
        waiting: dict[str, str] = {}
        for row in rows:
            if row["direction"] == "inbound":
                waiting.setdefault(row["booking_id"], row["created_at"])
            elif row["booking_id"] in waiting:
                seconds = _seconds(waiting.pop(row["booking_id"]), row["created_at"])
                if seconds is not None:
                    waits.append(seconds)
        return round(median(waits), 1) if waits else None

    def decisions(self, limit: int = 40) -> list[dict[str, Any]]:
        """Ultimele decizii din toate fluxurile: ale agentului (cu motiv), ale codului, ale hostului."""
        guests, properties = self._names()
        rows = self._db.query(
            "SELECT s.created_at AS at, s.tool_name, s.reason, s.is_error, s.result, s.kind, "
            "r.agent, r.feature, r.correlation_id, r.booking_id, r.property_id "
            "FROM agent_steps s JOIN agent_runs r ON r.id = s.run_id "
            "WHERE s.kind IN ('tool', 'finish') ORDER BY s.created_at DESC, s.id DESC LIMIT ?",
            (limit,),
        )
        decisions = []
        for row in rows:
            blocked = bool(row["is_error"])
            error = from_json(row["result"]) if blocked else None
            decisions.append({
                "at": row["at"], "actor": "code" if blocked else "agent", "feature": row["feature"],
                "agent": row["agent"],
                "what": (f"Corrected: {_words(row['tool_name'] or '')}" if blocked else _words(row["tool_name"] or row["kind"])),
                "why": _short(error.get("error") if isinstance(error, dict) else error, 300) if blocked else row["reason"] or "",
                "agent_intent": row["reason"] or "" if blocked else "",
                "correlation_id": row["correlation_id"],
                "guest_name": guests.get(row["booking_id"] or ""),
                "property_name": properties.get(row["property_id"] or ""),
            })  # fmt: skip
        for approval in self._db.query(
            "SELECT * FROM approvals WHERE status IN ('approved', 'rejected') "
            "ORDER BY decided_at DESC LIMIT ?",
            (limit,),
        ):
            decisions.append({
                "at": approval["decided_at"], "actor": "host", "feature": approval["feature"], "agent": None,
                "what": f"{approval['status'].capitalize()}: {approval['title']}"
                + (f" · {approval['chosen_option']}" if approval["chosen_option"] else ""),
                "why": approval["decision_note"] or "", "agent_intent": "",
                "correlation_id": approval["correlation_id"],
                "guest_name": guests.get(approval["booking_id"] or ""),
                "property_name": properties.get(approval["property_id"] or ""),
            })  # fmt: skip
        decisions.sort(key=lambda item: item["at"] or "", reverse=True)
        return decisions[:limit]
