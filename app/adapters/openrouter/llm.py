"""Adaptorul LLM real: OpenRouter, printr-un client compatibil OpenAI (SDK-ul `openai`).

Verificat în documentația OpenRouter (noiembrie 2026):
- `reasoning_details` de pe mesajul asistentului se retrimite nemodificat în cererea următoare,
  altfel modelele cu reasoning pierd contextul între rundele de tool calling;
- `usage.cost` vine automat în răspuns (în creditele OpenRouter), fără parametri în plus.

Neverificat live: adaptorul nu a fost rulat cu o cheie reală.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai

from app.ports.llm import ChatMessage, ChatRequest, ChatResponse, LLMError, ToolCall, Usage


def to_wire(message: ChatMessage) -> dict[str, Any]:
    """Mesajul nostru -> formatul chat-completions."""
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content or "",
        }
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant":
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        if message.reasoning_details:
            wire["reasoning_details"] = message.reasoning_details  # exact cum a venit
    return wire


class OpenRouterLLM:
    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        utility_model: str | None = None,
        timeout_s: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._utility_model = utility_model or model
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,  # timeout explicit pe fiecare apel
            max_retries=1,
            default_headers={"X-Title": "StayPilot AI"},
            http_client=http_client,
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        model = self._utility_model if request.model_role == "utility" else self._model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [to_wire(message) for message in request.messages],
        }
        if request.tools:
            # Lista de unelte trebuie trimisă la fiecare cerere, nu doar la prima.
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens

        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except openai.OpenAIError as exc:
            raise LLMError(f"OpenRouter call failed ({model}): {exc}") from exc

        if not completion.choices:
            error = (completion.model_extra or {}).get("error")
            raise LLMError(f"OpenRouter returned no choices ({model}): {error}")
        choice = completion.choices[0]
        message = choice.message

        tool_calls = []
        for call in message.tool_calls or []:
            function = getattr(call, "function", None)
            if function is not None:
                tool_calls.append(
                    ToolCall(id=call.id, name=function.name, arguments=function.arguments or "{}")
                )
        usage = completion.usage
        cost = (usage.model_extra or {}).get("cost") if usage is not None else None
        return ChatResponse(
            message=ChatMessage(
                role="assistant",
                content=message.content,
                tool_calls=tool_calls,
                reasoning_details=(message.model_extra or {}).get("reasoning_details") or None,
            ),
            usage=Usage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                cost=float(cost or 0.0),
            ),
            model=completion.model or model,
            finish_reason=choice.finish_reason,
        )

    async def aclose(self) -> None:
        await self._client.close()
