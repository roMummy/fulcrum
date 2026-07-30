from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class PluginExecutionRequest:
    action: str
    config: Mapping[str, Any]
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginExecutionResult:
    ok: bool
    status: str
    message: str
    output: Mapping[str, Any] = field(default_factory=dict)


class PluginExecutor(Protocol):
    def execute(self, request: PluginExecutionRequest) -> PluginExecutionResult:
        ...
