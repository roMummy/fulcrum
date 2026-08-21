"""增加曜枢 AI 公开套餐、订单和通用机器人绑定模型。

Revision ID: 20260821_0006
Revises: 20260801_0005
Create Date: 2026-08-21 10:30:00
"""

from datetime import datetime
from typing import Sequence, Union
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


revision: str = "20260821_0006"
down_revision: Union[str, Sequence[str], None] = "20260801_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now_shanghai() -> datetime:
    """为迁移插入的套餐记录生成与业务模型一致的上海时区时间。"""

    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _seed_public_plans() -> None:
    """补齐首版套餐，同时保留同编码旧套餐已经存在的业务配置。"""

    bind = op.get_bind()
    plans = sa.table(
        "saas_plans",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("provider_node_limit", sa.Integer()),
        sa.column("bot_limit", sa.Integer()),
        sa.column("monthly_price_cents", sa.Integer()),
        sa.column("yearly_price_cents", sa.Integer()),
        sa.column("trial_days", sa.Integer()),
        sa.column("is_public", sa.Boolean()),
        sa.column("is_featured", sa.Boolean()),
        sa.column("display_order", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    defaults = (
        {
            "code": "trial",
            "name": "免费试用",
            "provider_node_limit": 1,
            "bot_limit": 1,
            "monthly_price_cents": 0,
            "yearly_price_cents": 0,
            "trial_days": 7,
            "is_public": False,
            "is_featured": False,
            "display_order": 0,
            "description": "注册后自动开通的七天试用套餐",
        },
        {
            "code": "starter",
            "name": "入门版",
            "provider_node_limit": 1,
            "bot_limit": 1,
            "monthly_price_cents": 9_900,
            "yearly_price_cents": 99_000,
            "trial_days": 0,
            "is_public": True,
            "is_featured": False,
            "display_order": 10,
            "description": "适合单机器人起步的轻量团队",
        },
        {
            "code": "standard",
            "name": "标准版",
            "provider_node_limit": 3,
            "bot_limit": 3,
            "monthly_price_cents": 25_900,
            "yearly_price_cents": 259_000,
            "trial_days": 0,
            "is_public": True,
            "is_featured": True,
            "display_order": 20,
            "description": "适合多渠道协作和稳定运营的成长团队",
        },
        {
            "code": "professional",
            "name": "专业版",
            "provider_node_limit": 10,
            "bot_limit": 10,
            "monthly_price_cents": 69_900,
            "yearly_price_cents": 699_000,
            "trial_days": 0,
            "is_public": True,
            "is_featured": False,
            "display_order": 30,
            "description": "适合规模化机器人运营和精细权限管理",
        },
    )

    for plan_defaults in defaults:
        existing = bind.execute(
            sa.select(plans).where(plans.c.code == plan_defaults["code"])
        ).mappings().first()
        now = _now_shanghai()
        if existing is None:
            # 只在同编码套餐不存在时写入完整默认值，避免覆盖平台已有名称、旧额度和说明。
            bind.execute(
                plans.insert().values(
                    **plan_defaults,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            continue

        # 这些字段均由本迁移首次引入。仅当整组商业字段仍是迁移默认值时补齐推荐值，
        # 这样即使 MariaDB DDL 中途提交后人工调整过数据，再次执行也不会反向覆盖。
        commercial_fields_are_empty = (
            int(existing["monthly_price_cents"] or 0) == 0
            and int(existing["yearly_price_cents"] or 0) == 0
            and int(existing["trial_days"] or 0) == 0
            and not bool(existing["is_public"])
            and not bool(existing["is_featured"])
            and int(existing["display_order"] or 0) == 0
        )
        update_values: dict[str, object] = {}
        if int(existing["bot_limit"] or 0) == 0:
            update_values["bot_limit"] = plan_defaults["bot_limit"]
        if commercial_fields_are_empty:
            update_values.update(
                monthly_price_cents=plan_defaults["monthly_price_cents"],
                yearly_price_cents=plan_defaults["yearly_price_cents"],
                trial_days=plan_defaults["trial_days"],
                is_public=plan_defaults["is_public"],
                is_featured=plan_defaults["is_featured"],
                display_order=plan_defaults["display_order"],
            )
        if update_values:
            update_values["updated_at"] = now
            bind.execute(
                plans.update().where(plans.c.id == existing["id"]).values(**update_values)
            )


def upgrade() -> None:
    """增加商业化字段、订单表、通用绑定表和设备试用领取记录。"""

    # 先给旧表增加带服务端默认值的列，确保 MariaDB 上已有记录可以无停机完成结构变更。
    with op.batch_alter_table("tenant_users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mobile", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.create_index(batch_op.f("ix_tenant_users_mobile"), ["mobile"], unique=True)

    with op.batch_alter_table("saas_plans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("bot_limit", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("monthly_price_cents", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("yearly_price_cents", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("trial_days", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("is_featured", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.create_check_constraint("ck_saas_plans_bot_limit", "bot_limit >= 0")
        batch_op.create_check_constraint(
            "ck_saas_plans_monthly_price",
            "monthly_price_cents >= 0",
        )
        batch_op.create_check_constraint(
            "ck_saas_plans_yearly_price",
            "yearly_price_cents >= 0",
        )
        batch_op.create_check_constraint("ck_saas_plans_trial_days", "trial_days >= 0")
        batch_op.create_check_constraint("ck_saas_plans_display_order", "display_order >= 0")

    # 旧套餐额度先原样映射到通用机器人额度，兼容已有租户套餐配置。
    op.execute(sa.text("UPDATE saas_plans SET bot_limit = provider_node_limit WHERE bot_limit = 0"))
    _seed_public_plans()

    op.create_table(
        "billing_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_no", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("billing_cycle", sa.String(length=20), nullable=False),
        sa.Column("bot_limit_snapshot", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("payment_reference", sa.String(length=255), nullable=True),
        # 私有凭证只能经鉴权路由访问，数据库不保存文件正文，也不把该路径返回给页面。
        sa.Column("payment_proof_path", sa.String(length=500), nullable=True),
        sa.Column("payment_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("fulfilled_subscription_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("billing_cycle IN ('monthly', 'yearly')", name="ck_billing_orders_cycle"),
        sa.CheckConstraint("bot_limit_snapshot >= 0", name="ck_billing_orders_bot_limit"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_billing_orders_amount"),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'payment_submitted', 'approved', 'rejected', 'cancelled')",
            name="ck_billing_orders_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["saas_plans.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["platform_users.id"]),
        sa.ForeignKeyConstraint(["fulfilled_subscription_id"], ["tenant_subscriptions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("billing_orders", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_billing_orders_order_no"), ["order_no"], unique=True)
        batch_op.create_index(batch_op.f("ix_billing_orders_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_billing_orders_plan_id"), ["plan_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_billing_orders_billing_cycle"), ["billing_cycle"], unique=False)
        batch_op.create_index(batch_op.f("ix_billing_orders_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_billing_orders_reviewed_by_user_id"),
            ["reviewed_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_billing_orders_fulfilled_subscription_id"),
            ["fulfilled_subscription_id"],
            unique=True,
        )

    op.create_table(
        "bot_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column("bot_account_id", sa.String(length=191), nullable=False),
        sa.Column("display_name", sa.String(length=150), nullable=True),
        # 设备原始指纹绝不落库，只持久化固定长度的不可逆摘要。
        sa.Column("device_fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by_user_id", sa.Integer(), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'released')",
            name="ck_bot_bindings_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["released_by_user_id"], ["platform_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("adapter_type", "bot_account_id", name="uq_bot_binding_adapter_account"),
    )
    with op.batch_alter_table("bot_bindings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_bot_bindings_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_bot_bindings_adapter_type"), ["adapter_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_bot_bindings_bot_account_id"), ["bot_account_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_bot_bindings_device_fingerprint_hash"),
            ["device_fingerprint_hash"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_bot_bindings_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_bot_bindings_released_by_user_id"),
            ["released_by_user_id"],
            unique=False,
        )

    op.create_table(
        "device_trial_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("device_fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("device_trial_claims", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_device_trial_claims_device_fingerprint_hash"),
            ["device_fingerprint_hash"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_device_trial_claims_tenant_id"),
            ["tenant_id"],
            unique=False,
        )


def downgrade() -> None:
    """删除本迁移增加的商业化结构，保留旧 Provider 节点额度和历史业务表。"""

    with op.batch_alter_table("device_trial_claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_device_trial_claims_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_device_trial_claims_device_fingerprint_hash"))
    op.drop_table("device_trial_claims")

    with op.batch_alter_table("bot_bindings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bot_bindings_released_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_bot_bindings_status"))
        batch_op.drop_index(batch_op.f("ix_bot_bindings_device_fingerprint_hash"))
        batch_op.drop_index(batch_op.f("ix_bot_bindings_bot_account_id"))
        batch_op.drop_index(batch_op.f("ix_bot_bindings_adapter_type"))
        batch_op.drop_index(batch_op.f("ix_bot_bindings_tenant_id"))
    op.drop_table("bot_bindings")

    with op.batch_alter_table("billing_orders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_billing_orders_fulfilled_subscription_id"))
        batch_op.drop_index(batch_op.f("ix_billing_orders_reviewed_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_billing_orders_status"))
        batch_op.drop_index(batch_op.f("ix_billing_orders_billing_cycle"))
        batch_op.drop_index(batch_op.f("ix_billing_orders_plan_id"))
        batch_op.drop_index(batch_op.f("ix_billing_orders_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_billing_orders_order_no"))
    op.drop_table("billing_orders")

    with op.batch_alter_table("saas_plans", schema=None) as batch_op:
        batch_op.drop_constraint("ck_saas_plans_display_order", type_="check")
        batch_op.drop_constraint("ck_saas_plans_trial_days", type_="check")
        batch_op.drop_constraint("ck_saas_plans_yearly_price", type_="check")
        batch_op.drop_constraint("ck_saas_plans_monthly_price", type_="check")
        batch_op.drop_constraint("ck_saas_plans_bot_limit", type_="check")
        batch_op.drop_column("display_order")
        batch_op.drop_column("is_featured")
        batch_op.drop_column("is_public")
        batch_op.drop_column("trial_days")
        batch_op.drop_column("yearly_price_cents")
        batch_op.drop_column("monthly_price_cents")
        batch_op.drop_column("bot_limit")

    with op.batch_alter_table("tenant_users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tenant_users_mobile"))
        batch_op.drop_column("is_owner")
        batch_op.drop_column("mobile")
