"""账号(渠道)的逻辑删除:从清单隐藏、停止接收事件、释放占用的机器人额度,数据全部保留。

只允许删除未启用的账号。群、成员、会话、消息、日志、AI 记忆等历史数据都不动,
这样误删后可以由管理员在数据库里清空 deleted_at 恢复;额度绑定记录保留为已释放状态以便审计。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.models.saas import BotBinding

RELEASE_REASON = "租户删除账号"


class ChannelDeletionError(ValueError):
    """账号当前不能删除(如仍处于启用状态)。"""


def delete_channel_account(db: Session, channel: Channel) -> int:
    """逻辑删除账号,返回被释放的额度绑定数量。调用方负责提交事务。"""

    if channel.deleted_at is not None:
        return 0
    if channel.is_enabled:
        raise ChannelDeletionError("请先停用账号再删除")
    current = now_shanghai()
    channel.deleted_at = current
    channel.status = "deleted"
    # 账号令牌属于凭据,删除后不再保留可用的密文;其余运行信息保留,历史页面仍能展示昵称头像。
    channel.bot_token_encrypted = None
    channel.bot_token_masked = None
    released = 0
    for binding in db.scalars(
        select(BotBinding).where(BotBinding.channel_id == channel.id, BotBinding.status.in_(("active", "disabled")))
    ).all():
        # 释放额度:只统计 active/disabled,置为 released 即可立刻接入新账号。
        binding.status = "released"
        binding.released_at = current
        binding.release_reason = RELEASE_REASON
        released += 1
    db.flush()
    return released
