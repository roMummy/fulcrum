"""统一本群触发词与业务命令，转换只作用于当前插件的内存消息视图。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.entities import Message


def command_overrides(config: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """读取经过设置页校验的映射；无效形态不能扩大匹配范围。"""

    raw = config.get('command_overrides')
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(command): tuple(dict.fromkeys(word.strip() for word in words if isinstance(word, str) and word.strip()))
        for command, words in raw.items()
        if isinstance(command, str) and isinstance(words, (list, tuple)) and words
    }


def command_triggers(commands: Iterable[str], config: Mapping[str, Any]) -> tuple[str, ...]:
    """自定义词完整替换对应正式命令；保留旧词必须由用户在设置中明确填写。"""

    overrides = command_overrides(config)
    return tuple(dict.fromkeys(word for command in commands for word in (overrides.get(command) or (command,))))


def canonical_command_text(content: str, config: Mapping[str, Any]) -> str:
    """最长触发词优先，只替换命令部分，原样保留命令参数、段落及支持的前缀。"""

    aliases = [(word, command) for command, words in command_overrides(config).items() for word in words]
    stripped = str(content or '').lstrip()
    leading = str(content or '')[:len(str(content or '')) - len(stripped)]
    prefix = stripped[:1] if stripped.startswith(('/', '#')) else ''
    body = stripped[len(prefix):]
    command_body = body.lstrip() if prefix else body
    prefix_space = body[:len(body) - len(command_body)]
    for word, command in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if not command_body.casefold().startswith(word.casefold()):
            continue
        suffix = command_body[len(word):]
        # 无前缀只接受独立命令，避免普通聊天里的“打卡成功”触发签到。
        # 显式 /、# 沿用平台支持的紧接参数形式，例如“/天气上海”。
        if suffix and not suffix[0].isspace() and not prefix:
            continue
        return leading + prefix + prefix_space + command + suffix
    return content


@contextmanager
def effective_message_content(db: Session, message_id: int | None, content: str | None):
    """在单次插件执行期间提供正式命令，flush/commit 均不改写已保存的用户原文。

    AI 改写与本群指令共用这一条路径。set_committed_value 不产生 ORM 脏字段，
    即使签到插件内部提交积分事务，也不会将“签到”写回用户原本发送的“打卡”。
    finally 同时保护正常返回、业务异常及嵌套 AI 调度后的会话内存状态。
    """

    message = db.get(Message, int(message_id)) if message_id and content else None
    if message is None:
        yield
        return
    original = message.content
    set_committed_value(message, 'content', content)
    try:
        yield
    finally:
        set_committed_value(message, 'content', original)
