"""Tenant image bank and session-bound archive import previews."""
from alembic import op
import sqlalchemy as sa

revision = "20261112_0038"
down_revision = "20261112_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guess_image_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("question_id", sa.String(36), nullable=False),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("answer", sa.String(80), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(500), nullable=False),
        sa.Column("license", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "question_id", name="uq_guess_image_question_tenant_uuid"),
        sa.CheckConstraint("status IN ('draft', 'enabled', 'disabled')", name="ck_guess_image_question_status"),
    )
    op.create_index("ix_guess_image_questions_tenant_id", "guess_image_questions", ["tenant_id"])
    op.create_index("ix_guess_image_bank_status", "guess_image_questions", ["tenant_id", "status", "category"])
    op.create_table(
        "guess_image_imports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "token_hash", name="uq_guess_image_import_token"),
    )
    op.create_index("ix_guess_image_imports_tenant_id", "guess_image_imports", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("guess_image_imports")
    op.drop_table("guess_image_questions")
