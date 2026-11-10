"""新增 moments_media:朋友圈媒体(图片/视频)的本地副本与获取状态。

Revision ID: 20261111_0033
Revises: 20261111_0032
Create Date: 2026-11-11 06:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261111_0033"
down_revision: Union[str, Sequence[str], None] = "20261111_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """只新增表,不改动已有数据;同一条动态里同一个媒体只有一行。"""

    op.create_table(
        "moments_media",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("sns_id", sa.String(length=32), nullable=False),
        sa.Column("mid", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["moments_posts.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "mid", name="uq_moments_media_post_mid"),
    )
    with op.batch_alter_table("moments_media", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_moments_media_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_moments_media_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_moments_media_post_id"), ["post_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_moments_media_status"), ["status"], unique=False)


def downgrade() -> None:
    """删除朋友圈媒体记录表(文件留在磁盘上不动,可随时让手机重新传)。"""

    op.drop_table("moments_media")
