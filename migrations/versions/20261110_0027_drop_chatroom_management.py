"""去掉群的“已启用管理”概念:删除 chatrooms 的 management_status、is_registered、is_active 三列。

Revision ID: 20261110_0027
Revises: 20261110_0026
Create Date: 2026-11-10 18:00:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20261110_0027"
down_revision: Union[str, Sequence[str], None] = "20261110_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """所有群都参与插件调度和群功能,群里的开关只剩“群功能开关”和“插件在本群启停”两处。

    列上有索引,先删索引再删列。
    """

    with op.batch_alter_table("chatrooms", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chatrooms_management_status"))
        batch_op.drop_index(batch_op.f("ix_chatrooms_is_active"))
        batch_op.drop_column("management_status")
        batch_op.drop_column("is_registered")
        batch_op.drop_column("is_active")


def downgrade() -> None:
    """旧概念已废弃,不提供还原。"""
