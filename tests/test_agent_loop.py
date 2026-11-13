"""Bucla agentului: terminare prin finish, erori întoarse modelului, logare, context."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel, Field

from app.container import CORE_SCHEMA
from app.core.agent.context import build_context_view, truncate_text
from app.core.agent.loop import AgentRunner, AgentSpec
from app.core.agent.tools import RunContext, Tool, ToolArgs
from app.core.agent.trace import AgentTrace
from app.core.db import Database
from app.ports.llm import ChatMessage, ChatRequest, ChatResponse, ToolCall, Usage


class ScriptedLLM:
    """Dublură de LLM: întoarce pe rând mesajele pregătite și ține minte cererile primite."""

    name = "scripted"

    def __init__(self, *messages: ChatMessage | Exception) -> None:
        self._script = list(messages)
        self.requests: list[ChatRequest] = []

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request.model_copy(deep=True))
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return ChatResponse(
            message=step, usage=Usage(prompt_tokens=100, completion_tokens=10, cost=0.002), model="test-model"
        )


def tool_call(name: str, **arguments) -> ChatMessage:
    return ChatMessage(role="assistant", tool_calls=[ToolCall.make(name, **arguments)])


def finish(result: str = "done", evidence: list[str] | None = None) -> ChatMessage:
    return tool_call("finish", result=result, evidence=evidence or [], reason="objective met")


class LookupArgs(ToolArgs):
    key: str = Field(description="What to look up.")


@pytest.fixture
def trace(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.script(CORE_SCHEMA.read_text(encoding="utf-8"))
    yield AgentTrace(db)
    db.close()


def make_spec(handler=None, **kwargs) -> AgentSpec:
    async def lookup(args: LookupArgs, run: RunContext) -> dict:
        run.state.setdefault("calls", []).append(args.key)
        return {"key": args.key, "value": 42}

    tool = Tool("lookup", "Look up a value.", LookupArgs, handler or lookup)
    return AgentSpec(name="test.agent", feature="test", system_prompt="Objective: X.", tools=[tool], **kwargs)


def runner(llm, trace, **kwargs) -> AgentRunner:
    return AgentRunner(llm, trace, **kwargs)


def tool_messages(request: ChatRequest) -> list[dict]:
    return [json.loads(m.content) for m in request.messages if m.role == "tool"]


async def test_runs_tools_until_finish_and_logs_every_step(trace):
    llm = ScriptedLLM(tool_call("lookup", key="a", reason="need a"), finish("a is 42", ["value 42"]))
    state: dict = {}

    result = await runner(llm, trace).run(make_spec(), objective="Find a.", state=state)

    assert (result.status, result.result, result.evidence) == ("finished", "a is 42", ["value 42"])
    assert state["calls"] == ["a"]
    # Al doilea apel vede rezultatul uneltei; lista de unelte include mereu finish.
    assert tool_messages(llm.requests[1]) == [{"key": "a", "value": 42}]
    assert [t.name for t in llm.requests[0].tools] == ["lookup", "finish"]

    run = trace.get_run(result.run_id)
    assert (run["status"], run["steps"], run["model"]) == ("finished", 2, "test-model")
    assert (run["prompt_tokens"], run["completion_tokens"]) == (200, 20)
    assert run["cost"] == pytest.approx(0.004)
    assert [(s["kind"], s["tool_name"]) for s in run["steps_detail"]] == [
        ("llm", None), ("tool", "lookup"), ("llm", None), ("finish", "finish"),
    ]  # fmt: skip
    tool_step = run["steps_detail"][1]
    assert tool_step["reason"] == "need a"
    assert tool_step["arguments"] == {"key": "a", "reason": "need a"}
    assert tool_step["result"] == {"key": "a", "value": 42}
    assert run["steps_detail"][0]["messages"][0]["role"] == "system"  # mesajele trimise modelului


async def test_tool_errors_are_returned_to_the_model_not_hidden(trace):
    async def failing(args: LookupArgs, run: RunContext) -> dict:
        raise LookupError("calendar is down")

    llm = ScriptedLLM(tool_call("lookup", key="a", reason="try"), finish("gave up"))
    result = await runner(llm, trace).run(make_spec(failing), objective="Find a.")

    assert result.status == "finished"
    assert tool_messages(llm.requests[1]) == [{"error": "LookupError: calendar is down"}]
    assert trace.get_run(result.run_id)["steps_detail"][1]["is_error"] is True


async def test_invalid_calls_are_returned_to_the_model(trace):
    llm = ScriptedLLM(
        tool_call("lookup", key="a"),  # lipsește `reason`
        tool_call("nope", reason="guessing"),  # unealtă inexistentă
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="c1", name="lookup", arguments="{broken")]),
        tool_call("finish", reason="forgot the result"),  # finish invalid: bucla continuă
        finish(),
    )
    state: dict = {}
    result = await runner(llm, trace).run(make_spec(), objective="Find a.", state=state)

    assert result.status == "finished" and result.steps == 5
    assert "calls" not in state  # niciun apel invalid nu a ajuns la unealtă
    errors = tool_messages(llm.requests[4])
    assert errors[0]["error"] == "invalid arguments" and "reason" in errors[0]["details"][0]
    assert "unknown tool 'nope'" in errors[1]["error"] and "lookup" in errors[1]["error"]
    assert "not valid JSON" in errors[2]["error"]
    assert errors[3]["error"] == "invalid arguments"


async def test_text_without_a_tool_call_is_nudged(trace):
    llm = ScriptedLLM(ChatMessage(role="assistant", content="I think it's 42."), finish())
    result = await runner(llm, trace).run(make_spec(), objective="Find a.")

    assert result.status == "finished"
    last = llm.requests[1].messages[-1]
    assert last.role == "user" and "finish(result, evidence)" in last.content


async def test_step_limit_ends_the_run(trace):
    llm = ScriptedLLM(*[tool_call("lookup", key=str(i), reason="again") for i in range(3)])
    result = await runner(llm, trace).run(make_spec(max_steps=3), objective="Loop.")

    assert (result.status, result.steps) == ("max_steps", 3)
    assert trace.get_run(result.run_id)["status"] == "max_steps"


async def test_llm_failure_and_timeout_end_the_run_with_an_error(trace):
    failed = await runner(ScriptedLLM(RuntimeError("503 from provider")), trace).run(
        make_spec(), objective="x"
    )
    assert failed.status == "error" and "503" in failed.error

    class HangingLLM:
        name = "hanging"

        async def chat(self, request: ChatRequest) -> ChatResponse:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    timed_out = await runner(HangingLLM(), trace, llm_timeout_s=0.05).run(make_spec(), objective="x")
    assert timed_out.status == "error" and "timed out" in timed_out.error


def test_every_tool_must_have_a_mandatory_reason():
    class NoReason(BaseModel):
        key: str

    async def handler(args, run):
        return {}

    with pytest.raises(TypeError, match="reason"):
        Tool("bad", "No reason parameter.", NoReason, handler)  # type: ignore[arg-type]

    schema = Tool("ok", "Has reason.", LookupArgs, handler).spec().parameters
    assert "reason" in schema["required"] and schema["additionalProperties"] is False
    with pytest.raises(ValueError):
        AgentSpec(name="x", feature="x", system_prompt="", tools=[Tool("finish", "", LookupArgs, handler)])


async def test_reasoning_details_are_sent_back_unchanged(trace):
    details = [{"type": "reasoning.encrypted", "data": "abc", "id": "r1", "format": "x", "index": 0}]
    first = tool_call("lookup", key="a", reason="need a")
    first.reasoning_details = details
    llm = ScriptedLLM(first, finish())

    await runner(llm, trace).run(make_spec(), objective="Find a.")

    assistant = next(m for m in llm.requests[1].messages if m.role == "assistant")
    assert assistant.reasoning_details == details


async def test_long_tool_output_is_truncated(trace):
    async def verbose(args: LookupArgs, run: RunContext) -> dict:
        return {"blob": "x" * 5000}

    llm = ScriptedLLM(tool_call("lookup", key="a", reason="need a"), finish())
    await runner(llm, trace, tool_output_max_chars=200).run(make_spec(verbose), objective="Find a.")

    content = next(m.content for m in llm.requests[1].messages if m.role == "tool")
    assert len(content) < 260 and "truncated" in content


# --- Fereastra glisantă -------------------------------------------------------------


def history(rounds: int) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content="S"), ChatMessage(role="user", content="Objective")]
    for i in range(1, rounds + 1):
        call = ToolCall(id=f"call_{i}", name="lookup", arguments=json.dumps({"key": str(i), "reason": "r"}))
        messages.append(ChatMessage(role="assistant", tool_calls=[call]))
        messages.append(
            ChatMessage(role="tool", tool_call_id=call.id, name="lookup", content=json.dumps({"value": i}))
        )
    return messages


def test_window_keeps_system_objective_and_last_rounds():
    view = build_context_view(history(6), keep_last_rounds=2)

    assert [m.role for m in view] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert view[0].content == "S" and view[1].content.startswith("Objective")
    # Rundele vechi devin o notă scurtă, atașată obiectivului.
    assert 'step 1: lookup -> {"value": 1}' in view[1].content
    assert 'step 4: lookup -> {"value": 4}' in view[1].content
    assert "step 5" not in view[1].content


def test_window_never_separates_a_tool_call_from_its_result():
    for keep in range(1, 7):
        view = build_context_view(history(6), keep_last_rounds=keep)
        calls = {c.id for m in view if m.role == "assistant" for c in m.tool_calls}
        results = {m.tool_call_id for m in view if m.role == "tool"}
        assert calls == results and len(calls) == keep


def test_short_history_is_sent_unchanged():
    messages = history(2)
    assert build_context_view(messages, keep_last_rounds=6) == messages
    assert truncate_text("short", 100) == "short"
