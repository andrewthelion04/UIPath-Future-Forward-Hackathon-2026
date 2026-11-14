#!/usr/bin/env python3
"""Evaluează F3 prin efectele observabile, cu PMS mock și baze temporare.

Rulare fără cheie: `.venv/Scripts/python scripts/eval_models.py --models mock`.
Un model OpenRouter este folosit numai dacă ID-ul lui apare explicit în `--models`.
Scenariile nu folosesc baza aplicației și nu trimit mesaje către un PMS real.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import ConfigError, Settings, load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

FEATURE = "f3_guest_copilot"
TODAY = date(2026, 6, 1)


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    guest: str
    messages: tuple[str, ...]
    decision: str | None = None


SCENARIOS = (
    Scenario(
        "grounded_info", "Check-in and Wi-Fi facts come from the booking context.",
        "Emma Johnson", ("Hi! What time is check-in, and is there wifi?",),
    ),
    Scenario(
        "repeated_early", "A reformulated pending request reuses the same approval.",
        "Emma Johnson", (
            "Could we check in early, around noon?",
            "Can we arrive early at 12:00 on our check-in day?",
        ),
    ),
    Scenario(
        "approved_early", "An approved early check-in is confirmed once per decision.",
        "Emma Johnson", ("Could we check in early, around noon?",), "approve",
    ),
    Scenario(
        "rejected_late", "A rejected late check-out is communicated and not requested again.",
        "Lukas Mueller", (
            "Could we check out late at 13:00 on our last day?",
            "Can we leave later, at 13:00 on the check-out date?",
        ), "reject",
    ),
    Scenario(
        "pets_exception", "A pet exception requires the host's decision.",
        "Emma Johnson", ("Can we bring one dog for our stay?",),
    ),
    Scenario(
        "extra_guest_capacity", "An extra guest within capacity needs approval; excess is blocked.",
        "Lukas Mueller", (
            "Can we add one extra guest, making three guests in total?",
            "Can we add more extra guests, making four guests in total?",
        ),
    ),
    Scenario(
        "missing_parking_details", "Unknown parking location and cost are escalated, not invented.",
        "Emma Johnson", ("Where can we park nearby and how much does parking cost?",),
    ),
    Scenario(
        "mixed_issue_and_early", "A reported defect and an early check-in request are both handled.",
        "Emma Johnson", ("The shower is broken. Could we check in early at noon?",),
    ),
)


@dataclass
class EvaluationResult:
    model: str
    scenario: str
    success: bool = False
    steps: int = 0
    cost: float = 0.0
    usage_complete: bool = True
    elapsed_s: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


def evaluation_settings(model: str, database_path: Path, *, timeout_s: float = 60) -> Settings:
    """Configul CLI nu poate activa accidental adaptoare PMS sau baza utilizatorului."""
    return load_settings(
        # Mock-ul ignoră complet .env; pentru model real citim cheia/configul OpenRouter.
        env_file=None if model == "mock" else ROOT / ".env",
        database_path=database_path,
        seed_path=ROOT / "seed" / "world.json",
        fixed_today=TODAY,
        auto_seed=True,
        pms_provider="mock",
        reviews_provider="mock",
        market_provider="mock",
        llm_provider="mock" if model == "mock" else "openrouter",
        llm_model=None if model == "mock" else model,
        webhook_token=None,
        disabled_features=[],
        handler_timeout_s=timeout_s,
        sim_wait_timeout_s=timeout_s + 1,
        llm_timeout_s=min(timeout_s, 60),
        agent_max_steps=16,
    )


def _json(response) -> Any:
    if response.status_code >= 400:
        raise RuntimeError(f"Local evaluation API returned HTTP {response.status_code}")
    return response.json()


def _guest_approvals(client: TestClient, booking_id: str) -> list[dict[str, Any]]:
    return [
        approval for approval in _json(client.get("/approvals", params={"status": "all"}))
        if approval["feature"] == FEATURE and approval["kind"] == "guest_request"
        and approval["booking_id"] == booking_id
    ]


def _send(client: TestClient, booking_id: str, message: str) -> None:
    response = _json(client.post(
        "/sim/guest-message", params={"wait": True},
        json={"booking_id": booking_id, "text": message},
    ))
    if response.get("outcome") != "published" or response.get("completed") is not True:
        raise RuntimeError("The guest message was not fully processed")


def _decide(client: TestClient, approval_id: str, decision: str) -> dict[str, Any]:
    response = _json(client.post(
        f"/approvals/{approval_id}/decision", params={"wait": True},
        json={"decision": decision},
    ))
    if response.get("completed") is not True:
        raise RuntimeError("The host decision was not fully processed")
    return response


def assess_effects(
    scenario: Scenario,
    booking: dict[str, Any],
    messages: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    events: list[dict[str, Any]],
    notifications: list[dict[str, Any]],
) -> dict[str, bool]:
    """Verificări asupra efectelor; `finish` singur nu face un scenariu reușit."""
    inbound = [message for message in messages if message["direction"] == "inbound"]
    outbound = [message for message in messages if message["direction"] == "outbound"]
    sent = [message for message in outbound if message["status"] == "sent"]
    text = " ".join(message["body"] for message in sent).lower()
    received_events = [
        event for event in events if event["type"] == "GuestMessageReceived"
        and event["payload"]["booking_id"] == booking["id"]
    ]
    sent_events = [
        event for event in events if event["type"] == "GuestMessageSent"
        and event["payload"]["booking_id"] == booking["id"]
    ]
    checks = {
        "each_guest_message_published": len(inbound) == len(scenario.messages) == len(received_events),
        "guest_received_reply": len(sent) >= len(scenario.messages),
        "no_failed_deliveries": len(sent) == len(outbound),
        "sent_events_match_deliveries": len(sent_events) == len(sent),
        "internal_ids_only": all(
            "MOCK-" not in json.dumps(event["payload"])
            for event in (*received_events, *sent_events)
        ),
    }
    if scenario.name == "grounded_info":
        checks.update(
            no_approval=not approvals,
            check_in_from_data="15:00" in text and booking["check_in"] in text,
            wifi_answer="wi-fi" in text or "wifi" in text,
            no_invented_wifi_guide="welcome guide" not in text,
        )
    elif scenario.name == "missing_parking_details":
        checks.update(
            no_approval=not approvals,
            no_invented_parking=not any(phrase in text for phrase in (
                "public parking is available nearby", "a parking spot is included",
                "free parking is included", "parking is free",
            )),
            host_follow_up=any(
                notification["feature"] == FEATURE
                and notification.get("booking_id") == booking["id"]
                for notification in notifications
            ),
        )
    else:
        checks["one_scoped_approval"] = len(approvals) == 1
        if approvals:
            approval = approvals[0]
            expected_status = {
                "approved_early": "approved", "rejected_late": "rejected",
            }.get(scenario.name, "pending")
            checks["expected_approval_state"] = approval["status"] == expected_status
            expected_type = {
                "pets_exception": "pets", "extra_guest_capacity": "extra_guests",
                "rejected_late": "late_check_out",
            }.get(scenario.name, "early_check_in")
            request = approval["payload"].get("request", {})
            checks["expected_request_type"] = request.get("request_type") == expected_type
            if scenario.name == "approved_early":
                checks["reviewed_reply_delivered"] = any(
                    message["body"] == approval["payload"].get("proposed_reply") for message in sent
                )
                checks["one_decision_reply"] = len(sent) == 2
            elif scenario.name == "rejected_late":
                checks["no_approved_reply"] = all(
                    message["body"] != approval["payload"].get("proposed_reply") for message in sent
                )
            elif scenario.name == "extra_guest_capacity":
                checks["only_within_capacity_requested"] = request.get("guest_count") == 3
            else:
                checks["no_premature_confirmation"] = all(
                    message["body"] != approval["payload"].get("proposed_reply")
                    and "host confirmed" not in message["body"].lower() for message in sent
                )
        if scenario.name == "mixed_issue_and_early":
            checks["incident_forwarded"] = len([
                event for event in events if event["type"] == "IssueReported"
                and event["payload"].get("booking_id") == booking["id"]
            ]) == 1
    return checks


def run_scenario(model: str, scenario: Scenario, *, timeout_s: float = 60) -> EvaluationResult:
    result = EvaluationResult(model=model, scenario=scenario.name)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="staypilot-f3-eval-") as temporary:
        settings = evaluation_settings(model, Path(temporary) / "scenario.db", timeout_s=timeout_s)
        try:
            with TestClient(create_app(settings)) as client:
                container = client.app.state.container
                try:
                    if container.pms.providers() != ["mock"]:
                        raise RuntimeError("Evaluation requires only the mock PMS connection")
                    failed_features = [feature.name for feature in container.features if not feature.loaded]
                    if failed_features:
                        raise RuntimeError("Evaluation features failed to load: " + ", ".join(failed_features))
                    booking = next(
                        booking for booking in _json(client.get("/bookings"))
                        if booking["guest_name"] == scenario.guest
                    )
                    _send(client, booking["id"], scenario.messages[0])
                    if scenario.decision:
                        approvals = _guest_approvals(client, booking["id"])
                        if len(approvals) != 1:
                            raise RuntimeError("Expected one guest approval before the host decision")
                        first = _decide(client, approvals[0]["id"], scenario.decision)
                        repeated = _decide(client, approvals[0]["id"], scenario.decision)
                        result.checks["decision_applied_once"] = first["applied"] and not repeated["applied"]
                    for message in scenario.messages[1:]:
                        _send(client, booking["id"], message)
                    messages = _json(client.get(f"/bookings/{booking['id']}/messages"))
                    events = _json(client.get("/events"))
                    result.checks.update(assess_effects(
                        scenario, booking, messages, _guest_approvals(client, booking["id"]),
                        events, _json(client.get("/notifications")),
                    ))
                    result.checks["event_handlers_succeeded"] = all(
                        handler["status"] == "ok"
                        for event in events for handler in event["handlers"]
                    )
                finally:
                    # Chiar și un scenariu nereușit poate consuma apeluri și credite LLM.
                    runs = container.agent_trace.list_runs(feature=FEATURE, limit=1000)
                    known_cost = 0.0
                    for run in runs:
                        detail = container.agent_trace.get_run(run["id"])
                        llm_steps = [
                            step for step in detail["steps_detail"] if step["kind"] == "llm"
                        ] if detail else []
                        # La anulare, sumarul poate rămâne nefinalizat; pașii deja
                        # persistați păstrează consumul cunoscut fără dublă numărare.
                        result.steps += max(run["steps"], len(llm_steps))
                        known_cost += max(run["cost"], sum(step["cost"] for step in llm_steps))
                    result.cost = round(known_cost, 10)
                    # Consumul apelurilor fără răspuns nu poate fi estimat local.
                    result.usage_complete = all(
                        run["status"] in ("finished", "max_steps") for run in runs
                    )
                    result.checks["agent_runs_finished"] = bool(runs) and all(
                        run["status"] == "finished" for run in runs
                    )
                result.success = all(result.checks.values())
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.checks["scenario_completed"] = False
    result.elapsed_s = round(time.perf_counter() - started, 4)
    return result


def render_results(results: list[EvaluationResult], format: str) -> str:
    if format == "json":
        return json.dumps({
            "fixed_today": TODAY.isoformat(), "pms_provider": "mock",
            "success": bool(results) and all(result.success for result in results),
            "results": [asdict(result) for result in results],
        }, indent=2, ensure_ascii=False)
    if format == "csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=[
            "model", "scenario", "success", "steps", "cost", "usage_complete", "elapsed_s", "checks", "error",
        ])
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["checks"] = json.dumps(row["checks"], sort_keys=True)
            writer.writerow(row)
        return output.getvalue().rstrip()
    lines = ["MODEL | SCENARIO | SUCCESS | STEPS | COST | SECONDS"]
    for result in results:
        cost = f"{result.cost:.6f}" + (" (partial)" if not result.usage_complete else "")
        lines.append(
            f"{result.model} | {result.scenario} | {'PASS' if result.success else 'FAIL'} | "
            f"{result.steps} | {cost} | {result.elapsed_s:.3f}"
        )
        if not result.success:
            failed = ", ".join(key for key, value in result.checks.items() if not value)
            lines.append(f"  {result.error or failed}")
    lines.append(f"Passed {sum(result.success for result in results)}/{len(results)} scenarios.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate guest copilot scenarios with isolated mock PMS data.",
        epilog="Use --models mock to run without an API key or network access.",
    )
    parser.add_argument("--models", nargs="+", default=["mock"], help="mock and/or OpenRouter model IDs")
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")
    parser.add_argument("--output", type=Path, help="Optional .json or .csv report path")
    parser.add_argument("--scenarios", nargs="+", choices=[scenario.name for scenario in SCENARIOS])
    parser.add_argument("--timeout", type=float, default=60, help="Per-message handler timeout in seconds")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    models = list(dict.fromkeys(args.models))
    scenarios = [scenario for scenario in SCENARIOS if not args.scenarios or scenario.name in args.scenarios]
    try:
        # Validăm toate modelele înainte de a începe evaluările și de a face apeluri LLM.
        for model in models:
            evaluation_settings(model, Path(tempfile.gettempdir()) / "f3-eval-validation-only.db", timeout_s=args.timeout)
        results = [run_scenario(model, scenario, timeout_s=args.timeout) for model in models for scenario in scenarios]
    except ConfigError:
        print("Evaluation configuration is invalid. Explicit OpenRouter models require OPENROUTER_API_KEY; "
              "use --models mock to run without a key.", file=sys.stderr)
        return 2
    print(render_results(results, args.format))
    if args.output:
        suffix = args.output.suffix.lower()
        output_format = {".json": "json", ".csv": "csv"}.get(suffix, args.format)
        args.output.write_text(render_results(results, output_format) + "\n", encoding="utf-8")
    return 0 if results and all(result.success for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
