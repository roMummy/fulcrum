"""群成员变更、潜水排行从插件收归群功能:把已启用的绑定转成对应群的功能开关。

Revision ID: 20261110_0026
Revises: 20261109_0025
Create Date: 2026-11-10 12:00:00
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261110_0026"
down_revision: Union[str, Sequence[str], None] = "20261109_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 旧插件代码 -> 群功能 key。群功能配置存在 chatrooms.extra_json 的 group_features 键下。
PLUGIN_TO_FEATURE = {"group_member_change": "member_change", "inactive_rank": "inactive_rank"}


def _clamp(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _feature_config(feature: str, binding_config: dict) -> dict:
    """成员变更按新默认(三类通知全开、不发卡片);潜水排行沿用绑定里配置过的默认天数和人数。"""

    if feature == "inactive_rank":
        return {
            "default_days": _clamp(binding_config.get("default_days"), 60, 1, 365),
            "default_limit": _clamp(binding_config.get("default_limit"), 10, 1, 50),
        }
    return {}


def upgrade() -> None:
    """只为“已启用管理”的群开启;群里单独停用过该插件的(作用域 enabled=0)保持关闭。

    旧插件目录删除后,启动同步会清掉 plugins 行及其绑定,所以必须在这里先把开关转过来。
    """

    bind = op.get_bind()
    bindings = bind.execute(
        sa.text(
            "SELECT bp.id, bp.bot_id, bp.config_json, p.code FROM bot_plugins bp "
            "JOIN plugins p ON p.id = bp.plugin_id "
            "WHERE p.code IN ('group_member_change', 'inactive_rank') AND bp.status = 'enabled'"
        )
    ).fetchall()
    for binding_id, bot_id, config_json, code in bindings:
        feature = PLUGIN_TO_FEATURE[code]
        try:
            binding_config = json.loads(config_json or "{}")
        except ValueError:
            binding_config = {}
        if not isinstance(binding_config, dict):
            binding_config = {}
        rooms = bind.execute(
            sa.text(
                "SELECT c.id, c.extra_json FROM chatrooms c JOIN channels ch ON ch.id = c.channel_id "
                "WHERE ch.bot_id = :bot_id AND c.is_active = 1"
            ),
            {"bot_id": bot_id},
        ).fetchall()
        for room_id, extra_json in rooms:
            scope = bind.execute(
                sa.text("SELECT enabled FROM group_plugin_scopes WHERE chatroom_id = :room AND bot_plugin_id = :bp"),
                {"room": room_id, "bp": binding_id},
            ).first()
            if scope is not None and not scope[0]:
                continue
            try:
                extra = json.loads(extra_json or "{}")
            except ValueError:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            features = extra.get("group_features") if isinstance(extra.get("group_features"), dict) else {}
            if feature in features:
                # 群里已经手动配置过(重复执行迁移时),不覆盖。
                continue
            entry = {"enabled": True}
            config = _feature_config(feature, binding_config)
            if config:
                entry["config"] = config
            features[feature] = entry
            extra["group_features"] = features
            bind.execute(
                sa.text("UPDATE chatrooms SET extra_json = :extra WHERE id = :id"),
                {"extra": json.dumps(extra, ensure_ascii=False, sort_keys=True), "id": room_id},
            )


    # 旧插件目录删除后,启动同步会删除 plugins 行并级联删除绑定;但执行日志和小时统计
    # 通过外键指向它们,不先清掉会让同步失败、应用停在“数据库未就绪”(readyz 503)。
    # 这些只是已下线插件的历史记录,随插件一起清理。
    for table in ("plugin_execution_logs", "plugin_activity_hourly"):
        bind.execute(
            sa.text(
                f"DELETE FROM {table} WHERE plugin_id IN "
                "(SELECT id FROM plugins WHERE code IN ('group_member_change', 'inactive_rank'))"
            )
        )


def downgrade() -> None:
    """旧插件目录已删除,无法还原绑定;群功能配置留在 extra_json 里不影响旧版本运行。"""
