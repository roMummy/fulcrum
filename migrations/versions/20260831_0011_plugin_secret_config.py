"""增加插件敏感配置独立密文列。

Revision ID: 20260831_0011
Revises: 20260829_0010
Create Date: 2026-08-31 19:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0011"
down_revision: Union[str, Sequence[str], None] = "20260829_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """只增加新密文列，不把来源不明的历史普通配置自动提升为可信密钥。"""

    with op.batch_alter_table("bot_plugins", schema=None) as batch_op:
        batch_op.add_column(sa.Column("secret_config_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("secret_config_version", sa.String(length=20), nullable=True))


def downgrade() -> None:
    """删除插件密文版本和密文列。"""

    with op.batch_alter_table("bot_plugins", schema=None) as batch_op:
        batch_op.drop_column("secret_config_version")
        batch_op.drop_column("secret_config_encrypted")
