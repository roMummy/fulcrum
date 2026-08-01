"""tenantize business tables

Revision ID: 20260730_0003
Revises: 20260730_0002
Create Date: 2026-08-01 12:30:00
"""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0003"
down_revision: str | Sequence[str] | None = "20260730_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 这些表保存租户实际产生或配置的业务数据。Plugin 是平台共享目录定义，
# AdminUser 是旧登录兼容表，平台账号、套餐和租户身份表也不在本次迁移范围内。
BUSINESS_TABLES = (
    "bots",
    "channels",
    "bot_plugins",
    "conversations",
    "chatrooms",
    "chatroom_members",
    "workwechat_contacts",
    "workwechat_contact_relations",
    "customer_operation_requests",
    "open_api_apps",
    "open_api_app_channels",
    "group_plugin_scopes",
    "raw_webhook_events",
    "messages",
    "message_attachments",
    "task_jobs",
    "weather_subscriptions",
    "sign_in_records",
    "sign_in_history",
    "points_accounts",
    "points_transactions",
    "points_prison_records",
    "error_logs",
    "plugin_execution_logs",
    "message_activity_hourly",
    "plugin_activity_hourly",
    "health_check_records",
    "alert_records",
    "audit_logs",
)


def _database_has_legacy_business_data(bind: sa.Connection) -> bool:
    """只探测记录是否存在，不读取任何历史业务正文或敏感字段。"""

    for table_name in BUSINESS_TABLES:
        row = bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first()
        if row is not None:
            return True
    return False


