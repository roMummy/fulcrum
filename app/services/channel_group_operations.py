from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom
from app.services.channel_adapters import get_channel_adapter
from app.services.plugin_runtime.contracts import PluginExecutionContext


def invite_group_members(
    db: Session,
    *,
    context: PluginExecutionContext,
    chatroom_id: int,
    member_external_ids: Iterable[str],
) -> dict[str, object]:
    """在可信租户、机器人和渠道范围内派发群成员邀请动作。"""

    if context.channel_id is None:
        return _error("trusted_channel_required", "缺少可信渠道上下文")
    channel = db.get(Channel, int(context.channel_id))
    chatroom = db.get(Chatroom, int(chatroom_id))
    if (
        channel is None
        or chatroom is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or int(chatroom.tenant_id) != int(context.tenant_id)
        or int(chatroom.bot_id) != int(context.bot_id)
        or int(chatroom.channel_id) != int(context.channel_id)
    ):
        # chatroom_id 来自插件配置，但最终授权仍以这里加载到的租户资源为准，
        # 即使配置被篡改也不能邀请到其他机器人或其他租户的群。
        return _error("chatroom_not_found", "目标群不属于当前机器人渠道")
    if not chatroom.is_active:
        return _error("chatroom_inactive", "目标群尚未启用管理")

    members = _normalize_member_ids(member_external_ids)
    if not members:
        return _error("member_required", "待邀请成员为空")
    adapter = get_channel_adapter(channel)
    return adapter.invite_group_members(
        channel,
        chatroom_external_id=str(chatroom.external_id),
        member_external_ids=members,
    )


def _normalize_member_ids(values: Iterable[str]) -> list[str]:
    """保持输入顺序去重，避免同一渠道动作重复邀请同一成员。"""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        member_id = str(value or "").strip()
        if not member_id or member_id in seen:
            continue
        seen.add(member_id)
        normalized.append(member_id)
    return normalized


def _error(error_code: str, error_message: str) -> dict[str, object]:
    return {
        "ok": False,
        "action": "invite_group_members",
        "status": "failed",
        "external_status": "failed",
        "error_code": error_code,
        "error_message": error_message,
    }
