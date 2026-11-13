"""Ajutoare pentru politicile deterministe ale LLM-ului mock.

O politică primește `ChatRequest` și decide următorul mesaj al asistentului uitându-se la ce
unelte au fost deja apelate și la rezultatele lor, la fel cum ar face un model real.
"""

from __future__ import annotations

import json
from typing import Any

from app.ports.llm import ChatMessage, ChatRequest, ToolCall


def objective_text(request: ChatRequest) -> str:
    """Primul mesaj `user` (obiectivul rulării)."""
    for message in request.messages:
        if message.role == "user":
            return message.content or ""
    return ""


def called_tools(request: ChatRequest) -> list[str]:
    return [
        call.name
        for message in request.messages
        if message.role == "assistant"
        for call in message.tool_calls
    ]


def tool_results(request: ChatRequest, name: str) -> list[Any]:
    """Rezultatele (JSON decodat, dacă se poate) ale apelurilor unei unelte, în ordine."""
    results: list[Any] = []
    for message in request.messages:
        if message.role == "tool" and message.name == name:
            try:
                results.append(json.loads(message.content or "null"))
            except json.JSONDecodeError:
                results.append(message.content)
    return results


def last_result(request: ChatRequest, name: str) -> Any:
    results = tool_results(request, name)
    return results[-1] if results else None


def failed(result: Any) -> bool:
    return isinstance(result, dict) and "error" in result


def call(name: str, **arguments: Any) -> ChatMessage:
    """Mesaj de asistent cu un singur `tool_call`."""
    return ChatMessage(role="assistant", tool_calls=[ToolCall.make(name, **arguments)])


def finish(result: str, evidence: list[str] | None = None, reason: str = "Objective met.") -> ChatMessage:
    return call("finish", result=result, evidence=evidence or [], reason=reason)
