"""统一渠道后台任务的租户隔离协调器。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel
from app.models.saas import Tenant, TenantSubscription
from app.services.channel_catalog import (
    channel_supports,
    get_channel_type_definition,
    list_channel_type_definitions,
)
from app.services.telegram_polling import poll_telegram_channel
from app.services.wechat_ipad import (
    auto_recover_wechat_ipad_login,
    heartbeat_wechat_ipad_channel,
    pull_wechat_ipad_messages,
)


logger = get_logger(__name__)
ChannelRunner = Callable[[Session, Channel], dict[str, Any]]
ChannelEligibility = Callable[[Channel], bool]


@dataclass(frozen=True)
class ChannelRuntimeTask:
    """声明单项后台任务的渠道、能力、调度间隔和运行状态条件。"""

    name: str
    channel_type: str
    required_capability: str
    interval_seconds: float
    eligible: ChannelEligibility


@dataclass(frozen=True)
class ChannelRuntimeResult:
    """返回一次协调扫描的可观测结果，Worker 只依赖 worked 决定是否短暂休眠。"""

    worked: bool
    attempted_tasks: int
    completed_tasks: int
    failed_tasks: int
    skipped_tenants: int


def _always_eligible(_channel: Channel) -> bool:
    return True


def _wechat_ipad_online(channel: Channel) -> bool:
    return str(channel.login_state or "").strip() == "online"


def _wechat_ipad_recoverable(channel: Channel) -> bool:
    return str(channel.login_state or "").strip() in {
        "offline",
        "reconnect_failed",
        "reconnect_blocked",
        "idle",
        "error",
    }


CHANNEL_RUNTIME_TASKS = (
    ChannelRuntimeTask(
        name="telegram_poll",
        channel_type="telegram",
        required_capability="polling",
        interval_seconds=3.0,
        eligible=_always_eligible,
    ),
    ChannelRuntimeTask(
        name="wechat_ipad_message_sync",
        channel_type="wechat_ipad",
        required_capability="polling",
        interval_seconds=2.0,
        eligible=_wechat_ipad_online,
    ),
    ChannelRuntimeTask(
        name="wechat_ipad_heartbeat",
        channel_type="wechat_ipad",
        required_capability="health",
        interval_seconds=5.0,
        eligible=_wechat_ipad_online,
    ),
    ChannelRuntimeTask(
        name="wechat_ipad_auto_recover",
        channel_type="wechat_ipad",
        required_capability="login",
        interval_seconds=30.0,
        eligible=_wechat_ipad_recoverable,
    ),
)

PLATFORM_POLLING_CHANNEL_TYPES = tuple(
    definition.code
    for definition in list_channel_type_definitions()
    if definition.runtime_mode == "platform_polling"
)

DEFAULT_CHANNEL_RUNNERS: dict[str, ChannelRunner] = {
    "telegram_poll": poll_telegram_channel,
    "wechat_ipad_message_sync": pull_wechat_ipad_messages,
    "wechat_ipad_heartbeat": heartbeat_wechat_ipad_channel,
    "wechat_ipad_auto_recover": auto_recover_wechat_ipad_login,
}

# 到期键包含可信租户主键，避免未来拆库或数据迁移后相同渠道主键共享调度状态。
_next_due_at: dict[tuple[int, int, str], float] = {}


def _list_active_tenant_ids() -> list[int]:
    """平台会话只枚举有效租户主键，不读取或处理任何租户业务对象。"""

    with platform_session(SessionLocal) as db:
        return [
            int(tenant_id)
            for tenant_id in db.scalars(
                select(Tenant.id).where(Tenant.status == "active").order_by(Tenant.id.asc())
            ).all()
        ]


def _tenant_runtime_allowed(db: Session, tenant_id: int) -> bool:
    """订阅停用、挂起、未开始或到期时停止主动执行，但不修改租户现有数据。"""

    current = now_shanghai()
    subscription = db.scalar(
        select(TenantSubscription)
        .where(
            TenantSubscription.tenant_id == int(tenant_id),
            TenantSubscription.starts_at <= current,
        )
        .order_by(TenantSubscription.starts_at.desc(), TenantSubscription.id.desc())
        .limit(1)
    )
    if subscription is None or subscription.status != "active":
        return False
    if subscription.expires_at is not None and ensure_shanghai_datetime(subscription.expires_at) <= current:
        return False
    return True


def _task_specs_for_channel(channel: Channel) -> tuple[ChannelRuntimeTask, ...]:
    """从统一目录和任务注册表解析可运行任务，不在协调器中写协议分支链。"""

    definition = get_channel_type_definition(channel.channel_type)
    if definition.runtime_mode != "platform_polling":
        return ()
    return tuple(
        task
        for task in CHANNEL_RUNTIME_TASKS
        if task.channel_type == definition.code
        and channel_supports(definition.code, task.required_capability)
        and task.eligible(channel)
    )


def _load_runtime_channel_ids(db: Session) -> list[int]:
    """当前租户会话只返回启用渠道主键，具体任务执行时再用独立会话重新加载。"""

    return [
        int(channel_id)
        for channel_id in db.scalars(
            select(Channel.id)
            .where(
                Channel.is_enabled.is_(True),
                Channel.deleted_at.is_(None),
                Channel.channel_type.in_(PLATFORM_POLLING_CHANNEL_TYPES),
            )
            .order_by(Channel.id.asc())
        ).all()
    ]


def run_due_channel_tasks(
    *,
    now_monotonic: float | None = None,
    runners: Mapping[str, ChannelRunner] | None = None,
) -> ChannelRuntimeResult:
    """按租户和渠道执行到期任务，单项失败不阻断其他任务或租户。"""

    current_monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
    resolved_runners = dict(DEFAULT_CHANNEL_RUNNERS)
    if runners:
        resolved_runners.update(runners)

    attempted_tasks = 0
    completed_tasks = 0
    failed_tasks = 0
    skipped_tenants = 0
    active_schedule_keys: set[tuple[int, int, str]] = set()

    for tenant_id in _list_active_tenant_ids():
        with tenant_scope(tenant_id):
            with tenant_session(TenantSessionLocal) as discovery_db:
                if not _tenant_runtime_allowed(discovery_db, tenant_id):
                    skipped_tenants += 1
                    continue
                channel_ids = _load_runtime_channel_ids(discovery_db)

            for channel_id in channel_ids:
                # 任务发现与协议调用使用不同租户会话。协议异常导致事务失败时，
                # tenant_session 会回滚并关闭当前会话，随后任务从干净会话继续。
                with tenant_session(TenantSessionLocal) as definition_db:
                    channel = definition_db.get(Channel, channel_id)
                    if channel is None or not channel.is_enabled:
                        continue
                    task_specs = _task_specs_for_channel(channel)

                for task in task_specs:
                    schedule_key = (tenant_id, channel_id, task.name)
                    active_schedule_keys.add(schedule_key)
                    if current_monotonic < _next_due_at.get(schedule_key, 0.0):
                        continue
                    runner = resolved_runners.get(task.name)
                    if runner is None:
                        logger.error(
                            "channel runtime runner missing",
                            extra={"tenant_id": tenant_id, "channel_id": channel_id, "task_name": task.name},
                        )
                        continue

                    try:
                        with tenant_session(TenantSessionLocal) as task_db:
                            task_channel = task_db.get(Channel, channel_id)
                            if task_channel is None or not task_channel.is_enabled or not task.eligible(task_channel):
                                continue
                            attempted_tasks += 1
                            _next_due_at[schedule_key] = current_monotonic + task.interval_seconds
                            result = runner(task_db, task_channel)
                            completed_tasks += 1
                            logger.debug(
                                "channel runtime task completed",
                                extra={
                                    "tenant_id": tenant_id,
                                    "channel_id": channel_id,
                                    "channel_type": task_channel.channel_type,
                                    "task_name": task.name,
                                    # 协议返回可能包含第三方原始数据，只记录稳定状态位，
                                    # 避免调试日志意外保存消息正文、账号资料或访问凭证。
                                    "ok": bool(result.get("ok")) if isinstance(result, dict) else True,
                                    "skipped": bool(result.get("skipped")) if isinstance(result, dict) else False,
                                },
                            )
                    except Exception:
                        failed_tasks += 1
                        logger.exception(
                            "channel runtime task failed",
                            extra={
                                "tenant_id": tenant_id,
                                "channel_id": channel_id,
                                "task_name": task.name,
                            },
                        )

    # 删除已停用、已删除或不再具备对应状态的任务键，防止长期运行进程积累无效调度项。
    for schedule_key in tuple(_next_due_at):
        if schedule_key not in active_schedule_keys:
            _next_due_at.pop(schedule_key, None)

    return ChannelRuntimeResult(
        worked=attempted_tasks > 0,
        attempted_tasks=attempted_tasks,
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        skipped_tenants=skipped_tenants,
    )
