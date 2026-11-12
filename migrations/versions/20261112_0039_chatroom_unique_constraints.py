"""合并重复的群/成员/群插件作用域记录,并加唯一约束,杜绝并发重复建群。

背景:chatrooms、chatroom_members、group_plugin_scopes 一直没有唯一约束。微桥现在每条群消息都会
"找不到群就建群、找不到发言人就建成员",同一个群的首批消息并发到达时,每个请求都读到"不存在"并各建一条,
结果同一个群在群列表里重复出现(实测 6 个并发消息建出 6 条)。应用层加锁挡不住多个连接,只有数据库约束能兜底。

线上可能已经有重复数据,所以先合并再加约束,且合并不丢任何用户设置:
- 群:每组 (channel_id, external_id) 保留 id 最小的一条;用户设置类字段(运行模式、启用状态、备注、扩展配置)
  以同组里**最近更新**的那条为准(用户最后一次操作应当生效),名称/成员数等资料字段缺失时从其他行补齐;
- 成员:并入保留群;同一人有多条时保留信息更全的那条(优先 active,其次最近有活动);
- 群插件作用域:并入保留群;同一插件绑定有多条时保留最近被操作过的那条;
- 触发规则等其他引用群的行:直接改指向保留群;
- 全部指向完成后才删除多余的群行。

Revision ID: 20261112_0039
Revises: 20261112_0038
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261112_0039"
down_revision: Union[str, Sequence[str], None] = "20261112_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 这些字段属于用户的设置,以最近更新的那条群记录为准
# (extra_json 不在这里:它装着群功能开关、AI 记忆游标、目录状态等互不相干的键,必须按键合并,见 _merge_extra)
_SETTING_COLUMNS = ("management_status", "is_active", "operator_note")
# 这些字段是协议同步来的资料,保留群缺失时从其他重复行补齐
_PROFILE_COLUMNS = ("name", "remark", "owner_external_id", "first_seen_at", "last_synced_at")


def _ts(value) -> float:
    """把数据库里取出的时间(可能是 datetime、字符串或 None)转成可比较的数字。"""

    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _merge_extra(rows_oldest_first) -> str | None:
    """把同一个群的多条 extra_json 按键合并:每个键取"最近更新的那行里有这个键"的值。

    extra_json 里并存着互不相干的状态——群功能开关(group_features)、AI 群聊记忆的整理游标(ai_memory_cursor)、
    目录状态(member_directory)、退群信息(left)……如果整条取最新的一行,别的行上的键就丢了:
    用户设置的群功能会消失,记忆游标会倒退(导致群聊被重复写入长期记忆)。
    """

    import json

    merged: dict = {}
    for row in rows_oldest_first:
        raw = row["extra_json"]
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            merged.update(value)  # 后面(更新)的行覆盖前面的同名键,不同名的键全部保留
    return json.dumps(merged, ensure_ascii=False, sort_keys=True) if merged else None


def merge_duplicate_chatrooms(connection) -> dict[str, int]:
    """合并重复数据,返回统计。可重复执行:没有重复时什么也不改。"""

    stats = {"rooms_removed": 0, "members_moved": 0, "members_removed": 0, "scopes_moved": 0, "scopes_removed": 0}
    metadata = sa.MetaData()
    rooms = sa.Table("chatrooms", metadata, autoload_with=connection)
    members = sa.Table("chatroom_members", metadata, autoload_with=connection)
    scopes = sa.Table("group_plugin_scopes", metadata, autoload_with=connection)
    inspector = sa.inspect(connection)
    # 其他引用 chatrooms.id 的表(例如触发规则),只需要改指向
    referencing = [
        sa.Table(table, metadata, autoload_with=connection)
        for table in inspector.get_table_names()
        if table not in {"chatrooms", "chatroom_members", "group_plugin_scopes"}
        and any(fk["referred_table"] == "chatrooms" for fk in inspector.get_foreign_keys(table))
    ]

    groups = connection.execute(
        sa.select(rooms.c.channel_id, rooms.c.external_id)
        .group_by(rooms.c.channel_id, rooms.c.external_id)
        .having(sa.func.count(rooms.c.id) > 1)
    ).all()
    for channel_id, external_id in groups:
        rows = connection.execute(
            sa.select(rooms).where(rooms.c.channel_id == channel_id, rooms.c.external_id == external_id).order_by(rooms.c.id)
        ).mappings().all()
        keeper, duplicates = rows[0], rows[1:]
        latest = max(rows, key=lambda row: (_ts(row["updated_at"]), row["id"]))
        updates = {column: latest[column] for column in _SETTING_COLUMNS if column in rooms.c}
        if "extra_json" in rooms.c:
            updates["extra_json"] = _merge_extra(sorted(rows, key=lambda row: (_ts(row["updated_at"]), row["id"])))
        for column in _PROFILE_COLUMNS:
            if column in rooms.c and keeper[column] in (None, ""):
                donor = next((row[column] for row in duplicates if row[column] not in (None, "")), None)
                if donor is not None:
                    updates[column] = donor
        member_counts = [row["member_count"] or 0 for row in rows if "member_count" in rooms.c]
        if member_counts:
            updates["member_count"] = max(member_counts)
        if updates:
            connection.execute(rooms.update().where(rooms.c.id == keeper["id"]).values(**updates))

        for duplicate in duplicates:
            duplicate_id = duplicate["id"]
            # 成员:并入保留群;同一人已存在时保留信息更全的那条
            for member in connection.execute(sa.select(members).where(members.c.chatroom_id == duplicate_id)).mappings().all():
                existing = connection.execute(
                    sa.select(members).where(members.c.chatroom_id == keeper["id"], members.c.member_external_id == member["member_external_id"])
                ).mappings().first()
                if existing is None:
                    connection.execute(members.update().where(members.c.id == member["id"]).values(chatroom_id=keeper["id"]))
                    stats["members_moved"] += 1
                    continue
                better = max((existing, member), key=lambda row: (row["status"] == "active", _ts(row["last_active_at"]), _ts(row["updated_at"]), -row["id"]))
                if better["id"] == member["id"]:
                    connection.execute(members.delete().where(members.c.id == existing["id"]))
                    connection.execute(members.update().where(members.c.id == member["id"]).values(chatroom_id=keeper["id"]))
                else:
                    connection.execute(members.delete().where(members.c.id == member["id"]))
                stats["members_removed"] += 1
            # 群插件作用域:同一插件绑定有多条时保留最近被操作过的
            for scope in connection.execute(sa.select(scopes).where(scopes.c.chatroom_id == duplicate_id)).mappings().all():
                existing = connection.execute(
                    sa.select(scopes).where(scopes.c.chatroom_id == keeper["id"], scopes.c.bot_plugin_id == scope["bot_plugin_id"])
                ).mappings().first()
                if existing is None:
                    connection.execute(scopes.update().where(scopes.c.id == scope["id"]).values(chatroom_id=keeper["id"]))
                    stats["scopes_moved"] += 1
                    continue
                newer = max((existing, scope), key=lambda row: (_ts(row["last_operated_at"]), _ts(row["updated_at"]), row["id"]))
                if newer["id"] == scope["id"]:
                    connection.execute(scopes.delete().where(scopes.c.id == existing["id"]))
                    connection.execute(scopes.update().where(scopes.c.id == scope["id"]).values(chatroom_id=keeper["id"]))
                else:
                    connection.execute(scopes.delete().where(scopes.c.id == scope["id"]))
                stats["scopes_removed"] += 1
            for table in referencing:
                connection.execute(table.update().where(table.c.chatroom_id == duplicate_id).values(chatroom_id=keeper["id"]))
            connection.execute(rooms.delete().where(rooms.c.id == duplicate_id))
            stats["rooms_removed"] += 1

    # 即使群本身没有重复,也可能有"同一个群里同一个人"的重复成员(并发首条发言)
    for chatroom_id, member_id in connection.execute(
        sa.select(members.c.chatroom_id, members.c.member_external_id)
        .group_by(members.c.chatroom_id, members.c.member_external_id)
        .having(sa.func.count(members.c.id) > 1)
    ).all():
        rows = connection.execute(
            sa.select(members).where(members.c.chatroom_id == chatroom_id, members.c.member_external_id == member_id)
        ).mappings().all()
        best = max(rows, key=lambda row: (row["status"] == "active", _ts(row["last_active_at"]), _ts(row["updated_at"]), -row["id"]))
        for row in rows:
            if row["id"] != best["id"]:
                connection.execute(members.delete().where(members.c.id == row["id"]))
                stats["members_removed"] += 1
    # 同样,同一个群里同一个插件绑定的重复作用域
    for chatroom_id, binding_id in connection.execute(
        sa.select(scopes.c.chatroom_id, scopes.c.bot_plugin_id)
        .group_by(scopes.c.chatroom_id, scopes.c.bot_plugin_id)
        .having(sa.func.count(scopes.c.id) > 1)
    ).all():
        rows = connection.execute(
            sa.select(scopes).where(scopes.c.chatroom_id == chatroom_id, scopes.c.bot_plugin_id == binding_id)
        ).mappings().all()
        best = max(rows, key=lambda row: (_ts(row["last_operated_at"]), _ts(row["updated_at"]), row["id"]))
        for row in rows:
            if row["id"] != best["id"]:
                connection.execute(scopes.delete().where(scopes.c.id == row["id"]))
                stats["scopes_removed"] += 1
    return stats


def upgrade() -> None:
    merge_duplicate_chatrooms(op.get_bind())
    with op.batch_alter_table("chatrooms") as batch_op:
        batch_op.create_unique_constraint("uq_chatrooms_channel_external", ["channel_id", "external_id"])
    with op.batch_alter_table("chatroom_members") as batch_op:
        batch_op.create_unique_constraint("uq_chatroom_members_room_member", ["chatroom_id", "member_external_id"])
    with op.batch_alter_table("group_plugin_scopes") as batch_op:
        batch_op.create_unique_constraint("uq_group_plugin_scopes_room_binding", ["chatroom_id", "bot_plugin_id"])


def downgrade() -> None:
    # 只撤销约束;已合并的重复数据不会、也不应该还原
    with op.batch_alter_table("group_plugin_scopes") as batch_op:
        batch_op.drop_constraint("uq_group_plugin_scopes_room_binding", type_="unique")
    with op.batch_alter_table("chatroom_members") as batch_op:
        batch_op.drop_constraint("uq_chatroom_members_room_member", type_="unique")
    with op.batch_alter_table("chatrooms") as batch_op:
        batch_op.drop_constraint("uq_chatrooms_channel_external", type_="unique")
