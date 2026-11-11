"""机器人逻辑删除:bots 新增 deleted_at,并修正"账号已删除但机器人还在"的历史数据。

Revision ID: 20261111_0034
Revises: 20261111_0033
Create Date: 2026-11-11 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261111_0034"
down_revision: Union[str, Sequence[str], None] = "20261111_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增可空列与索引;已有机器人若名下所有账号都已被逻辑删除,同步标记为已删除。

    账号与机器人在产品里是一对一的,之前删除账号只隐藏了渠道,机器人还留在各处列表里。
    只处理"至少有一个账号且全部已删除"的机器人,没有账号的机器人(尚未接入)不动。
    """

    with op.batch_alter_table("bots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_bots_deleted_at"), ["deleted_at"], unique=False)
    op.execute(
        """
        UPDATE bots
        SET deleted_at = (SELECT MAX(channels.deleted_at) FROM channels WHERE channels.bot_id = bots.id),
            is_enabled = 0,
            status = 'deleted'
        WHERE EXISTS (SELECT 1 FROM channels WHERE channels.bot_id = bots.id)
          AND NOT EXISTS (SELECT 1 FROM channels WHERE channels.bot_id = bots.id AND channels.deleted_at IS NULL)
        """
    )


def downgrade() -> None:
    """去掉 deleted_at 列;被标记删除的机器人会重新出现在列表里(状态保持 deleted,需要手动处理)。"""

    with op.batch_alter_table("bots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bots_deleted_at"))
        batch_op.drop_column("deleted_at")
