"""新增 moments_posts:朋友圈动态缓存。

Revision ID: 20261111_0032
Revises: 20261110_0031
Create Date: 2026-11-11 04:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261111_0032"
down_revision: Union[str, Sequence[str], None] = "20261110_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """只新增表,不改动已有数据;同一渠道里同一条动态(sns_id)只有一行。"""

    op.create_table(
        "moments_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("sns_id", sa.String(length=32), nullable=False),
        sa.Column("author_wxid", sa.String(length=191), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("post_type", sa.Integer(), nullable=False),
        sa.Column("is_mine", sa.Boolean(), nullable=False),
        sa.Column("liked", sa.Boolean(), nullable=False),
        sa.Column("like_count", sa.Integer(), nullable=False),
        sa.Column("comment_count", sa.Integer(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("media_json", sa.Text(), nullable=True),
        sa.Column("likes_json", sa.Text(), nullable=True),
        sa.Column("comments_json", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "sns_id", name="uq_moments_posts_channel_sns"),
    )
    with op.batch_alter_table("moments_posts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_moments_posts_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_moments_posts_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_moments_posts_author_wxid"), ["author_wxid"], unique=False)
        batch_op.create_index("ix_moments_posts_channel_created", ["channel_id", "create_time"], unique=False)


def downgrade() -> None:
    """删除朋友圈缓存表(内容可以随时从手机重新取回)。"""

    op.drop_table("moments_posts")
