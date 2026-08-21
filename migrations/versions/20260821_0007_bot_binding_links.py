"""关联机器人额度绑定与实际机器人、渠道。

Revision ID: 20260821_0007
Revises: 20260821_0006
Create Date: 2026-08-21 12:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260821_0007"
down_revision: Union[str, Sequence[str], None] = "20260821_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """增加真实业务关联，不根据历史文本字段猜测或回填关系。"""

    with op.batch_alter_table("bot_bindings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bot_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("channel_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_bot_bindings_bot_id_bots",
            "bots",
            ["bot_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_bot_bindings_channel_id_channels",
            "channels",
            ["channel_id"],
            ["id"],
        )
        batch_op.create_index("ix_bot_bindings_bot_id", ["bot_id"], unique=True)
        batch_op.create_index("ix_bot_bindings_channel_id", ["channel_id"], unique=True)


def downgrade() -> None:
    """按依赖逆序删除额度绑定与机器人、渠道的关联。"""

    with op.batch_alter_table("bot_bindings", schema=None) as batch_op:
        batch_op.drop_index("ix_bot_bindings_channel_id")
        batch_op.drop_index("ix_bot_bindings_bot_id")
        batch_op.drop_constraint("fk_bot_bindings_channel_id_channels", type_="foreignkey")
        batch_op.drop_constraint("fk_bot_bindings_bot_id_bots", type_="foreignkey")
        batch_op.drop_column("channel_id")
        batch_op.drop_column("bot_id")
