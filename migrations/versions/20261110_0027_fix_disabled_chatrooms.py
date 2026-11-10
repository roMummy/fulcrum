"""修正被“同步群列表”误重新启用的群:状态为停用或归档的群,启用开关恢复为关闭。

Revision ID: 20261110_0027
Revises: 20261110_0026
Create Date: 2026-11-10 19:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261110_0027"
down_revision: Union[str, Sequence[str], None] = "20261110_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """微桥每次同步群列表都会把 is_active 置为真,覆盖了后台手动停用,导致“停用”的群仍触发插件。

    代码已修复为只对新群默认启用;这里把已经被覆盖的数据改回去:management_status 是
    disabled/archived 的群,is_active 必须为假。待启用(discovered)的群不动,保持当前行为。
    """

    op.get_bind().execute(
        sa.text("UPDATE chatrooms SET is_active = 0 WHERE management_status IN ('disabled', 'archived') AND is_active = 1")
    )


def downgrade() -> None:
    """数据修正不提供还原。"""
