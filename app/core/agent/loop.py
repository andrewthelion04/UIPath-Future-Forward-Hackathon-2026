"""Bucla agentului, scrisă de mână și comună tuturor feature-urilor.

1. Trimitem modelului system promptul, istoricul și lista de unelte.
2. Modelul răspunde cu text sau cu `tool_call`-uri.
3. Codul nostru execută uneltele; rezultatul (sau eroarea) intră în istoric ca `role: tool`.
4. Repetăm până când modelul apelează `finish(result, evidence)` sau se atinge limita de pași.

Ordinea uneltelor nu e scrisă nicăieri: o alege modelul. Feature-urile definesc doar
promptul și uneltele (`AgentSpec`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.agent.context import build_context_view, truncate_text
from app.core.agent.tools import (
    FINISH_SPEC,
    FINISH_TOOL_NAME,
    FinishArgs,
    RunContext,
    Tool,
)
from app.core.agent.trace import AgentTrace
from app.core.db import to_json
from app.ports.llm import ChatMessage, ChatRequest, LLMPort, ToolCall, Usage

logger = logging.getLogger("staypilot.agent")

NUDGE = (
    "You replied without calling a tool. Call the next tool you need, or call "
    "finish(result, evidence) if the objective is met or cannot be met."
)


@dataclass
class AgentSpec:
    name: str  # ex. "f3_guest_copilot.reply"; apare în timeline și selectează politica mock
    feature: str
    system_prompt: str  # obiectivul și condiția de terminare
    tools: list[Tool] = field(default_factory=list)
    max_steps: int | None = None
    model_role: Literal["decision", "utility"] = "decision"

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if FINISH_TOOL_NAME in names:
            raise ValueError("'finish' is built in; do not define it as a feature tool")
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate tool names in agent '{self.name}'")


class AgentResult(BaseModel):
    run_id: str
    status: Literal["finished", "max_steps", "error"]
    result: str | None = None
    evidence: list[str] = Field(default_factory=list)
    error: str | None = None
    steps: int = 0
    usage: Usage = Field(default_factory=Usage)


@dataclass
class _Outcome:
    result: Any
    is_error: bool
    arguments: Any
    reason: str | None
    finish: FinishArgs | None = None


class AgentRunner:
    def __init__(
        self,
        llm: LLMPort,
        trace: AgentTrace,
        *,
        max_steps: int = 12,
        keep_last_rounds: int = 6,
        tool_output_max_chars: int = 3000,
        llm_timeout_s: float = 60.0,
    ) -> None:
        self._llm = llm
        self._trace = trace
        self._max_steps = max_steps
        self._keep_last_rounds = keep_last_rounds
        self._tool_output_max_chars = tool_output_max_chars
        self._llm_timeout_s = llm_timeout_s

    async def run(
        self,
        spec: AgentSpec,
        *,
        objective: str,
        state: dict[str, Any] | None = None,
        booking_id: str | None = None,
        property_id: str | None = None,
    ) -> AgentResult:
        run_id = self._trace.start_run(
            agent=spec.name,
            feature=spec.feature,
            objective=objective,
            booking_id=booking_id,
            property_id=property_id,
        )
        ctx = RunContext(run_id=run_id, agent=spec.name, state=state if state is not None else {})
        tools = {tool.name: tool for tool in spec.tools}
        tool_specs = [tool.spec() for tool in spec.tools] + [FINISH_SPEC]
        # Istoricul complet îl ținem noi; modelul nu are memorie între apeluri.
        messages = [
            ChatMessage(role="system", content=spec.system_prompt),
            ChatMessage(role="user", content=objective),
        ]
        usage, model_name, steps = Usage(), None, 0
        max_steps = spec.max_steps or self._max_steps

        def done(status: str, **kwargs: Any) -> AgentResult:
            self._trace.finish_run(
                run_id, status=status, steps=steps, usage=usage, model=model_name, **kwargs
            )
            return AgentResult(run_id=run_id, status=status, steps=steps, usage=usage, **kwargs)

        for _ in range(max_steps):
            steps += 1  # citit de `done()` la încheiere
            view = build_context_view(messages, self._keep_last_rounds)
            started = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self._llm.chat(
                        ChatRequest(
                            purpose=spec.name,
                            messages=view,
                            tools=tool_specs,
                            model_role=spec.model_role,
                        )
                    ),
                    timeout=self._llm_timeout_s,
                )
            except Exception as exc:
                error = (
                    f"LLM call timed out after {self._llm_timeout_s:g}s"
                    if isinstance(exc, TimeoutError)
                    else f"{type(exc).__name__}: {exc}"
                )
                logger.warning("Agent %s: %s", spec.name, error)
                self._trace.add_step(run_id, kind="error", content=error, is_error=True)
                return done("error", error=error)

            usage.add(response.usage)
            model_name = response.model
            assistant = response.message
            messages.append(assistant)
            self._trace.add_step(
                run_id,
                kind="llm",
                messages=view,
                content=assistant.content,
                arguments=[call.model_dump() for call in assistant.tool_calls] or None,
                model=response.model,
                usage=response.usage,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

            if not assistant.tool_calls:
                messages.append(ChatMessage(role="user", content=NUDGE))
                self._trace.add_step(run_id, kind="nudge", content=NUDGE)
                continue

            finished: FinishArgs | None = None
            for call in assistant.tool_calls:
                started = time.perf_counter()
                outcome = await self._execute(call, tools, ctx)
                content = truncate_text(to_json(outcome.result), self._tool_output_max_chars)
                messages.append(
                    ChatMessage(role="tool", tool_call_id=call.id, name=call.name, content=content)
                )
                self._trace.add_step(
                    run_id,
                    kind="finish" if outcome.finish else "tool",
                    tool_name=call.name,
                    reason=outcome.reason,
                    arguments=outcome.arguments,
                    result=outcome.result,
                    is_error=outcome.is_error,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
                finished = finished or outcome.finish
            if finished is not None:
                return done("finished", result=finished.result, evidence=finished.evidence)

        return done("max_steps", error=f"step limit reached ({max_steps}) without finish()")

    async def _execute(self, call: ToolCall, tools: dict[str, Tool], ctx: RunContext) -> _Outcome:
        """Execută un apel de unealtă. Orice eroare se întoarce modelului, nu se ascunde."""
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return _Outcome({"error": f"arguments are not valid JSON: {exc}"}, True, None, None)
        if not isinstance(raw, dict):
            return _Outcome({"error": "arguments must be a JSON object"}, True, raw, None)
        reason = raw.get("reason") if isinstance(raw.get("reason"), str) else None

        if call.name == FINISH_TOOL_NAME:
            try:
                finish = FinishArgs.model_validate(raw)
            except ValidationError as exc:
                return _Outcome(_validation_error(exc), True, raw, reason)
            return _Outcome({"status": "finished"}, False, raw, reason, finish)

        tool = tools.get(call.name)
        if tool is None:
            available = ", ".join([*tools, FINISH_TOOL_NAME])
            return _Outcome(
                {"error": f"unknown tool '{call.name}'. Available tools: {available}"},
                True,
                raw,
                reason,
            )
        try:
            args = tool.args_model.model_validate(raw)
        except ValidationError as exc:
            return _Outcome(_validation_error(exc), True, raw, reason)
        try:
            result = await tool.handler(args, ctx)
        except Exception as exc:
            logger.info("Tool %s failed: %s", call.name, exc)
            return _Outcome({"error": f"{type(exc).__name__}: {exc}"}, True, raw, reason)
        return _Outcome(result, False, raw, reason)


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    problems = [
        f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors()
    ]
    return {"error": "invalid arguments", "details": problems}
