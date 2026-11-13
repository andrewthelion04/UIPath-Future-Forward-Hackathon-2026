"""LLM mock determinist: aplicația și testele rulează fără cheie API.

Nu conține logică de feature. Fiecare agent își înregistrează politica (o funcție
`ChatRequest -> ChatMessage`) prin `FeatureContext.register_mock_llm_policy`.
"""

from __future__ import annotations

from app.ports.llm import ChatMessage, ChatRequest, ChatResponse, MockPolicy, ToolCall, Usage

MODEL_NAME = "mock-deterministic"


def _estimate_tokens(messages: list[ChatMessage]) -> int:
    chars = sum(
        len(m.content or "") + sum(len(c.name) + len(c.arguments) for c in m.tool_calls)
        for m in messages
    )
    return max(1, chars // 4)


def _default_policy(request: ChatRequest) -> ChatMessage:
    """Fără politică înregistrată: încheie explicit, ca bucla să nu se învârtă în gol."""
    note = f"[mock LLM] No scripted policy is registered for '{request.purpose}'."
    if any(tool.name == "finish" for tool in request.tools):
        return ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall.make("finish", result=note, evidence=[], reason="No mock policy available.")
            ],
        )
    return ChatMessage(role="assistant", content=note)


class MockLLM:
    name = "mock"

    def __init__(self) -> None:
        self._policies: dict[str, MockPolicy] = {}

    def register_policy(self, purpose: str, policy: MockPolicy) -> None:
        self._policies[purpose] = policy

    async def chat(self, request: ChatRequest) -> ChatResponse:
        policy = self._policies.get(request.purpose, _default_policy)
        message = policy(request)
        usage = Usage(
            prompt_tokens=_estimate_tokens(request.messages),
            completion_tokens=_estimate_tokens([message]),
            cost=0.0,
        )
        return ChatResponse(
            message=message,
            usage=usage,
            model=MODEL_NAME,
            finish_reason="tool_calls" if message.tool_calls else "stop",
        )
