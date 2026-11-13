"""Ținerea contextului sub control: trunchiere și fereastră glisantă.

O „rundă” este un mesaj al asistentului împreună cu tot ce îi urmează până la următorul mesaj
al asistentului (rezultatele uneltelor apelate). Rundele se păstrează sau se scot întregi,
deci un `tool_call` nu rămâne niciodată fără răspunsul lui.
"""

from __future__ import annotations

from app.ports.llm import ChatMessage

_HEAD = 2  # system prompt + obiectivul


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}… [truncated {len(text) - max_chars} characters]"


def split_rounds(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    rounds: list[list[ChatMessage]] = []
    for message in messages:
        if message.role == "assistant" or not rounds:
            rounds.append([message])
        else:
            rounds[-1].append(message)
    return rounds


def summarize_round(index: int, round_messages: list[ChatMessage]) -> str:
    """Nota scurtă care înlocuiește o rundă veche."""
    results = {m.tool_call_id: m.content or "" for m in round_messages if m.role == "tool"}
    parts: list[str] = []
    for message in round_messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            outcome = " ".join(results.get(call.id, "").split())
            parts.append(f"{call.name} -> {truncate_text(outcome, 160)}")
        if not message.tool_calls and message.content:
            parts.append(f"said: {truncate_text(' '.join(message.content.split()), 120)}")
    return f"step {index}: " + ("; ".join(parts) if parts else "(no tool call)")


def build_context_view(messages: list[ChatMessage], keep_last_rounds: int) -> list[ChatMessage]:
    """Ce trimitem modelului: system + obiectiv (cu nota rundelor vechi) + ultimele N runde."""
    head, rest = messages[:_HEAD], messages[_HEAD:]
    rounds = split_rounds(rest)
    if len(rounds) <= keep_last_rounds or len(head) < _HEAD:
        return list(messages)

    old, recent = rounds[:-keep_last_rounds], rounds[-keep_last_rounds:]
    note = "\n".join(summarize_round(i + 1, r) for i, r in enumerate(old))
    objective = head[1].model_copy(
        update={
            "content": (
                f"{head[1].content or ''}\n\n"
                f"[Earlier steps, summarized to save context]\n{note}"
            )
        }
    )
    return [head[0], objective, *[m for r in recent for m in r]]
