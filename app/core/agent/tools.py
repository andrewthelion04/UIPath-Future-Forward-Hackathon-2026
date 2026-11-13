"""Definirea uneltelor unui agent.

Fiecare unealtă are nume, descriere și un model Pydantic al parametrilor. Modelul moștenește
`ToolArgs`, deci parametrul `reason` este obligatoriu pentru orice unealtă.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.ports.llm import ToolSpec


class ToolArgs(BaseModel):
    """Baza parametrilor oricărei unelte: `reason` e obligatoriu."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ..., min_length=3, description="Why you are calling this tool now, in one sentence."
    )


@dataclass
class RunContext:
    """Starea unei rulări, pusă la dispoziția uneltelor de feature-ul care pornește agentul."""

    run_id: str
    agent: str
    state: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[Any, RunContext], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[ToolArgs]
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not (isinstance(self.args_model, type) and issubclass(self.args_model, ToolArgs)):
            raise TypeError(
                f"Tool '{self.name}': args_model must inherit ToolArgs (mandatory `reason`)"
            )

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=json_schema(self.args_model),
        )


class FinishArgs(ToolArgs):
    result: str = Field(..., min_length=1, description="The final outcome, stated plainly.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Facts taken from tool results that support the outcome.",
    )


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Schema JSON a parametrilor, fără titlurile generate de Pydantic."""
    schema = model.model_json_schema()
    _strip_titles(schema)
    return schema


def _strip_titles(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


FINISH_TOOL_NAME = "finish"
FINISH_SPEC = ToolSpec(
    name=FINISH_TOOL_NAME,
    description=(
        "Call this exactly once, when the objective is met or cannot be met. "
        "It ends the run. Nothing else ends it."
    ),
    parameters=json_schema(FinishArgs),
)
