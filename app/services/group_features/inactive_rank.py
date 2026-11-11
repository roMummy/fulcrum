"""潜水排行:群里发「潜水排行 [天数] [名数]」,按成员最后发言时间列出长期没发言的人。

原先是 inactive_rank 插件,现在属于群自身能力:数据完全来自本群成员表,
不依赖机器人绑定;默认天数和展示人数在群详情页「功能」里配置。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import ChatroomMember
from app.services.group_features.dispatch import FeatureContext, register_handler
from app.services.config_fields import ConfigField
from app.services.group_features.registry import GroupFeature, register
from app.services.plugin_runtime.replies import text_reply

# 命令里天数和名数的上限,防止群命令触发无界查询或超长回复。
MAX_DAYS = 365
MAX_LIMIT = 50

FEATURE = register(
    GroupFeature(
        key="inactive_rank",
        name="潜水排行",
        description=f"群成员发「潜水排行」查看谁最久没发言;可带参数,如「潜水排行 30 5」(天数最大 {MAX_DAYS},名数最大 {MAX_LIMIT})。",
        commands=("潜水排行",),
        fields=(
            ConfigField("default_days", "默认统计天数", kind="number", default=60, minimum=1, maximum=MAX_DAYS, group="默认参数", hint="多少天没发言算潜水"),
            ConfigField("default_limit", "默认展示人数", kind="number", default=10, minimum=1, maximum=MAX_LIMIT, group="默认参数", hint="榜单最多列出几人"),
        ),
    )
)


def handle(ctx: FeatureContext, args: str) -> list[dict[str, Any]]:
    """统计本群成员的潜水情况并生成排行文本。"""

    parameters = parse_parameters(args, default_days=int(ctx.config["default_days"]), default_limit=int(ctx.config["default_limit"]))
    if parameters is None:
        return [text_reply(f"命令格式：潜水排行 [1-{MAX_DAYS}天] [1-{MAX_LIMIT}名]")]
    days, limit = parameters

    current = now_shanghai()
    threshold = current - timedelta(days=days)
    members = ctx.db.scalars(
        select(ChatroomMember).where(
            ChatroomMember.tenant_id == int(ctx.chatroom.tenant_id),
            ChatroomMember.chatroom_id == int(ctx.chatroom.id),
            ChatroomMember.status != "left",
        )
    ).all()

    # 成员表已经由渠道同步和入站消息更新 last_active_at,这里只消费这份数据,不扫描消息表。
    ranking_rows: list[dict[str, Any]] = []
    for member in members:
        member_id = str(member.member_external_id or "").strip()
        if not member_id:
            continue
        last_active = ensure_shanghai_datetime(member.last_active_at) if member.last_active_at is not None else None
        if last_active is not None and last_active >= threshold:
            continue
        ranking_rows.append(
            {
                "name": str(member.display_name or member.nickname or member_id).strip() or member_id,
                "inactivity_days": MAX_DAYS + 1 if last_active is None else max(int((current - last_active).total_seconds() // 86_400), 0),
                "never_spoken": last_active is None,
            }
        )

    # 从未发言的排最前,其余按沉默天数从长到短,同天数按名字稳定排序。
    ranking_rows.sort(key=lambda item: (not item["never_spoken"], -int(item["inactivity_days"]), str(item["name"])))
    selected = ranking_rows[:limit]
    if not selected:
        return [text_reply(f"近 {days} 天没有符合条件的潜水成员。")]

    lines = [f"潜水排行（近 {days} 天未发言，前 {limit} 名）"]
    for index, item in enumerate(selected, start=1):
        # 只展示最后发言距今多久;没有记录的成员写“暂无发言记录”,不暗示统计起点。
        status = "暂无发言记录" if item["never_spoken"] else f"最后发言 {item['inactivity_days']} 天前"
        lines.append(f"{index}. {item['name']} - {status}")
    return [text_reply("\n".join(lines))]


def parse_parameters(args: str, *, default_days: int, default_limit: int) -> tuple[int, int] | None:
    """解析命令后的两个位置参数(天数、名数);格式不对返回 None。"""

    parts = [item for item in args.split() if item]
    if len(parts) > 2:
        return None
    try:
        days = int(parts[0]) if parts else default_days
        limit = int(parts[1]) if len(parts) == 2 else default_limit
    except ValueError:
        return None
    if days <= 0 or limit <= 0:
        return None
    return min(days, MAX_DAYS), min(limit, MAX_LIMIT)


register_handler(FEATURE, handle)
