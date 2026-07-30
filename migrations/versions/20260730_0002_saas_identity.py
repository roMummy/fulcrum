"""平台账号、租户、套餐和数据库会话模型。

Revision ID: 20260730_0002
Revises: 20260730_0001
Create Date: 2026-07-30 22:50:12.359955
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = '20260730_0002'
down_revision: Union[str, Sequence[str], None] = '20260730_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """把数据库升级到当前版本。"""
    # 平台身份与租户身份使用独立表和外键链路；会话表只保存令牌摘要，并保留认证版本、
    # 绝对过期时间与吊销信息，为后续即时失效和可信 tenant_id 恢复提供数据库基础。
    op.create_table('platform_users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=30), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('auth_version', sa.Integer(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("role IN ('platform_admin')", name='ck_platform_users_role'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('platform_users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_platform_users_role'), ['role'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_users_username'), ['username'], unique=True)

    op.create_table('saas_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('provider_node_limit', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name='ck_saas_plans_status'),
    sa.CheckConstraint('provider_node_limit >= 0', name='ck_saas_plans_provider_node_limit'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('saas_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_saas_plans_code'), ['code'], unique=True)
        batch_op.create_index(batch_op.f('ix_saas_plans_status'), ['status'], unique=False)

    op.create_table('tenants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('contact_name', sa.String(length=100), nullable=True),
    sa.Column('contact_phone', sa.String(length=64), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name='ck_tenants_status'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenants_code'), ['code'], unique=True)
        batch_op.create_index(batch_op.f('ix_tenants_status'), ['status'], unique=False)

    op.create_table('platform_audit_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('target_type', sa.String(length=50), nullable=False),
    sa.Column('target_id', sa.Integer(), nullable=True),
    sa.Column('target_tenant_id', sa.Integer(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['platform_users.id'], ),
    sa.ForeignKeyConstraint(['target_tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('platform_audit_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_platform_audit_logs_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_audit_logs_actor_user_id'), ['actor_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_audit_logs_target_id'), ['target_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_audit_logs_target_tenant_id'), ['target_tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_audit_logs_target_type'), ['target_type'], unique=False)

    op.create_table('platform_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('platform_user_id', sa.Integer(), nullable=False),
    sa.Column('session_token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token_hash', sa.String(length=64), nullable=False),
    sa.Column('auth_version', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoke_reason', sa.String(length=255), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['platform_user_id'], ['platform_users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('platform_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_platform_sessions_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_sessions_platform_user_id'), ['platform_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_sessions_revoked_at'), ['revoked_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_sessions_session_token_hash'), ['session_token_hash'], unique=True)

    op.create_table('tenant_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('active', 'expired', 'suspended')", name='ck_tenant_subscriptions_status'),
    sa.ForeignKeyConstraint(['plan_id'], ['saas_plans.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'starts_at', name='uq_tenant_subscription_start')
    )
    with op.batch_alter_table('tenant_subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenant_subscriptions_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_subscriptions_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_subscriptions_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_subscriptions_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('tenant_users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=30), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('auth_version', sa.Integer(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("role IN ('tenant_admin', 'operator', 'readonly')", name='ck_tenant_users_role'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'username', name='uq_tenant_user_username')
    )
    with op.batch_alter_table('tenant_users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenant_users_role'), ['role'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_users_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('tenant_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('tenant_user_id', sa.Integer(), nullable=False),
    sa.Column('session_token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token_hash', sa.String(length=64), nullable=False),
    sa.Column('auth_version', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoke_reason', sa.String(length=255), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['tenant_user_id'], ['tenant_users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tenant_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenant_sessions_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_sessions_revoked_at'), ['revoked_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_sessions_session_token_hash'), ['session_token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_tenant_sessions_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tenant_sessions_tenant_user_id'), ['tenant_user_id'], unique=False)



def downgrade() -> None:
    """把数据库回退到上一版本。"""
    # 先删除依赖账号、租户和套餐的会话及订阅表，再删除根实体，保持外键顺序可回退。
    with op.batch_alter_table('tenant_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_sessions_tenant_user_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_sessions_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_sessions_session_token_hash'))
        batch_op.drop_index(batch_op.f('ix_tenant_sessions_revoked_at'))
        batch_op.drop_index(batch_op.f('ix_tenant_sessions_expires_at'))

    op.drop_table('tenant_sessions')
    with op.batch_alter_table('tenant_users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_users_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_users_role'))

    op.drop_table('tenant_users')
    with op.batch_alter_table('tenant_subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenant_subscriptions_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_subscriptions_status'))
        batch_op.drop_index(batch_op.f('ix_tenant_subscriptions_plan_id'))
        batch_op.drop_index(batch_op.f('ix_tenant_subscriptions_expires_at'))

    op.drop_table('tenant_subscriptions')
    with op.batch_alter_table('platform_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_platform_sessions_session_token_hash'))
        batch_op.drop_index(batch_op.f('ix_platform_sessions_revoked_at'))
        batch_op.drop_index(batch_op.f('ix_platform_sessions_platform_user_id'))
        batch_op.drop_index(batch_op.f('ix_platform_sessions_expires_at'))

    op.drop_table('platform_sessions')
    with op.batch_alter_table('platform_audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_platform_audit_logs_target_type'))
        batch_op.drop_index(batch_op.f('ix_platform_audit_logs_target_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_platform_audit_logs_target_id'))
        batch_op.drop_index(batch_op.f('ix_platform_audit_logs_actor_user_id'))
        batch_op.drop_index(batch_op.f('ix_platform_audit_logs_action'))

    op.drop_table('platform_audit_logs')
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenants_status'))
        batch_op.drop_index(batch_op.f('ix_tenants_code'))

    op.drop_table('tenants')
    with op.batch_alter_table('saas_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_saas_plans_status'))
        batch_op.drop_index(batch_op.f('ix_saas_plans_code'))

    op.drop_table('saas_plans')
    with op.batch_alter_table('platform_users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_platform_users_username'))
        batch_op.drop_index(batch_op.f('ix_platform_users_role'))

    op.drop_table('platform_users')
