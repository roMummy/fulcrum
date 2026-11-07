from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class PluginExecutionContext:
    """由平台可信执行链路创建的插件身份和消息定位信息。"""

    tenant_id: int
    binding_id: int
    bot_id: int
    channel_id: int | None = None
    conversation_id: int | None = None
    message_id: int | None = None
    raw_event_id: int | None = None
    sender_id: str | None = None
    channel_type: str | None = None
    chat_type: str | None = None
    message_type: str | None = None


@dataclass(frozen=True)
class PluginExecutionRequest:
    action: str
    config: Mapping[str, Any]
    payload: Mapping[str, Any] = field(default_factory=dict)
    context: PluginExecutionContext | None = None


@dataclass(frozen=True)
class PluginExecutionResult:
    ok: bool
    status: str
    message: str
    output: Mapping[str, Any] = field(default_factory=dict)


class PluginExecutor(Protocol):
    def execute(self, request: PluginExecutionRequest) -> PluginExecutionResult:
        ...