def _ensure_legacy_tenant(bind: sa.Connection) -> int:
    """为无法可靠推导归属的旧单租户数据创建可追踪的自营租户。"""

    legacy_tenant_id = bind.execute(
        sa.text("SELECT id FROM tenants WHERE code = :code"),
        {"code": "legacy-self"},
    ).scalar_one_or_none()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    if legacy_tenant_id is None:
        tenants = sa.table(
            "tenants",
            sa.column("id", sa.Integer()),
            sa.column("code", sa.String()),
            sa.column("name", sa.String()),
            sa.column("status", sa.String()),
            sa.column("contact_name", sa.String()),
            sa.column("contact_phone", sa.String()),
            sa.column("notes", sa.Text()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        bind.execute(
            tenants.insert().values(
                code="legacy-self",
                name="自营历史租户",
                status="active",
                contact_name=None,
                contact_phone=None,
                notes="由业务表租户化迁移自动创建，用于承接旧单租户数据。",
                created_at=now,
                updated_at=now,
            )
        )
        # sa.table() 是迁移期轻量表定义，不携带主键约束元数据，因此不能依赖
        # Result.inserted_primary_key。code 已有唯一索引，插入后按 code 回查可同时
        # 兼容 SQLite 与 MariaDB，也不会把驱动特有的 lastrowid 带入迁移逻辑。
        legacy_tenant_id = bind.execute(
            sa.text("SELECT id FROM tenants WHERE code = :code"),
            {"code": "legacy-self"},
        ).scalar_one()

    legacy_plan_id = bind.execute(
        sa.text("SELECT id FROM saas_plans WHERE code = :code"),
        {"code": "legacy-self"},
    ).scalar_one_or_none()
    if legacy_plan_id is None:
        plans = sa.table(
            "saas_plans",
            sa.column("id", sa.Integer()),
            sa.column("code", sa.String()),
            sa.column("name", sa.String()),
            sa.column("provider_node_limit", sa.Integer()),
            sa.column("status", sa.String()),
            sa.column("description", sa.Text()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        bind.execute(
            plans.insert().values(
                code="legacy-self",
                name="自营迁移套餐",
                provider_node_limit=999999,
                status="active",
                description="历史单租户数据迁移专用套餐，不用于公开售卖。",
                created_at=now,
                updated_at=now,
            )
        )
        legacy_plan_id = bind.execute(
            sa.text("SELECT id FROM saas_plans WHERE code = :code"),
            {"code": "legacy-self"},
        ).scalar_one()

    subscription_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM tenant_subscriptions "
            "WHERE tenant_id = :tenant_id AND plan_id = :plan_id LIMIT 1"
        ),
        {"tenant_id": legacy_tenant_id, "plan_id": legacy_plan_id},
    ).first()
    if subscription_exists is None:
        subscriptions = sa.table(
            "tenant_subscriptions",
            sa.column("tenant_id", sa.Integer()),
            sa.column("plan_id", sa.Integer()),
            sa.column("status", sa.String()),
            sa.column("starts_at", sa.DateTime(timezone=True)),
            sa.column("expires_at", sa.DateTime(timezone=True)),
            sa.column("notes", sa.Text()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        bind.execute(
            subscriptions.insert().values(
                tenant_id=legacy_tenant_id,
                plan_id=legacy_plan_id,
                status="active",
                starts_at=now,
                expires_at=None,
                notes="由业务表租户化迁移自动创建。",
                created_at=now,
                updated_at=now,
            )
        )

    return int(legacy_tenant_id)


def _replace_global_unique_constraints() -> None:
    """把旧单租户全局唯一键改成共享数据库中的租户内唯一键。"""

    with op.batch_alter_table("bots") as batch_op:
        batch_op.drop_index("ix_bots_name")
        batch_op.create_index("ix_bots_name", ["name"], unique=False)
        batch_op.create_unique_constraint("uq_bots_tenant_name", ["tenant_id", "name"])

    with op.batch_alter_table("open_api_apps") as batch_op:
        batch_op.drop_index("ix_open_api_apps_app_key")
        batch_op.create_index("ix_open_api_apps_app_key", ["app_key"], unique=False)
        batch_op.create_unique_constraint(
            "uq_open_api_apps_tenant_app_key",
            ["tenant_id", "app_key"],
        )

    with op.batch_alter_table("open_api_app_channels") as batch_op:
        batch_op.drop_constraint("uq_open_api_app_channel", type_="unique")
        batch_op.create_unique_constraint(
            "uq_open_api_app_tenant_channel",
            ["tenant_id", "app_id", "channel_id"],
        )

    with op.batch_alter_table("customer_operation_requests") as batch_op:
        batch_op.drop_constraint(
            "uq_customer_operation_request_channel_request",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_customer_operation_request_tenant_channel_request",
            ["tenant_id", "channel_id", "request_id"],
        )


def _restore_global_unique_constraints() -> None:
    """回退时恢复旧结构；存在跨租户重复值时数据库会拒绝有损降级。"""

    with op.batch_alter_table("customer_operation_requests") as batch_op:
        batch_op.drop_constraint(
            "uq_customer_operation_request_tenant_channel_request",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_customer_operation_request_channel_request",
            ["channel_id", "request_id"],
        )

    with op.batch_alter_table("open_api_app_channels") as batch_op:
        batch_op.drop_constraint("uq_open_api_app_tenant_channel", type_="unique")
        batch_op.create_unique_constraint(
            "uq_open_api_app_channel",
            ["app_id", "channel_id"],
        )

    with op.batch_alter_table("open_api_apps") as batch_op:
        batch_op.drop_constraint("uq_open_api_apps_tenant_app_key", type_="unique")
        batch_op.drop_index("ix_open_api_apps_app_key")
        batch_op.create_index("ix_open_api_apps_app_key", ["app_key"], unique=True)

    with op.batch_alter_table("bots") as batch_op:
        batch_op.drop_constraint("uq_bots_tenant_name", type_="unique")
        batch_op.drop_index("ix_bots_name")
        batch_op.create_index("ix_bots_name", ["name"], unique=True)


def upgrade() -> None:
    """为全部正式业务表增加强制租户归属并回填旧单租户数据。"""

    bind = op.get_bind()
    has_legacy_data = _database_has_legacy_business_data(bind)

    # 先使用可空列完成结构扩展，保证带历史数据的数据库不会因新增非空列直接失败。
    # 外键和索引同步建立，后续代码从迁移完成时起即可稳定按 tenant_id 查询。
    for table_name in BUSINESS_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column("tenant_id", sa.Integer(), nullable=True))
            batch_op.create_index(f"ix_{table_name}_tenant_id", ["tenant_id"], unique=False)
            batch_op.create_foreign_key(
                f"fk_{table_name}_tenant_id_tenants",
                "tenants",
                ["tenant_id"],
                ["id"],
            )

    if has_legacy_data:
        legacy_tenant_id = _ensure_legacy_tenant(bind)
        # 旧版本是单租户系统，迁移前没有可信字段可以区分客户。统一归入明确命名的
        # 自营租户比根据机器人、渠道或文本内容猜测归属更安全，也便于后续人工拆分。
        for table_name in BUSINESS_TABLES:
            bind.execute(
                sa.text(f"UPDATE {table_name} SET tenant_id = :tenant_id WHERE tenant_id IS NULL"),
                {"tenant_id": legacy_tenant_id},
            )

    _replace_global_unique_constraints()

    # 数据回填和唯一键调整全部完成后再收紧非空约束，迁移一旦成功就不允许产生
    # 无租户业务记录；后续 ORM 写入还会由 TenantOwnedMixin 做第二层强制校验。
    for table_name in BUSINESS_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "tenant_id",
                existing_type=sa.Integer(),
                nullable=False,
            )


def downgrade() -> None:
    """移除业务租户列并恢复旧单租户唯一约束。"""

    _restore_global_unique_constraints()

    for table_name in reversed(BUSINESS_TABLES):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_tenant_id_tenants",
                type_="foreignkey",
            )
            batch_op.drop_index(f"ix_{table_name}_tenant_id")
            batch_op.drop_column("tenant_id")

    # 不自动删除 legacy-self 租户和套餐。它们可能已被平台管理员继续使用，
    # 降级时保留少量孤立配置比静默删除账号、订阅或审计关联更可恢复。
