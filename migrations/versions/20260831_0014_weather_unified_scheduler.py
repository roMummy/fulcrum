"""清理天气订阅明文配置并停用专用任务。

Revision ID: 20260831_0014
Revises: 20260831_0013
Create Date: 2026-08-31 23:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0014"
down_revision: Union[str, Sequence[str], None] = "20260831_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """删除订阅表中的历史密钥副本，并取消不再执行的 weather_push 任务。"""

    weather_subscriptions = sa.table(
        "weather_subscriptions",
        sa.column("config_json", sa.Text()),
    )
    task_jobs = sa.table(
        "task_jobs",
        sa.column("job_type", sa.String(length=50)),
        sa.column("status", sa.String(length=20)),
        sa.column("finished_at", sa.DateTime(timezone=True)),
    )
    # 旧实现把 BotPlugin 的完整配置复制到每条订阅，其中可能包含 API Key。
    # 新实现每次执行都从绑定密文读取凭证，因此历史副本必须一次性清空。
    op.execute(weather_subscriptions.update().values(config_json=None))
    # 平台已经改用 plugin_scheduled_action。遗留专用任务若继续保持 pending，
    # Worker 将永远无法消费并持续占用运维视图，因此在迁移时明确终止。
    op.execute(
        task_jobs.update()
        .where(
            task_jobs.c.job_type == "weather_push",
            task_jobs.c.status.in_(("pending", "running")),
        )
        .values(status="cancelled", finished_at=sa.func.now())
    )


def downgrade() -> None:
    """安全清理不可逆；降级不会恢复已删除的明文密钥或重新激活旧任务。"""

