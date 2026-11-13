"""Portul pentru LLM. Formatul mesajelor urmează convenția OpenAI chat-completions."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.core.ids import new_id


class LLMError(RuntimeError):
    """Eroare la apelul modelului (rețea, timeout, răspuns invalid)."""


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str = "{}"  # JSON brut, exact cum l-a produs modelul

    @classmethod
    def make(cls, name: str, **arguments: Any) -> ToolCall:
        return cls(
            id=new_id("call"), name=name, arguments=json.dumps(arguments, ensure_ascii=False)
        )


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # doar pentru role="tool"
    name: str | None = None  # numele uneltei, doar pentru role="tool"
    # Detaliile de reasoning se retrimit nemodificate între rundele de tool calling.
    reasoning_details: list[dict[str, Any]] | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0  # în creditele providerului (OpenRouter: `usage.cost`)

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost += other.cost


class ChatRequest(BaseModel):
    purpose: str  # numele agentului, ex. "f3_guest_copilot.reply"; folosit la logare și de mock
    messages: list[ChatMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    model_role: Literal["decision", "utility"] = "decision"
    temperature: float | None = None
    max_tokens: int | None = None


class ChatResponse(BaseModel):
    message: ChatMessage
    usage: Usage = Field(default_factory=Usage)
    model: str
    finish_reason: str | None = None


@runtime_checkable
class LLMPort(Protocol):
    name: str

    async def chat(self, request: ChatRequest) -> ChatResponse: ...


# Comportamentul determinist al LLM-ului mock pentru un agent: primește cererea și întoarce
# mesajul asistentului. Fiecare feature își înregistrează politica prin `FeatureContext`.
MockPolicy = Callable[[ChatRequest], ChatMessage]
