"""迁移斗鱼订阅键并删除绑定配置中的运行状态。

Revision ID: 20260831_0015
Revises: 20260831_0014
Create Date: 2026-08-31 23:58:00
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0015"
down_revision: Union[str, Sequence[str], None] = "20260831_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUBSCRIPTION_FIELDS = ("room_subscriptions", "yuba_subscriptions", "reminder_subscribers")
REMOVED_RUNTIME_FIELDS = (
    "room_states",
    "yuba_last_feed_ids",
    "push_all_channels",
    "default_timeout",
    "default_feed_limit",
)


def upgrade() -> None:
    """把 external_id 订阅键转换为会话主键，并彻底移除旧运行状态字段。"""

    connection = op.get_bind()
    bot_plugins = sa.table(
        "bot_plugins",
        sa.column("id", sa.Integer()),
        sa.column("bot_id", sa.Integer()),
        sa.column("plugin_id", sa.Integer()),
        sa.column("config_json", sa.Text()),
    )
    plugins = sa.table(
        "plugins",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String(length=100)),
    )
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Integer()),
        sa.column("bot_id", sa.Integer()),
        sa.column("external_id", sa.String(length=128)),
    )

    douyu_plugin_id = connection.execute(
        sa.select(plugins.c.id).where(plugins.c.code == "douyu")
    ).scalar_one_or_none()
    if douyu_plugin_id is None:
        return

    rows = list(
        connection.execute(
            sa.select(bot_plugins.c.id, bot_plugins.c.bot_id, bot_plugins.c.config_json).where(
                bot_plugins.c.plugin_id == int(douyu_plugin_id)
            )
        ).mappings()
    )
    for row in rows:
        config = _parse_object(row["config_json"])
        conversation_rows = list(
            connection.execute(
                sa.select(conversations.c.id, conversations.c.external_id).where(
                    conversations.c.bot_id == int(row["bot_id"])
                )
            ).mappings()
        )
        conversation_ids = {str(item["id"]) for item in conversation_rows}
        external_index: dict[str, list[str]] = {}
        # 同一机器人下若不同渠道恰好使用相同 external_id，旧配置无法区分来源。
        # 迁移会把原订阅复制给全部匹配会话，之后新主线只按内部会话主键精确维护。
        for item in conversation_rows:
            external_id = str(item["external_id"] or "").strip()
            if external_id:
                external_index.setdefault(external_id, []).append(str(item["id"]))

        for field_name in SUBSCRIPTION_FIELDS:
            config[field_name] = _migrate_subscription_mapping(
                config.get(field_name),
                conversation_ids=conversation_ids,
                external_index=external_index,
            )
        config["highlight_push_conversation_ids"] = _migrate_conversation_list(
            config.get("highlight_push_conversation_ids"),
            conversation_ids=conversation_ids,
            external_index=external_index,
        )
        timeout_seconds = config.get("timeout_seconds", config.get("default_timeout", 10))
        feed_limit = config.get("feed_limit", config.get("default_feed_limit", 3))
        for field_name in REMOVED_RUNTIME_FIELDS:
            config.pop(field_name, None)
        # 旧字段只在迁移时转换一次，运行时代码不再读取别名或双配置路径。
        config["timeout_seconds"] = timeout_seconds
        config["feed_limit"] = feed_limit
        connection.execute(
            bot_plugins.update()
            .where(bot_plugins.c.id == int(row["id"]))
            .values(config_json=json.dumps(config, ensure_ascii=False, sort_keys=True))
        )


def downgrade() -> None:
    """旧 external_id 键和运行状态已失效，降级不会恢复废弃数据。"""


def _parse_object(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _migrate_subscription_mapping(
    raw: Any,
    *,
    conversation_ids: set[str],
    external_index: Mapping[str, list[str]],
) -> dict[str, list[str]]:
    """把一组旧会话键映射为内部主键，不保留无法定位的失效订阅。"""

    if not isinstance(raw, Mapping):
        return {}
    migrated: dict[str, set[str]] = {}
    for raw_key, raw_values in raw.items():
        key = str(raw_key or "").strip()
        if not isinstance(raw_values, list):
            continue
        target_ids = [key] if key in conversation_ids else list(external_index.get(key, []))
        values = {str(item or "").strip() for item in raw_values if str(item or "").strip()}
        for target_id in target_ids:
            migrated.setdefault(target_id, set()).update(values)
    return {key: sorted(values) for key, values in sorted(migrated.items(), key=lambda item: int(item[0]))}


def _migrate_conversation_list(
    raw: Any,
    *,
    conversation_ids: set[str],
    external_index: Mapping[str, list[str]],
) -> list[str]:
    if not isinstance(raw, list):
        return []
    migrated: set[str] = set()
    for raw_value in raw:
        value = str(raw_value or "").strip()
        if value in conversation_ids:
            migrated.add(value)
        else:
            migrated.update(external_index.get(value, []))
    return sorted(migrated, key=int)
