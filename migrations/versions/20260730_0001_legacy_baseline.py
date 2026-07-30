"""精简后的历史业务结构基线。

Revision ID: 20260730_0001
Revises: 
Create Date: 2026-07-30 22:45:16.764860
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa



revision: str = '20260730_0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """把数据库升级到当前版本。"""
    # 本迁移从机器人精简完成后的 Base.metadata 一次性生成。它只负责新 SaaS 空库建表，
    # 不包含旧项目的数据搬迁，也不会创建机器人分组、模板、标签、环境等已删除结构。
    op.create_table('admin_users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=50), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('auth_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('admin_users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_admin_users_role'), ['role'], unique=False)
        batch_op.create_index(batch_op.f('ix_admin_users_username'), ['username'], unique=True)

    op.create_table('alert_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=100), nullable=False),
    sa.Column('level', sa.String(length=20), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('related_type', sa.String(length=50), nullable=True),
    sa.Column('related_id', sa.Integer(), nullable=True),
    sa.Column('rule_key', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('details_json', sa.Text(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alert_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_records_level'), ['level'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_records_related_id'), ['related_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_records_rule_key'), ['rule_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_records_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_records_status'), ['status'], unique=False)

    op.create_table('audit_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('actor', sa.String(length=100), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('target_type', sa.String(length=50), nullable=False),
    sa.Column('target_id', sa.Integer(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_logs_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_actor'), ['actor'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_target_id'), ['target_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_target_type'), ['target_type'], unique=False)

    op.create_table('bots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bots_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bots_name'), ['name'], unique=True)

    op.create_table('error_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=100), nullable=False),
    sa.Column('level', sa.String(length=20), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('error_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_error_logs_source'), ['source'], unique=False)

    op.create_table('health_check_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.String(length=50), nullable=False),
    sa.Column('target_type', sa.String(length=50), nullable=False),
    sa.Column('target_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('details_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('health_check_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_health_check_records_scope'), ['scope'], unique=False)
        batch_op.create_index(batch_op.f('ix_health_check_records_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_health_check_records_target_id'), ['target_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_health_check_records_target_type'), ['target_type'], unique=False)

    op.create_table('plugins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=False),
    sa.Column('plugin_type', sa.String(length=50), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=True),
    sa.Column('author', sa.String(length=100), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('default_enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plugins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_plugins_code'), ['code'], unique=True)

    op.create_table('points_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('group_id', sa.String(length=128), nullable=False),
    sa.Column('user_name', sa.String(length=255), nullable=True),
    sa.Column('total_points', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('points_accounts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_points_accounts_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_accounts_user_id'), ['user_id'], unique=False)

    op.create_table('points_prison_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('group_id', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('bailed_out_by', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('points_prison_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_points_prison_records_end_at'), ['end_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_prison_records_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_prison_records_start_at'), ['start_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_prison_records_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_prison_records_user_id'), ['user_id'], unique=False)

    op.create_table('points_transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.String(length=128), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('counterparty_user_id', sa.String(length=128), nullable=True),
    sa.Column('points_delta', sa.Integer(), nullable=False),
    sa.Column('balance_after', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('metadata_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('points_transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_points_transactions_counterparty_user_id'), ['counterparty_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_transactions_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_transactions_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_points_transactions_user_id'), ['user_id'], unique=False)

    op.create_table('sign_in_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('group_id', sa.String(length=128), nullable=False),
    sa.Column('user_name', sa.String(length=255), nullable=True),
    sa.Column('points', sa.Integer(), nullable=False),
    sa.Column('total_sign_count', sa.Integer(), nullable=False),
    sa.Column('signin_streak', sa.Integer(), nullable=False),
    sa.Column('previous_streak', sa.Integer(), nullable=False),
    sa.Column('sign_stat', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sign_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sign_in_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sign_in_records_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sign_in_records_user_id'), ['user_id'], unique=False)

    op.create_table('task_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job_type', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('related_type', sa.String(length=50), nullable=True),
    sa.Column('related_id', sa.Integer(), nullable=True),
    sa.Column('payload_json', sa.Text(), nullable=False),
    sa.Column('result_json', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_task_jobs_available_at'), ['available_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_jobs_job_type'), ['job_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_jobs_priority'), ['priority'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_jobs_related_id'), ['related_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_jobs_related_type'), ['related_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_jobs_status'), ['status'], unique=False)

    op.create_table('bot_plugins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=False),
    sa.Column('plugin_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('runtime_status', sa.String(length=20), nullable=False),
    sa.Column('last_reload_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_execution_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_execution_status', sa.String(length=20), nullable=True),
    sa.Column('last_execution_summary', sa.Text(), nullable=True),
    sa.Column('runtime_details_json', sa.Text(), nullable=True),
    sa.Column('config_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['plugin_id'], ['plugins.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bot_plugins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bot_plugins_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bot_plugins_plugin_id'), ['plugin_id'], unique=False)

    op.create_table('channels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('channel_type', sa.String(length=50), nullable=False),
    sa.Column('api_base', sa.String(length=255), nullable=True),
    sa.Column('bot_token_encrypted', sa.Text(), nullable=True),
    sa.Column('bot_token_masked', sa.String(length=255), nullable=True),
    sa.Column('webhook_path', sa.String(length=255), nullable=True),
    sa.Column('dm_policy', sa.String(length=30), nullable=False),
    sa.Column('allow_from', sa.Text(), nullable=True),
    sa.Column('adapter_config_json', sa.Text(), nullable=True),
    sa.Column('login_state', sa.String(length=30), nullable=False),
    sa.Column('qrcode_status', sa.String(length=30), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('runtime_info_json', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_channels_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_channels_channel_type'), ['channel_type'], unique=False)

    op.create_table('sign_in_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('record_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('group_id', sa.String(length=128), nullable=False),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('is_makeup', sa.Boolean(), nullable=False),
    sa.Column('points_delta', sa.Integer(), nullable=False),
    sa.Column('streak', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['record_id'], ['sign_in_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sign_in_history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sign_in_history_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sign_in_history_is_makeup'), ['is_makeup'], unique=False)
        batch_op.create_index(batch_op.f('ix_sign_in_history_record_id'), ['record_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sign_in_history_signed_at'), ['signed_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_sign_in_history_user_id'), ['user_id'], unique=False)

    op.create_table('chatrooms',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('remark', sa.String(length=255), nullable=True),
    sa.Column('owner_external_id', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('registration_source', sa.String(length=20), nullable=False),
    sa.Column('management_status', sa.String(length=30), nullable=False),
    sa.Column('is_registered', sa.Boolean(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('member_count', sa.Integer(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('operator_note', sa.Text(), nullable=True),
    sa.Column('extra_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('chatrooms', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chatrooms_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatrooms_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatrooms_external_id'), ['external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatrooms_is_active'), ['is_active'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatrooms_management_status'), ['management_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatrooms_registration_source'), ['registration_source'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatrooms_status'), ['status'], unique=False)

    op.create_table('conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=True),
    sa.Column('channel_type', sa.String(length=50), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=True),
    sa.Column('chat_type', sa.String(length=20), nullable=False),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversations_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_channel_type'), ['channel_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversations_external_id'), ['external_id'], unique=False)

    op.create_table('message_activity_hourly',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=True),
    sa.Column('channel_id', sa.Integer(), nullable=True),
    sa.Column('channel_type', sa.String(length=50), nullable=True),
    sa.Column('chat_type', sa.String(length=20), nullable=True),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('message_count', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('message_activity_hourly', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_message_activity_hourly_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_activity_hourly_bucket_start'), ['bucket_start'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_activity_hourly_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_activity_hourly_channel_type'), ['channel_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_activity_hourly_chat_type'), ['chat_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_activity_hourly_direction'), ['direction'], unique=False)

    op.create_table('open_api_apps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('app_key', sa.String(length=100), nullable=False),
    sa.Column('app_secret_encrypted', sa.Text(), nullable=True),
    sa.Column('app_secret_hash', sa.String(length=128), nullable=True),
    sa.Column('bot_id', sa.Integer(), nullable=True),
    sa.Column('default_channel_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('allowed_plugin_codes_json', sa.Text(), nullable=True),
    sa.Column('webhook_url', sa.Text(), nullable=True),
    sa.Column('webhook_secret', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['default_channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('open_api_apps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_open_api_apps_app_key'), ['app_key'], unique=True)
        batch_op.create_index(batch_op.f('ix_open_api_apps_app_secret_hash'), ['app_secret_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_open_api_apps_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_open_api_apps_default_channel_id'), ['default_channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_open_api_apps_name'), ['name'], unique=False)
        batch_op.create_index(batch_op.f('ix_open_api_apps_status'), ['status'], unique=False)

    op.create_table('plugin_activity_hourly',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=True),
    sa.Column('channel_id', sa.Integer(), nullable=True),
    sa.Column('plugin_id', sa.Integer(), nullable=True),
    sa.Column('bot_plugin_id', sa.Integer(), nullable=True),
    sa.Column('plugin_code', sa.String(length=100), nullable=False),
    sa.Column('trigger_count', sa.Integer(), nullable=False),
    sa.Column('matched_count', sa.Integer(), nullable=False),
    sa.Column('attempted_count', sa.Integer(), nullable=False),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('failed_count', sa.Integer(), nullable=False),
    sa.Column('skipped_count', sa.Integer(), nullable=False),
    sa.Column('blocked_count', sa.Integer(), nullable=False),
    sa.Column('timeout_count', sa.Integer(), nullable=False),
    sa.Column('outbound_count', sa.Integer(), nullable=False),
    sa.Column('total_duration_ms', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['bot_plugin_id'], ['bot_plugins.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['plugin_id'], ['plugins.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plugin_activity_hourly', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_plugin_activity_hourly_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_activity_hourly_bot_plugin_id'), ['bot_plugin_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_activity_hourly_bucket_start'), ['bucket_start'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_activity_hourly_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_activity_hourly_plugin_code'), ['plugin_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_activity_hourly_plugin_id'), ['plugin_id'], unique=False)

    op.create_table('raw_webhook_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('event_key', sa.String(length=128), nullable=True),
    sa.Column('payload_json', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('dispatch_summary_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('raw_webhook_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_raw_webhook_events_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_raw_webhook_events_event_key'), ['event_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_raw_webhook_events_source'), ['source'], unique=False)

    op.create_table('workwechat_contacts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('contact_kind', sa.String(length=30), nullable=False),
    sa.Column('contact_external_id', sa.String(length=128), nullable=False),
    sa.Column('conversation_external_id', sa.String(length=128), nullable=True),
    sa.Column('external_userid', sa.String(length=128), nullable=True),
    sa.Column('userid', sa.String(length=128), nullable=True),
    sa.Column('open_userid', sa.String(length=128), nullable=True),
    sa.Column('unionid', sa.String(length=128), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('alias', sa.String(length=128), nullable=True),
    sa.Column('english_name', sa.String(length=255), nullable=True),
    sa.Column('avatar_url', sa.Text(), nullable=True),
    sa.Column('gender', sa.String(length=20), nullable=False),
    sa.Column('contact_type', sa.String(length=30), nullable=False),
    sa.Column('corp_name', sa.String(length=255), nullable=True),
    sa.Column('corp_full_name', sa.String(length=255), nullable=True),
    sa.Column('corp_id', sa.String(length=128), nullable=True),
    sa.Column('position', sa.String(length=255), nullable=True),
    sa.Column('mobile', sa.String(length=64), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('biz_mail', sa.String(length=255), nullable=True),
    sa.Column('telephone', sa.String(length=64), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('department_ids_json', sa.Text(), nullable=True),
    sa.Column('main_department_id', sa.Integer(), nullable=True),
    sa.Column('is_leader_in_dept_json', sa.Text(), nullable=True),
    sa.Column('direct_leader_json', sa.Text(), nullable=True),
    sa.Column('hide_mobile', sa.Boolean(), nullable=False),
    sa.Column('enable', sa.Boolean(), nullable=False),
    sa.Column('member_status', sa.String(length=30), nullable=True),
    sa.Column('external_profile_json', sa.Text(), nullable=True),
    sa.Column('extattr_json', sa.Text(), nullable=True),
    sa.Column('raw_contact_json', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=30), nullable=False),
    sa.Column('sync_status', sa.String(length=30), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delete_reason', sa.String(length=50), nullable=True),
    sa.Column('delete_event_key', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('workwechat_contacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_contact_external_id'), ['contact_external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_contact_kind'), ['contact_kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_conversation_external_id'), ['conversation_external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_external_userid'), ['external_userid'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_sync_status'), ['sync_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contacts_userid'), ['userid'], unique=False)

    op.create_table('chatroom_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chatroom_id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('member_external_id', sa.String(length=128), nullable=False),
    sa.Column('nickname', sa.String(length=255), nullable=True),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('role', sa.String(length=30), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_active_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('extra_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['chatroom_id'], ['chatrooms.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('chatroom_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chatroom_members_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatroom_members_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatroom_members_chatroom_id'), ['chatroom_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatroom_members_member_external_id'), ['member_external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_chatroom_members_status'), ['status'], unique=False)

    op.create_table('group_plugin_scopes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('chatroom_id', sa.Integer(), nullable=False),
    sa.Column('bot_plugin_id', sa.Integer(), nullable=False),
    sa.Column('scope_status', sa.String(length=30), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('action_scope', sa.String(length=50), nullable=True),
    sa.Column('policy_effect', sa.String(length=20), nullable=True),
    sa.Column('config_override_json', sa.Text(), nullable=True),
    sa.Column('last_operator_id', sa.Integer(), nullable=True),
    sa.Column('last_operated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('audit_note', sa.Text(), nullable=True),
    sa.Column('extra_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_plugin_id'], ['bot_plugins.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['chatroom_id'], ['chatrooms.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('group_plugin_scopes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_group_plugin_scopes_bot_plugin_id'), ['bot_plugin_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_plugin_scopes_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_plugin_scopes_chatroom_id'), ['chatroom_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_plugin_scopes_scope_status'), ['scope_status'], unique=False)

    op.create_table('messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('sender_id', sa.String(length=128), nullable=True),
    sa.Column('sender_name', sa.String(length=128), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('message_type', sa.String(length=30), nullable=False),
    sa.Column('external_message_id', sa.String(length=128), nullable=True),
    sa.Column('external_status', sa.String(length=30), nullable=True),
    sa.Column('raw_request_json', sa.Text(), nullable=True),
    sa.Column('raw_response_json', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_messages_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_messages_direction'), ['direction'], unique=False)

    op.create_table('open_api_app_channels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('app_id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['open_api_apps.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id', 'channel_id', name='uq_open_api_app_channel')
    )
    with op.batch_alter_table('open_api_app_channels', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_open_api_app_channels_app_id'), ['app_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_open_api_app_channels_channel_id'), ['channel_id'], unique=False)

    op.create_table('weather_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=True),
    sa.Column('channel_id', sa.Integer(), nullable=True),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('chat_type', sa.String(length=20), nullable=False),
    sa.Column('target_external_id', sa.String(length=128), nullable=False),
    sa.Column('subscriber_id', sa.String(length=128), nullable=True),
    sa.Column('city_query', sa.String(length=255), nullable=False),
    sa.Column('city_name', sa.String(length=255), nullable=False),
    sa.Column('city_id', sa.String(length=64), nullable=False),
    sa.Column('schedule_hour', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('next_push_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_push_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('config_json', sa.Text(), nullable=True),
    sa.Column('latest_snapshot_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('weather_subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_chat_type'), ['chat_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_city_id'), ['city_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_next_push_at'), ['next_push_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_subscriber_id'), ['subscriber_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_weather_subscriptions_target_external_id'), ['target_external_id'], unique=False)

    op.create_table('workwechat_contact_relations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=False),
    sa.Column('owner_userid', sa.String(length=128), nullable=True),
    sa.Column('owner_name', sa.String(length=255), nullable=True),
    sa.Column('relationship_status', sa.String(length=30), nullable=False),
    sa.Column('is_deleted', sa.Boolean(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delete_reason', sa.String(length=50), nullable=True),
    sa.Column('delete_event_key', sa.String(length=128), nullable=True),
    sa.Column('remark', sa.String(length=255), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('remark_corp_name', sa.String(length=255), nullable=True),
    sa.Column('remark_mobiles_json', sa.Text(), nullable=True),
    sa.Column('tags_json', sa.Text(), nullable=True),
    sa.Column('add_way', sa.String(length=50), nullable=True),
    sa.Column('state', sa.String(length=128), nullable=True),
    sa.Column('oper_userid', sa.String(length=128), nullable=True),
    sa.Column('createtime', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('raw_follow_user_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['workwechat_contacts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('workwechat_contact_relations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workwechat_contact_relations_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contact_relations_contact_id'), ['contact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contact_relations_is_deleted'), ['is_deleted'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contact_relations_owner_userid'), ['owner_userid'], unique=False)
        batch_op.create_index(batch_op.f('ix_workwechat_contact_relations_relationship_status'), ['relationship_status'], unique=False)

    op.create_table('customer_operation_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('contact_id', sa.Integer(), nullable=True),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('request_id', sa.String(length=128), nullable=False),
    sa.Column('source', sa.String(length=100), nullable=True),
    sa.Column('scene', sa.String(length=100), nullable=True),
    sa.Column('operation_type', sa.String(length=50), nullable=False),
    sa.Column('content_type', sa.String(length=50), nullable=False),
    sa.Column('target_contact_external_id', sa.String(length=128), nullable=True),
    sa.Column('target_external_userid', sa.String(length=128), nullable=True),
    sa.Column('target_userid', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('status_reason', sa.String(length=255), nullable=True),
    sa.Column('content_json', sa.Text(), nullable=True),
    sa.Column('biz_payload_json', sa.Text(), nullable=True),
    sa.Column('raw_payload_json', sa.Text(), nullable=True),
    sa.Column('adapter_result_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['workwechat_contacts.id'], ),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.ForeignKeyConstraint(['job_id'], ['task_jobs.id'], ),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel_id', 'request_id', name='uq_customer_operation_request_channel_request')
    )
    with op.batch_alter_table('customer_operation_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_contact_id'), ['contact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_content_type'), ['content_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_operation_type'), ['operation_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_request_id'), ['request_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_scene'), ['scene'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_target_contact_external_id'), ['target_contact_external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_target_external_userid'), ['target_external_userid'], unique=False)
        batch_op.create_index(batch_op.f('ix_customer_operation_requests_target_userid'), ['target_userid'], unique=False)

    op.create_table('message_attachments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('raw_event_id', sa.Integer(), nullable=True),
    sa.Column('channel_id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('attachment_type', sa.String(length=30), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('storage_path', sa.Text(), nullable=True),
    sa.Column('mime_type', sa.String(length=100), nullable=True),
    sa.Column('file_size', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('metadata_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
    sa.ForeignKeyConstraint(['raw_event_id'], ['raw_webhook_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('message_attachments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_message_attachments_attachment_type'), ['attachment_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_direction'), ['direction'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_provider'), ['provider'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_raw_event_id'), ['raw_event_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_attachments_status'), ['status'], unique=False)

    op.create_table('plugin_execution_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bot_id', sa.Integer(), nullable=True),
    sa.Column('channel_id', sa.Integer(), nullable=True),
    sa.Column('conversation_id', sa.Integer(), nullable=True),
    sa.Column('message_id', sa.Integer(), nullable=True),
    sa.Column('raw_event_id', sa.Integer(), nullable=True),
    sa.Column('plugin_id', sa.Integer(), nullable=True),
    sa.Column('bot_plugin_id', sa.Integer(), nullable=True),
    sa.Column('plugin_code', sa.String(length=100), nullable=False),
    sa.Column('channel_type', sa.String(length=50), nullable=True),
    sa.Column('chat_type', sa.String(length=20), nullable=True),
    sa.Column('message_type', sa.String(length=30), nullable=True),
    sa.Column('sender_id', sa.String(length=128), nullable=True),
    sa.Column('conversation_external_id', sa.String(length=128), nullable=True),
    sa.Column('matched', sa.Boolean(), nullable=False),
    sa.Column('attempted', sa.Boolean(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('consume_policy', sa.String(length=30), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('outbound_count', sa.Integer(), nullable=False),
    sa.Column('trigger_source', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('details_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['bot_id'], ['bots.id'], ),
    sa.ForeignKeyConstraint(['bot_plugin_id'], ['bot_plugins.id'], ),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
    sa.ForeignKeyConstraint(['plugin_id'], ['plugins.id'], ),
    sa.ForeignKeyConstraint(['raw_event_id'], ['raw_webhook_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plugin_execution_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_attempted'), ['attempted'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_bot_id'), ['bot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_bot_plugin_id'), ['bot_plugin_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_channel_type'), ['channel_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_chat_type'), ['chat_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_conversation_external_id'), ['conversation_external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_matched'), ['matched'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_message_type'), ['message_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_ok'), ['ok'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_plugin_code'), ['plugin_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_plugin_id'), ['plugin_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_raw_event_id'), ['raw_event_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_sender_id'), ['sender_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_plugin_execution_logs_trigger_source'), ['trigger_source'], unique=False)



def downgrade() -> None:
    """把数据库回退到上一版本。"""
    # 基线之前不存在任何 Alembic 管理的业务表，因此回退需要按外键依赖的逆序完整删除。
    with op.batch_alter_table('plugin_execution_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_trigger_source'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_status'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_sender_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_raw_event_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_plugin_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_plugin_code'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_ok'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_message_type'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_message_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_matched'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_conversation_external_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_chat_type'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_channel_type'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_channel_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_bot_plugin_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_bot_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_execution_logs_attempted'))

    op.drop_table('plugin_execution_logs')
    with op.batch_alter_table('message_attachments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_message_attachments_status'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_raw_event_id'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_provider'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_message_id'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_direction'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_channel_id'))
        batch_op.drop_index(batch_op.f('ix_message_attachments_attachment_type'))

    op.drop_table('message_attachments')
    with op.batch_alter_table('customer_operation_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_target_userid'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_target_external_userid'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_target_contact_external_id'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_status'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_source'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_scene'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_request_id'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_operation_type'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_message_id'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_job_id'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_content_type'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_contact_id'))
        batch_op.drop_index(batch_op.f('ix_customer_operation_requests_channel_id'))

    op.drop_table('customer_operation_requests')
    with op.batch_alter_table('workwechat_contact_relations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workwechat_contact_relations_relationship_status'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contact_relations_owner_userid'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contact_relations_is_deleted'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contact_relations_contact_id'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contact_relations_channel_id'))

    op.drop_table('workwechat_contact_relations')
    with op.batch_alter_table('weather_subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_target_external_id'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_subscriber_id'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_status'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_next_push_at'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_city_id'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_chat_type'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_channel_id'))
        batch_op.drop_index(batch_op.f('ix_weather_subscriptions_bot_id'))

    op.drop_table('weather_subscriptions')
    with op.batch_alter_table('open_api_app_channels', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_open_api_app_channels_channel_id'))
        batch_op.drop_index(batch_op.f('ix_open_api_app_channels_app_id'))

    op.drop_table('open_api_app_channels')
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_messages_direction'))
        batch_op.drop_index(batch_op.f('ix_messages_conversation_id'))

    op.drop_table('messages')
    with op.batch_alter_table('group_plugin_scopes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_group_plugin_scopes_scope_status'))
        batch_op.drop_index(batch_op.f('ix_group_plugin_scopes_chatroom_id'))
        batch_op.drop_index(batch_op.f('ix_group_plugin_scopes_channel_id'))
        batch_op.drop_index(batch_op.f('ix_group_plugin_scopes_bot_plugin_id'))

    op.drop_table('group_plugin_scopes')
    with op.batch_alter_table('chatroom_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chatroom_members_status'))
        batch_op.drop_index(batch_op.f('ix_chatroom_members_member_external_id'))
        batch_op.drop_index(batch_op.f('ix_chatroom_members_chatroom_id'))
        batch_op.drop_index(batch_op.f('ix_chatroom_members_channel_id'))
        batch_op.drop_index(batch_op.f('ix_chatroom_members_bot_id'))

    op.drop_table('chatroom_members')
    with op.batch_alter_table('workwechat_contacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_userid'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_sync_status'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_source'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_external_userid'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_conversation_external_id'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_contact_kind'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_contact_external_id'))
        batch_op.drop_index(batch_op.f('ix_workwechat_contacts_channel_id'))

    op.drop_table('workwechat_contacts')
    with op.batch_alter_table('raw_webhook_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_raw_webhook_events_source'))
        batch_op.drop_index(batch_op.f('ix_raw_webhook_events_event_key'))
        batch_op.drop_index(batch_op.f('ix_raw_webhook_events_channel_id'))

    op.drop_table('raw_webhook_events')
    with op.batch_alter_table('plugin_activity_hourly', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_plugin_activity_hourly_plugin_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_activity_hourly_plugin_code'))
        batch_op.drop_index(batch_op.f('ix_plugin_activity_hourly_channel_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_activity_hourly_bucket_start'))
        batch_op.drop_index(batch_op.f('ix_plugin_activity_hourly_bot_plugin_id'))
        batch_op.drop_index(batch_op.f('ix_plugin_activity_hourly_bot_id'))

    op.drop_table('plugin_activity_hourly')
    with op.batch_alter_table('open_api_apps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_open_api_apps_status'))
        batch_op.drop_index(batch_op.f('ix_open_api_apps_name'))
        batch_op.drop_index(batch_op.f('ix_open_api_apps_default_channel_id'))
        batch_op.drop_index(batch_op.f('ix_open_api_apps_bot_id'))
        batch_op.drop_index(batch_op.f('ix_open_api_apps_app_secret_hash'))
        batch_op.drop_index(batch_op.f('ix_open_api_apps_app_key'))

    op.drop_table('open_api_apps')
    with op.batch_alter_table('message_activity_hourly', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_message_activity_hourly_direction'))
        batch_op.drop_index(batch_op.f('ix_message_activity_hourly_chat_type'))
        batch_op.drop_index(batch_op.f('ix_message_activity_hourly_channel_type'))
        batch_op.drop_index(batch_op.f('ix_message_activity_hourly_channel_id'))
        batch_op.drop_index(batch_op.f('ix_message_activity_hourly_bucket_start'))
        batch_op.drop_index(batch_op.f('ix_message_activity_hourly_bot_id'))

    op.drop_table('message_activity_hourly')
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversations_external_id'))
        batch_op.drop_index(batch_op.f('ix_conversations_channel_type'))
        batch_op.drop_index(batch_op.f('ix_conversations_channel_id'))
        batch_op.drop_index(batch_op.f('ix_conversations_bot_id'))

    op.drop_table('conversations')
    with op.batch_alter_table('chatrooms', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chatrooms_status'))
        batch_op.drop_index(batch_op.f('ix_chatrooms_registration_source'))
        batch_op.drop_index(batch_op.f('ix_chatrooms_management_status'))
        batch_op.drop_index(batch_op.f('ix_chatrooms_is_active'))
        batch_op.drop_index(batch_op.f('ix_chatrooms_external_id'))
        batch_op.drop_index(batch_op.f('ix_chatrooms_channel_id'))
        batch_op.drop_index(batch_op.f('ix_chatrooms_bot_id'))

    op.drop_table('chatrooms')
    with op.batch_alter_table('sign_in_history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sign_in_history_user_id'))
        batch_op.drop_index(batch_op.f('ix_sign_in_history_signed_at'))
        batch_op.drop_index(batch_op.f('ix_sign_in_history_record_id'))
        batch_op.drop_index(batch_op.f('ix_sign_in_history_is_makeup'))
        batch_op.drop_index(batch_op.f('ix_sign_in_history_group_id'))

    op.drop_table('sign_in_history')
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_channels_channel_type'))
        batch_op.drop_index(batch_op.f('ix_channels_bot_id'))

    op.drop_table('channels')
    with op.batch_alter_table('bot_plugins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bot_plugins_plugin_id'))
        batch_op.drop_index(batch_op.f('ix_bot_plugins_bot_id'))

    op.drop_table('bot_plugins')
    with op.batch_alter_table('task_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_task_jobs_related_type'))
        batch_op.drop_index(batch_op.f('ix_task_jobs_related_id'))
        batch_op.drop_index(batch_op.f('ix_task_jobs_priority'))
        batch_op.drop_index(batch_op.f('ix_task_jobs_job_type'))
        batch_op.drop_index(batch_op.f('ix_task_jobs_available_at'))

    op.drop_table('task_jobs')
    with op.batch_alter_table('sign_in_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sign_in_records_user_id'))
        batch_op.drop_index(batch_op.f('ix_sign_in_records_group_id'))

    op.drop_table('sign_in_records')
    with op.batch_alter_table('points_transactions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_points_transactions_user_id'))
        batch_op.drop_index(batch_op.f('ix_points_transactions_source'))
        batch_op.drop_index(batch_op.f('ix_points_transactions_group_id'))
        batch_op.drop_index(batch_op.f('ix_points_transactions_counterparty_user_id'))

    op.drop_table('points_transactions')
    with op.batch_alter_table('points_prison_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_points_prison_records_user_id'))
        batch_op.drop_index(batch_op.f('ix_points_prison_records_status'))
        batch_op.drop_index(batch_op.f('ix_points_prison_records_start_at'))
        batch_op.drop_index(batch_op.f('ix_points_prison_records_group_id'))
        batch_op.drop_index(batch_op.f('ix_points_prison_records_end_at'))

    op.drop_table('points_prison_records')
    with op.batch_alter_table('points_accounts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_points_accounts_user_id'))
        batch_op.drop_index(batch_op.f('ix_points_accounts_group_id'))

    op.drop_table('points_accounts')
    with op.batch_alter_table('plugins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_plugins_code'))

    op.drop_table('plugins')
    with op.batch_alter_table('health_check_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_health_check_records_target_type'))
        batch_op.drop_index(batch_op.f('ix_health_check_records_target_id'))
        batch_op.drop_index(batch_op.f('ix_health_check_records_status'))
        batch_op.drop_index(batch_op.f('ix_health_check_records_scope'))

    op.drop_table('health_check_records')
    with op.batch_alter_table('error_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_error_logs_source'))

    op.drop_table('error_logs')
    with op.batch_alter_table('bots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bots_name'))
        batch_op.drop_index(batch_op.f('ix_bots_id'))

    op.drop_table('bots')
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_logs_target_type'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_target_id'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_actor'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_action'))

    op.drop_table('audit_logs')
    with op.batch_alter_table('alert_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alert_records_status'))
        batch_op.drop_index(batch_op.f('ix_alert_records_source'))
        batch_op.drop_index(batch_op.f('ix_alert_records_rule_key'))
        batch_op.drop_index(batch_op.f('ix_alert_records_related_id'))
        batch_op.drop_index(batch_op.f('ix_alert_records_level'))

    op.drop_table('alert_records')
    with op.batch_alter_table('admin_users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_admin_users_username'))
        batch_op.drop_index(batch_op.f('ix_admin_users_role'))

    op.drop_table('admin_users')
