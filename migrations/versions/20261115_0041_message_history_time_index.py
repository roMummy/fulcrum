"""为按消息时间读取会话历史增加组合索引。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261115_0041"
down_revision = "20261113_0040"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_messages_conversation_time"
INDEX_COLUMNS = ["conversation_id", "created_at", "id"]


def upgrade() -> None:
    # 部分安装已手动建立同名索引，确认列一致后直接复用。
    if not context.is_offline_mode():
        for index in sa.inspect(op.get_bind()).get_indexes("messages"):
            if index["name"] == INDEX_NAME:
                if index["column_names"] != INDEX_COLUMNS or index["unique"]:
                    raise RuntimeError(f"unexpected_index_definition:{INDEX_NAME}")
                return
    op.create_index(INDEX_NAME, "messages", INDEX_COLUMNS, unique=False)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="messages")
