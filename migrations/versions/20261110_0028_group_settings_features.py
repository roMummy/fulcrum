"""群设置收归群功能:自动下载附件迁入群功能;AI 聊天开关下沉到各群。

Revision ID: 20261110_0028
Revises: 20261110_0027
Create Date: 2026-11-10 21:00:00
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261110_0028"
down_revision: Union[str, Sequence[str], None] = "20261110_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _load(extra_json) -> dict:
    try:
        value = json.loads(extra_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def upgrade() -> None:
    """两件事,均只改 chatrooms.extra_json,不动表结构:

    1. extra_json 里旧的 auto_download_media 键迁到 group_features.media_download.enabled,并删除旧键
       (单一路径,不保留双写)。
    2. 此前 AI 聊天由租户级开关控制,群聊也生效。聊天开关改为群级后,为了不让现有聊天突然停掉,
       对“租户聊天开关已打开”的租户,把其已启用的群的「AI 对话」打开;租户开关保留,只管私聊。
    """

    bind = op.get_bind()
    chat_tenants = {
        row[0] for row in bind.execute(sa.text("SELECT tenant_id FROM tenant_ai_settings WHERE chat_enabled = 1")).fetchall()
    }
    rooms = bind.execute(sa.text("SELECT id, tenant_id, is_active, extra_json FROM chatrooms")).fetchall()
    for room_id, tenant_id, is_active, extra_json in rooms:
        extra = _load(extra_json)
        changed = False
        features = extra.get("group_features") if isinstance(extra.get("group_features"), dict) else {}

        if "auto_download_media" in extra:
            if extra.pop("auto_download_media") and "media_download" not in features:
                features["media_download"] = {"enabled": True}
            changed = True

        if tenant_id in chat_tenants and is_active and "ai_chat" not in features:
            features["ai_chat"] = {"enabled": True}
            changed = True

        if not changed:
            continue
        if features:
            extra["group_features"] = features
        bind.execute(
            sa.text("UPDATE chatrooms SET extra_json = :extra WHERE id = :id"),
            {"extra": json.dumps(extra, ensure_ascii=False, sort_keys=True), "id": room_id},
        )


def downgrade() -> None:
    """旧开关已废弃,不提供还原。"""
