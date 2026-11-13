"""账号清单和详情页共用的在线状态，只读取已有运行快照。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel


def build_account_presence(channel: Channel, *, now: datetime | None = None) -> dict[str, object]:
    current = ensure_shanghai_datetime(now or now_shanghai())

    def timestamp(value) -> datetime | None:
        if not value:
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed = ensure_shanghai_datetime(parsed)
        except (TypeError, ValueError, OverflowError):
            return None
        # 忽略明显错误的未来时间，避免一个坏快照让账号长期显示在线。
        return parsed if parsed <= current + timedelta(minutes=1) else None

    try:
        runtime = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError):
        runtime = {}
    runtime = runtime if isinstance(runtime, dict) else {}
    bridge = runtime.get("webridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    telegram = runtime.get("telegram_polling")
    telegram = telegram if isinstance(telegram, dict) else {}

    activity = [channel.last_login_at, channel.last_message_sync_at]
    # iPad 检查失败也会写心跳时间，不能把失败时间当作在线证据。
    if runtime.get("heartbeat_status") != "failed":
        activity.append(channel.last_heartbeat_at)
    timeout = timedelta(minutes=2)
    reported_offline = False
    reported_unknown = False
    if channel.channel_type == "wechat_webridge":
        # 微桥的轮询存活时间存在独立快照里，通用时间字段通常为空。
        activity = [bridge.get("last_seen_at")]
        reported_offline = bridge.get("online") is False
    elif channel.channel_type == "wechat_ipad":
        timeout = timedelta(minutes=10)
        # 同步失败也会写 last_message_sync_at；扫码期间还可能保留旧心跳。
        # iPad 只用成功登录/心跳，并要求登录态明确为 online。
        activity = [channel.last_login_at]
        if runtime.get("heartbeat_status") != "failed":
            activity.append(channel.last_heartbeat_at)
        reported_unknown = channel.login_state in {None, "", "unknown"}
        reported_offline = not reported_unknown and channel.login_state not in {"online", "reconnecting"}
    elif channel.channel_type == "telegram":
        if telegram.get("last_poll_ok") is True:
            activity.append(telegram.get("last_poll_at"))
    elif channel.channel_type == "workwechat" and channel.provider_account is not None:
        timeout = timedelta(minutes=5)
        account, node = channel.provider_account, channel.provider_node
        activity = [account.last_seen_at]
        node_seen_at = timestamp(node.last_heartbeat_at) if node else None
        reported_offline = bool(
            account.status == "offline" or node is None or node.status != "active"
            or (node_seen_at is not None and current - node_seen_at > timeout)
        )
        reported_unknown = account.status != "online" or node_seen_at is None

    times = [parsed for value in activity if (parsed := timestamp(value)) is not None]
    last_active_at = max(times, default=None)
    state, label, tone = "unknown", "状态未知", "muted"
    hint = "尚未收到在线信息"
    if not channel.is_enabled:
        state, label = "disabled", "已停用"
        hint = "账号已停用，启用后可查看在线情况"
    elif channel.channel_type == "wechat_ipad" and channel.login_state == "reconnecting":
        state, label, tone = "reconnecting", "重连中", "warning"
        hint = "正在恢复连接，请稍后刷新"
    elif reported_offline:
        state, label, tone = "offline", "离线", "error"
        hint = "账号或设备已离线，请检查登录和连接"
    # 微桥和企微的通用 status 也会被配置检查改写，不能覆盖手机/客户端的实时上报。
    elif (channel.channel_type in {"wechat_ipad", "telegram"} and channel.status in {"degraded", "error", "failed"}) or (
        channel.channel_type == "telegram" and telegram.get("last_poll_ok") is False
    ):
        state, label, tone = "error", "连接异常", "warning"
        hint = "最近一次连接检查失败，请检查设备或重新登录"
    elif last_active_at is not None and not reported_unknown:
        if current - last_active_at <= timeout:
            state, label, tone = "online", "在线", "success"
            hint = "近期收到账号活动"
        else:
            state, label, tone = "offline", "离线", "error"
            hint = f"超过 {int(timeout.total_seconds() // 60)} 分钟未收到活动，请检查设备连接"

    activity_text = "暂无活跃记录"
    if last_active_at is not None:
        seconds = max(0, int((current - last_active_at).total_seconds()))
        if seconds < 60:
            activity_text = "刚刚活跃"
        elif seconds < 3600:
            activity_text = f"{seconds // 60} 分钟前活跃"
        elif seconds < 86400:
            activity_text = f"{seconds // 3600} 小时前活跃"
        else:
            activity_text = f"{seconds // 86400} 天前活跃"
    return {
        "state": state, "label": label, "tone": tone, "hint": hint,
        "last_active_at": last_active_at, "activity_text": activity_text,
    }
