"""保存插件首次绑定时的默认群开放范围。"""

from alembic import op
import sqlalchemy as sa


revision = "20261112_0037"
down_revision = "20261111_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 迁移只补齐旧绑定的原有行为；新绑定由业务入口显式写入 False。
    op.add_column("bot_plugins", sa.Column("group_default_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("bot_plugins", "group_default_enabled")
