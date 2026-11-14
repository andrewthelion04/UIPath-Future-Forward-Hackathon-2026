"""Evaluarea F3 folosește efecte observabile și un mediu mock izolat."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.main import create_app
from app.ports.llm import ChatMessage, ChatResponse, ToolCall, Usage
from scripts import eval_models


def test_cli_help_is_english_and_works_with_windows_console_encoding(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(eval_models.ROOT / "scripts" / "eval_models.py"), "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
        capture_output=True,
        text=True,
        encoding="cp1252",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Evaluate guest copilot scenarios" in completed.stdout
    assert "--models mock" in completed.stdout
    assert "UnicodeEncodeError" not in completed.stderr


def test_mock_cli_runs_all_scenarios_without_touching_configured_database(tmp_path):
    existing_database = tmp_path / "application.db"
    existing_database.write_bytes(b"This application database must remain untouched.")
    original = existing_database.read_bytes()
    report_path = tmp_path / "report.json"
    environment = dict(os.environ)
    environment.update(
        PYTHONUTF8="1",
        DATABASE_PATH=str(existing_database),
        PMS_PROVIDER="ownerrez",
        OWNERREZ_ACCESS_TOKEN="unused-evaluation-token",
        LLM_PROVIDER="openrouter",
        LLM_MODEL="must-not-be-called",
        REVIEWS_PROVIDER="apify",
        MARKET_PROVIDER="inside_airbnb",
        DISABLED_FEATURES='["f3_guest_copilot"]',
    )
    completed = subprocess.run(
        [sys.executable, str(eval_models.ROOT / "scripts" / "eval_models.py"),
         "--models", "mock", "--format", "json", "--output", str(report_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report == json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] is True
    assert report["pms_provider"] == "mock" and report["fixed_today"] == "2026-06-01"
    assert {result["scenario"] for result in report["results"]} == {
        scenario.name for scenario in eval_models.SCENARIOS
    }
    assert len(report["results"]) == 14  # 8 cereri ale oaspetelui + 6 conversații despre un incident
    for result in report["results"]:
        assert result["model"] == "mock" and result["success"] is True
        assert result["steps"] > 0 and result["elapsed_s"] > 0 and result["cost"] >= 0
        assert result["checks"] and all(result["checks"].values())
    assert existing_database.read_bytes() == original
    assert {path.name for path in tmp_path.iterdir()} == {"application.db", "report.json"}


def test_evaluation_configuration_forces_mock_dependencies_for_explicit_real_model(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_models, "ROOT", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-secret")
    monkeypatch.setenv("PMS_PROVIDER", "hospitable")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "user.db"))
    monkeypatch.setenv("REVIEWS_PROVIDER", "apify")
    monkeypatch.setenv("MARKET_PROVIDER", "inside_airbnb")
    selected = eval_models.evaluation_settings("vendor/selected-model", tmp_path / "isolated.db")
    assert selected.llm_provider == "openrouter" and selected.llm_model == "vendor/selected-model"
    assert selected.pms_provider == selected.reviews_provider == selected.market_provider == "mock"
    assert selected.database_path == tmp_path / "isolated.db"
    assert selected.fixed_today == eval_models.TODAY
    assert selected.disabled_features == []
    assert not selected.database_path.exists()


def test_missing_real_model_key_fails_before_any_scenario(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_models, "ROOT", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def forbidden_run(*args, **kwargs):
        pytest.fail("A scenario must not run before credential validation")

    monkeypatch.setattr(eval_models, "run_scenario", forbidden_run)
    assert eval_models.main(["--models", "vendor/selected-model"]) == 2
    captured = capsys.readouterr()
    assert "OPENROUTER_API_KEY" in captured.err and "--models mock" in captured.err
    assert captured.out == ""


def test_finish_without_messages_cannot_pass_effect_checks():
    checks = eval_models.assess_effects(
        eval_models.SCENARIOS[0],
        {"id": "bk_evaluation", "check_in": "2026-06-03"},
        messages=[], approvals=[], events=[], notifications=[],
    )
    assert not checks["guest_received_reply"]
    assert not checks["each_guest_message_published"]
    assert not all(checks.values())


def test_failed_effects_produce_nonzero_exit_and_csv_report(tmp_path, monkeypatch, capsys):
    result = eval_models.EvaluationResult(
        model="mock", scenario="grounded_info", success=False,
        steps=3, checks={"guest_received_reply": False},
    )
    monkeypatch.setattr(eval_models, "run_scenario", lambda *args, **kwargs: result)
    output = tmp_path / "report.csv"
    code = eval_models.main([
        "--models", "mock", "--scenarios", "grounded_info", "--format", "json",
        "--output", str(output),
    ])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["success"] is False
    (row,) = list(csv.DictReader(io.StringIO(output.read_text(encoding="utf-8"))))
    assert row["scenario"] == "grounded_info" and row["success"] == "False"
    assert json.loads(row["checks"]) == {"guest_received_reply": False}


def test_failed_local_execution_reports_the_error(tmp_path, monkeypatch):
    def broken_app(settings):
        raise RuntimeError("test app could not start")

    monkeypatch.setattr(eval_models, "create_app", broken_app)
    result = eval_models.run_scenario("mock", eval_models.SCENARIOS[0])
    assert result.success is False
    assert result.checks["scenario_completed"] is False
    assert result.error == "RuntimeError: test app could not start"


def test_unsuccessful_scenario_still_reports_steps_and_llm_cost(monkeypatch):
    class FinishWithoutEffects:
        name = "finish_without_effects"

        async def chat(self, request):
            return ChatResponse(
                message=ChatMessage(role="assistant", tool_calls=[ToolCall.make(
                    "finish", result="I did nothing.", evidence=[], reason="test outcome",
                )]),
                usage=Usage(prompt_tokens=8, completion_tokens=6, cost=0.0123),
                model=self.name,
            )

    monkeypatch.setattr(
        eval_models, "create_app",
        lambda settings: create_app(settings, overrides={"llm": FinishWithoutEffects()}),
    )
    approval_scenario = next(s for s in eval_models.SCENARIOS if s.name == "approved_early")
    result = eval_models.run_scenario("mock", approval_scenario)
    assert result.success is False and "Expected one guest approval" in result.error
    assert result.steps == 1 and result.cost == pytest.approx(0.0123)


def test_interrupted_run_reports_known_usage_from_completed_llm_steps(monkeypatch):
    class InterruptedLLM:
        name = "interrupted_after_billed_context"

        def __init__(self):
            self.calls = 0

        async def chat(self, request):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    message=ChatMessage(role="assistant", tool_calls=[ToolCall.make(
                        "get_booking_context", reason="Read the known booking context.",
                    )]),
                    usage=Usage(prompt_tokens=8, completion_tokens=6, cost=0.0123),
                    model=self.name,
                )
            await asyncio.Event().wait()

    llm = InterruptedLLM()
    monkeypatch.setattr(
        eval_models, "create_app",
        lambda settings: create_app(
            settings.model_copy(update={"llm_timeout_s": 10}), overrides={"llm": llm}
        ),
    )
    result = eval_models.run_scenario("mock", eval_models.SCENARIOS[0], timeout_s=0.2)
    assert llm.calls == 2
    assert result.success is False and result.checks["agent_runs_finished"] is False
    assert result.steps >= 1 and result.cost == pytest.approx(0.0123)
    assert result.usage_complete is False
    json_report = json.loads(eval_models.render_results([result], "json"))
    assert json_report["results"][0]["usage_complete"] is False
    assert "0.012300 (partial)" in eval_models.render_results([result], "table")
