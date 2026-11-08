from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import SHANGHAI_TZ, now_shanghai
from app.models.entities import Channel, Chatroom, Conversation, Message, MessageAttachment, TaskJob
from app.models.saas import ProviderNodeAccount
from app.services.open_webhooks import dispatch_open_message_event, dispatch_open_send_result_event
from app.services.task_queue import (
    OUTBOUND_DISPATCH_JOB,
    WORKWECHAT_CLOUD_CONTROL_JOB,
    WORKWECHAT_CLOUD_OUTBOUND_JOB,
    complete_job,
    fail_job,
    fail_job_terminal,
    parse_job_payload,
)
from app.services.workwechat_ws_sessions import workwechat_ws_registry


DEFAULT_POLL_BATCH_SIZE = 5
DEFAULT_POLL_INTERVAL_SECONDS = 3
DEFAULT_AUTH_CODE_TTL_HOURS = 24
DEFAULT_RUNNING_LEASE_SECONDS = 15 * 60


def build_token_hash(value: str) -> str:
    # 云端只保存授权码和 client_token 的哈希，避免数据库泄漏后直接接管企业 provider。
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def generate_secret_token(prefix: str) -> str:
    # token 使用 URL 安全字符，便于放在本地 config.json 和 HTTP header 中。
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def load_adapter_config(channel: Channel) -> dict[str, Any]:
    raw = str(getattr(channel, "adapter_config_json", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_adapter_config(channel: Channel, config: dict[str, Any]) -> None:
    # adapter_config_json 是当前项目已有的渠道级扩展配置位，先复用它承载主动拉取状态。
    channel.adapter_config_json = json.dumps(config, ensure_ascii=False, sort_keys=True)


def load_cloud_poll_config(channel: Channel) -> dict[str, Any]:
    config = load_adapter_config(channel)
    config.setdefault("poll_batch_size", DEFAULT_POLL_BATCH_SIZE)
    config.setdefault("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    config.setdefault("running_lease_seconds", DEFAULT_RUNNING_LEASE_SECONDS)
    return config


def is_cloud_poll_enabled(channel: Channel) -> bool:
    if getattr(channel, "channel_type", "") != "workwechat":
        return False
    config = load_adapter_config(channel)
    return str(config.get("workwechat_delivery_mode") or "").strip() == "cloud_poll"


def resolve_outbound_job_type(channel: Channel) -> str:
    # 远端手机渠道必须由手机 poll 领取，不能交给云端 worker 直接调用不存在的本地 HTTP。
    if str(channel.channel_type or "").strip() == "wechat_webridge":
        from app.services.wechat_webridge.contract import JOB_TYPE

        return JOB_TYPE
    # cloud_poll 模式的任务由企业本地 provider 通过 WS/HTTP 领取，不能被云端 worker 直接发送。
    return WORKWECHAT_CLOUD_OUTBOUND_JOB if is_cloud_poll_enabled(channel) else OUTBOUND_DISPATCH_JOB


def notify_cloud_job_enqueued(channel: Channel, job: TaskJob) -> dict[str, int]:
    # 通知只是实时唤醒；即使没有在线客户端，任务仍留在队列里等待补偿 poll。
    if str(getattr(job, "job_type", "") or "") not in {WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB}:
        return {"sent": 0, "failed": 0}
    return workwechat_ws_registry.notify_job_available(channel_id=int(channel.id or 0), job_id=int(job.id or 0))


def claim_cloud_jobs(db: Session, channel: Channel, *, batch_size: int = DEFAULT_POLL_BATCH_SIZE) -> list[dict[str, Any]]:
    # 只领取主动拉取专用任务，避免和云端后台 worker 的 direct_http 任务混用。
    limit = max(1, int(batch_size or DEFAULT_POLL_BATCH_SIZE))
    config = load_cloud_poll_config(channel)
    recover_stale_cloud_jobs(
        db,
        channel,
        stale_seconds=int(config.get("running_lease_seconds") or DEFAULT_RUNNING_LEASE_SECONDS),
    )
    candidates = db.scalars(
        select(TaskJob)
        .where(
            TaskJob.job_type.in_((WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB)),
            TaskJob.status == "pending",
            TaskJob.available_at <= now_shanghai(),
        )
        .order_by(TaskJob.priority.desc(), TaskJob.id.asc())
        .limit(limit * 3)
    ).all()
    claimed: list[dict[str, Any]] = []
    for job in candidates:
        payload = parse_job_payload(job)
        if int(payload.get("channel_id") or 0) != int(channel.id or 0):
            continue
        conversation = None
        if str(job.job_type or "") == WORKWECHAT_CLOUD_OUTBOUND_JOB:
            conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
            if not conversation:
                continue
        job.status = "running"
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = now_shanghai()
        job.locked_at = now_shanghai()
        claimed.append(_serialize_cloud_job(job, payload, conversation))
        if len(claimed) >= limit:
            break
    db.commit()
    return claimed


def claim_provider_node_jobs(
    db: Session,
    *,
    node_id: int,
    batch_size: int = DEFAULT_POLL_BATCH_SIZE,
    lease_seconds: int = DEFAULT_RUNNING_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    """领取当前节点下多个已绑定企微账号的任务，并签发一次性租约令牌。"""

    limit = max(1, min(50, int(batch_size or DEFAULT_POLL_BATCH_SIZE)))
    current = now_shanghai()
    candidates = db.scalars(
        select(TaskJob)
        .where(
            TaskJob.job_type.in_((WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB)),
            TaskJob.status == "pending",
            TaskJob.provider_node_id == int(node_id),
            TaskJob.provider_account_id.is_not(None),
            TaskJob.available_at <= current,
        )
        .order_by(TaskJob.priority.desc(), TaskJob.id.asc())
        .limit(limit * 4)
        .with_for_update(skip_locked=True)
    ).all()
    claimed: list[dict[str, Any]] = []
    for job in candidates:
        account = db.get(ProviderNodeAccount, int(job.provider_account_id or 0))
        payload = parse_job_payload(job)
        channel = db.get(Channel, int(payload.get("channel_id") or 0))
        if (
            account is None
            or channel is None
            or int(account.provider_node_id) != int(node_id)
            or int(channel.provider_node_id or 0) != int(node_id)
            or int(channel.provider_account_id or 0) != int(account.id)
            or int(channel.tenant_id) != int(job.tenant_id)
            or int(account.tenant_id) != int(job.tenant_id)
        ):
            # 绑定不完整的任务保持 pending，等待管理员修正渠道；不能把它发给任意账号。
            continue
        conversation = None
        if str(job.job_type or "") == WORKWECHAT_CLOUD_OUTBOUND_JOB:
            conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
            if conversation is None:
                continue

        raw_lease_token = generate_secret_token("please")
        lease_expires_at = current + timedelta(seconds=max(30, int(lease_seconds or DEFAULT_RUNNING_LEASE_SECONDS)))
        job.status = "running"
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = current
        job.locked_at = current
        job.claimed_at = current
        job.lease_token_hash = build_token_hash(raw_lease_token)
        job.lease_expires_at = lease_expires_at
        serialized = _serialize_cloud_job(job, payload, conversation)
        serialized.update(
            {
                "provider_node_id": int(node_id),
                "provider_account_id": int(account.id),
                "provider_account_key": account.account_key,
                "lease_token": raw_lease_token,
                "lease_expires_at": lease_expires_at.isoformat(),
            }
        )
        claimed.append(serialized)
        if len(claimed) >= limit:
            break
    db.commit()
    return claimed


def complete_provider_node_job(
    db: Session,
    *,
    node_id: int,
    job_id: int,
    lease_token: str,
    result_payload: dict[str, Any],
) -> dict[str, Any]:
    """校验设备节点和任务租约后完成任务；重复完成保持幂等。"""

    job, channel = _load_provider_node_leased_job(
        db,
        node_id=node_id,
        job_id=job_id,
        lease_token=lease_token,
        allow_completed=True,
    )
    if job.status == "completed":
        return {"ok": True, "job_id": job.id, "status": "completed", "idempotent": True}
    # 节点级请求正文额外携带一次性租约原文。旧渠道协议会把 cloud_result
    # 持久化用于排障，因此必须在进入兼容完成逻辑前删除鉴权字段；任务表已经
    # 单独保存 lease_token_hash，不需要也不允许再次保存原始租约。
    persisted_result = {key: value for key, value in result_payload.items() if key != "lease_token"}
    return complete_cloud_job(db, channel, job.id, persisted_result)


def fail_provider_node_job(
    db: Session,
    *,
    node_id: int,
    job_id: int,
    lease_token: str,
    error_code: str,
    error_message: str,
    adapter_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验节点租约后记录失败，不能回报其他节点或过期租约任务。"""

    job, channel = _load_provider_node_leased_job(
        db,
        node_id=node_id,
        job_id=job_id,
        lease_token=lease_token,
        allow_completed=False,
    )
    return fail_cloud_job(
        db,
        channel,
        job.id,
        error_code=error_code,
        error_message=error_message,
        adapter_result=adapter_result,
    )


def _load_provider_node_leased_job(
    db: Session,
    *,
    node_id: int,
    job_id: int,
    lease_token: str,
    allow_completed: bool,
) -> tuple[TaskJob, Channel]:
    """同时校验任务节点、渠道绑定、租约摘要和租约有效期。"""

    job = db.get(TaskJob, int(job_id))
    if job is None or int(job.provider_node_id or 0) != int(node_id):
        raise ValueError("job_not_found")
    payload = parse_job_payload(job)
    channel = db.get(Channel, int(payload.get("channel_id") or 0))
    if (
        channel is None
        or int(channel.provider_node_id or 0) != int(node_id)
        or int(channel.provider_account_id or 0) != int(job.provider_account_id or 0)
    ):
        raise ValueError("job_not_found")
    if allow_completed and job.status == "completed":
        return job, channel
    if job.status != "running":
        raise ValueError("job_not_running")
    normalized_lease = (lease_token or "").strip()
    if not normalized_lease or build_token_hash(normalized_lease) != str(job.lease_token_hash or ""):
        raise ValueError("lease_token_invalid")
    if job.lease_expires_at is None or _parse_datetime(job.lease_expires_at) <= now_shanghai():
        raise ValueError("lease_expired")
    return job, channel


def recover_stale_cloud_jobs(db: Session, channel: Channel, *, stale_seconds: int = DEFAULT_RUNNING_LEASE_SECONDS) -> int:
    """处理长时间没有 provider 回报的云端任务。

    provider 领取任务后会先把 TaskJob 标记为 running。控制类任务没有直接发送业务消息，
    租约过期后仍可以恢复为 pending；出站发单任务则不同，provider 可能已经把文件/消息交给
    企微 Hook，只是客户端退出导致云端没有收到回报。这里把出站任务固化为“未确认完成”，
    让业务侧在批次错误摘要里关注，不再恢复成 pending 造成自动重发。
    """

    lease_seconds = max(60, int(stale_seconds or DEFAULT_RUNNING_LEASE_SECONDS))
    stale_before = now_shanghai() - timedelta(seconds=lease_seconds)
    jobs = db.scalars(
        select(TaskJob)
        .where(
            TaskJob.job_type.in_((WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB)),
            TaskJob.status == "running",
            TaskJob.locked_at <= stale_before,
        )
        .limit(200)
    ).all()
    recovered = 0
    for job in jobs:
        payload = parse_job_payload(job)
        if int(payload.get("channel_id") or 0) != int(channel.id or 0):
            continue
        if str(job.job_type or "") == WORKWECHAT_CLOUD_OUTBOUND_JOB:
            _complete_cloud_outbound_as_accepted_unconfirmed(
                db,
                channel,
                job,
                error_code="cloud_provider_lease_expired",
                error_message="provider 领取发送任务后超时未回报，已按未确认投递记录，未自动重发",
                adapter_result={
                    "ok": False,
                    "status": "cloud_provider_lease_expired",
                    "external_status": "accepted_unconfirmed",
                },
            )
            recovered += 1
            continue
        job.status = "pending"
        job.started_at = None
        job.locked_at = None
        job.error_message = "cloud_provider_lease_expired_requeued"
        recovered += 1
    if recovered:
        db.commit()
    return recovered


def complete_cloud_job(db: Session, channel: Channel, job_id: int, result_payload: dict[str, Any]) -> dict[str, Any]:
    job = _load_owned_cloud_job(db, channel, job_id)
    if job.status == "completed":
        return {"ok": True, "job_id": job.id, "status": "completed", "idempotent": True}
    if str(job.job_type or "") == WORKWECHAT_CLOUD_CONTROL_JOB:
        return _complete_cloud_control_job(db, channel, job, result_payload)
    payload = parse_job_payload(job)
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    if not conversation:
        raise ValueError("conversation_not_found")
    outbound_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    adapter_result = result_payload.get("adapter_result") if isinstance(result_payload.get("adapter_result"), dict) else {}
    message = _create_cloud_outbound_message(
        db,
        channel=channel,
        conversation=conversation,
        outbound_payload=outbound_payload,
        adapter_result=adapter_result,
    )
    complete_job(db, job, result={"message_id": message.id, "adapter_result": adapter_result, "cloud_result": result_payload})
    dispatch_open_send_result_event(
        db,
        event_type="message.sent" if adapter_result.get("ok") else "message.failed",
        channel=channel,
        conversation=conversation,
        job=job,
        message=message,
        error_message=str(adapter_result.get("error_message") or ""),
    )
    # complete_job 已经先提交发送任务完成状态；这里再提交发送结果 webhook 投递任务，
    # 避免外部业务系统异常影响 provider 的完成确认。
    db.commit()
    return {"ok": True, "job_id": job.id, "message_id": message.id, "status": "completed"}


def fail_cloud_job(
    db: Session,
    channel: Channel,
    job_id: int,
    *,
    error_code: str,
    error_message: str,
    adapter_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job = _load_owned_cloud_job(db, channel, job_id)
    if str(job.status or "") in {"completed", "failed", "cancelled"}:
        return {
            "ok": str(job.status or "") == "completed",
            "job_id": job.id,
            "status": job.status,
            "error_code": error_code,
            "idempotent": True,
        }
    if str(job.job_type or "") == WORKWECHAT_CLOUD_OUTBOUND_JOB and _is_delivery_confirmation_timeout(
        error_code=error_code,
        adapter_result=adapter_result,
    ):
        return _complete_cloud_outbound_as_accepted_unconfirmed(
            db,
            channel,
            job,
            error_code=error_code,
            error_message=error_message,
            adapter_result=adapter_result or {},
        )
    payload = parse_job_payload(job)
    conversation = None
    if str(job.job_type or "") == WORKWECHAT_CLOUD_OUTBOUND_JOB:
        conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    result = {"error_code": error_code, "adapter_result": adapter_result or {}}
    if str(job.job_type or "") == WORKWECHAT_CLOUD_OUTBOUND_JOB:
        fail_job_terminal(db, job, error_message=error_message or error_code or "cloud_job_failed", result=result)
    else:
        fail_job(db, job, error_message=error_message or error_code or "cloud_job_failed", result=result)
    if conversation is not None:
        dispatch_open_send_result_event(
            db,
            event_type="message.failed",
            channel=channel,
            conversation=conversation,
            job=job,
            message=None,
            error_message=error_message or error_code or "cloud_job_failed",
        )
        # fail_job 已经提交失败状态；发送失败通知同样走异步投递，单独提交入队结果。
        db.commit()
    return {"ok": False, "job_id": job.id, "status": job.status, "error_code": error_code}


def _is_delivery_confirmation_timeout(*, error_code: str, adapter_result: dict[str, Any] | None) -> bool:
    result = adapter_result or {}
    status_values = {
        str(error_code or "").strip(),
        str(result.get("status") or "").strip(),
        str(result.get("external_status") or "").strip(),
        str(result.get("error") or "").strip(),
        str(result.get("error_code") or "").strip(),
    }
    return "delivery_confirmation_timeout" in status_values or "accepted_unconfirmed" in status_values


def _complete_cloud_outbound_as_accepted_unconfirmed(
    db: Session,
    channel: Channel,
    job: TaskJob,
    *,
    error_code: str,
    error_message: str,
    adapter_result: dict[str, Any],
) -> dict[str, Any]:
    """把 Hook 已接收但未等到自发回显的发送任务固化为未确认完成。

    企微 Hook 的部分发送接口没有同步回执；provider 超时只能说明没有捕获到“自己发出”的回显，
    不能证明消息没有投递。这里禁止把它交给通用失败重试，否则同一个订单文件/通知会被自动重发。
    """

    payload = parse_job_payload(job)
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    if not conversation:
        raise ValueError("conversation_not_found")
    outbound_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    normalized_result = dict(adapter_result or {})
    normalized_result.update(
        {
            "ok": True,
            "status": "accepted_unconfirmed",
            "external_status": "accepted_unconfirmed",
            "error_code": error_code or "delivery_confirmation_timeout",
            "error_message": error_message or error_code or "delivery_confirmation_timeout",
            "error_summary": _build_unconfirmed_delivery_summary(error_code, error_message),
        }
    )
    message = _create_cloud_outbound_message(
        db,
        channel=channel,
        conversation=conversation,
        outbound_payload=outbound_payload,
        adapter_result=normalized_result,
    )
    complete_job(
        db,
        job,
        result={
            "message_id": message.id,
            "adapter_result": normalized_result,
            "cloud_result": {
                "accepted_unconfirmed": True,
                "error_code": error_code or "delivery_confirmation_timeout",
                "error_message": error_message or error_code or "delivery_confirmation_timeout",
            },
        },
    )
    dispatch_open_send_result_event(
        db,
        event_type="message.sent",
        channel=channel,
        conversation=conversation,
        job=job,
        message=message,
        error_message="",
    )
    db.commit()
    return {
        "ok": True,
        "job_id": job.id,
        "message_id": message.id,
        "status": "completed",
        "external_status": "accepted_unconfirmed",
    }


def _build_unconfirmed_delivery_summary(error_code: str, error_message: str) -> str:
    """生成给发单批次展示的未确认摘要。

    摘要使用业务可读中文，避免外部系统只拿到 provider 内部错误码后误判为发送失败；
    同事看到该摘要时应核对群内最新表格，而不是让系统自动补发。
    """

    code = str(error_code or "").strip() or "delivery_unconfirmed"
    message = str(error_message or "").strip()
    if code == "delivery_confirmation_timeout":
        return "企微投递未确认：Hook 已接收发送请求，但超时未捕获自发消息回显，请人工关注群内最新消息。"
    if code == "cloud_provider_lease_expired":
        return "企微投递未确认：provider 领取发送任务后超时未回报，系统未自动重发，请人工关注群内最新消息。"
    return f"企微投递未确认：{message or code}，系统未自动重发，请人工关注群内最新消息。"


def issue_authorization_code(channel: Channel, *, ttl_hours: int = DEFAULT_AUTH_CODE_TTL_HOURS) -> str:
    # 授权码只展示一次，云端保存哈希和过期时间；重新生成会让旧授权码立即失效。
    code = generate_secret_token("wwauth")
    config = load_cloud_poll_config(channel)
    expires_at = now_shanghai() + timedelta(hours=max(1, int(ttl_hours or DEFAULT_AUTH_CODE_TTL_HOURS)))
    config.update(
        {
            "workwechat_delivery_mode": "cloud_poll",
            "authorization_code_hash": build_token_hash(code),
            "authorization_code_expires_at": expires_at.isoformat(),
            "authorization_code_used_at": None,
        }
    )
    save_adapter_config(channel, config)
    return code


def issue_channel_authorization_code(db: Session, channel_id: int, *, ttl_hours: int = DEFAULT_AUTH_CODE_TTL_HOURS) -> tuple[Channel, str]:
    # 后台页面按渠道生成授权码，客户端只拿授权码兑换 client_token；
    # 这里集中校验渠道类型，避免普通微信或其他渠道误切到企微云端拉取模式。
    channel = db.get(Channel, int(channel_id))
    if not channel:
        raise ValueError("channel_not_found")
    if str(channel.channel_type or "") != "workwechat":
        raise ValueError("workwechat_channel_required")
    code = issue_authorization_code(channel, ttl_hours=ttl_hours)
    db.commit()
    return channel, code


def exchange_authorization_code(
    channel: Channel,
    authorization_code: str,
    *,
    provider_name: str = "",
    client_version: str = "",
) -> dict[str, Any]:
    # 首次注册时用一次性授权码兑换长期 client_token；兑换成功后授权码立刻不可再次使用。
    config = load_cloud_poll_config(channel)
    if config.get("authorization_code_used_at"):
        raise ValueError("authorization_code_used")
    expected_hash = str(config.get("authorization_code_hash") or "").strip()
    if not expected_hash or build_token_hash(authorization_code) != expected_hash:
        raise ValueError("authorization_code_invalid")
    expires_at = _parse_datetime(config.get("authorization_code_expires_at"))
    if expires_at and now_shanghai() > expires_at:
        raise ValueError("authorization_code_expired")

    client_token = generate_secret_token("wwclient")
    registered_at = now_shanghai().isoformat()
    config.update(
        {
            "workwechat_delivery_mode": "cloud_poll",
            "client_token_hash": build_token_hash(client_token),
            "authorization_code_used_at": registered_at,
            "provider_name": str(provider_name or "").strip(),
            "client_version": str(client_version or "").strip(),
            "registered_at": registered_at,
            "last_poll_at": None,
        }
    )
    save_adapter_config(channel, config)
    return {
        "channel_id": int(getattr(channel, "id", 0) or 0),
        "client_token": client_token,
        "poll_interval_seconds": int(config.get("poll_interval_seconds") or DEFAULT_POLL_INTERVAL_SECONDS),
        "poll_batch_size": int(config.get("poll_batch_size") or DEFAULT_POLL_BATCH_SIZE),
    }


def verify_client_token(channel: Channel, client_token: str) -> bool:
    config = load_adapter_config(channel)
    expected_hash = str(config.get("client_token_hash") or "").strip()
    return bool(expected_hash) and build_token_hash(client_token) == expected_hash


def find_channel_by_authorization_code(db: Session, authorization_code: str) -> Channel | None:
    # 授权码本身不带 channel_id，注册时需要在 WorkWechat 渠道中按哈希查找匹配项。
    code_hash = build_token_hash(authorization_code)
    for channel in db.scalars(select(Channel).where(Channel.channel_type == "workwechat")).all():
        config = load_adapter_config(channel)
        if str(config.get("authorization_code_hash") or "").strip() == code_hash:
            return channel
    return None


def find_channel_by_client_token(db: Session, client_token: str) -> Channel | None:
    # client_token 只在请求时出现明文，数据库中仍只保存哈希。
    token_hash = build_token_hash(client_token)
    for channel in db.scalars(select(Channel).where(Channel.channel_type == "workwechat")).all():
        config = load_adapter_config(channel)
        if str(config.get("client_token_hash") or "").strip() == token_hash:
            return channel
    return None


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed


def _serialize_cloud_job(job: TaskJob, payload: dict[str, Any], conversation: Conversation | None = None) -> dict[str, Any]:
    if str(job.job_type or "") == WORKWECHAT_CLOUD_CONTROL_JOB:
        # 控制任务用于让云端唤醒本地 provider 执行非发消息动作，例如同步群列表；这类任务没有 conversation。
        return {
            "job_id": job.id,
            "job_type": job.job_type,
            "channel_id": int(payload.get("channel_id") or 0),
            "action": str(payload.get("action") or "").strip(),
            "payload": payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
            "payload_hash": _payload_hash(payload),
        }
    if conversation is None:
        raise ValueError("conversation_required")
    outbound_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    return {
        "job_id": job.id,
        "job_type": job.job_type,
        "channel_id": int(payload.get("channel_id") or 0),
        "conversation_id": conversation.id,
        "conversation_external_id": conversation.external_id,
        "conversation_title": conversation.title or conversation.external_id,
        "chat_type": conversation.chat_type,
        "payload": outbound_payload,
        "payload_hash": _payload_hash(payload),
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _load_owned_cloud_job(db: Session, channel: Channel, job_id: int) -> TaskJob:
    job = db.get(TaskJob, int(job_id))
    if not job or str(job.job_type or "") not in {WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB}:
        raise ValueError("job_not_found")
    payload = parse_job_payload(job)
    if int(payload.get("channel_id") or 0) != int(channel.id or 0):
        raise ValueError("job_not_found")
    return job


def _parse_json_text(value: str | None) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _complete_cloud_control_job(db: Session, channel: Channel, job: TaskJob, result_payload: dict[str, Any]) -> dict[str, Any]:
    payload = parse_job_payload(job)
    action = str(payload.get("action") or "").strip()
    adapter_result = result_payload.get("adapter_result") if isinstance(result_payload.get("adapter_result"), dict) else {}
    if action == "sync_chatrooms":
        from app.services.workwechat_chatrooms import apply_workwechat_provider_chatroom_list, apply_workwechat_provider_chatroom_profile

        target_chatroom_id = _coerce_optional_int(payload.get("target_chatroom_id"))
        target_external_id = str(payload.get("target_external_id") or "").strip()
        if target_chatroom_id:
            chatroom = db.get(Chatroom, target_chatroom_id)
            if not chatroom or int(chatroom.channel_id or 0) != int(channel.id or 0):
                raise ValueError("chatroom_not_found")
            # 自动补资料和详情页单群同步虽然请求的是全量列表，但服务端只更新目标群，避免误创建其它群。
            applied = apply_workwechat_provider_chatroom_profile(db, channel, chatroom, adapter_result)
            created = 0
            updated = 1 if applied["updated"] else 0
            total = len(applied["rooms"])
        else:
            # provider 回传的是 /chatrooms/list 同构结果，服务端在完成任务时立即落库，页面刷新即可看到群样本。
            applied = apply_workwechat_provider_chatroom_list(db, channel, adapter_result)
            created = applied["created"]
            updated = applied["updated"]
            total = len(applied["rooms"])
        complete_job(
            db,
            job,
            result={
                "action": action,
                "adapter_result": adapter_result,
                "created": created,
                "updated": updated,
                "total": total,
                "target_chatroom_id": target_chatroom_id,
                "target_external_id": target_external_id,
            },
        )
        return {
            "ok": True,
            "job_id": job.id,
            "status": "completed",
            "action": action,
            "created": created,
            "updated": updated,
            "total": total,
            "target_chatroom_id": target_chatroom_id,
            "target_external_id": target_external_id,
        }
    if action == "sync_chatroom_members":
        from app.services.workwechat_chatrooms import apply_workwechat_provider_chatroom_members

        chatroom_id = int(payload.get("chatroom_id") or 0)
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or int(chatroom.channel_id or 0) != int(channel.id or 0):
            raise ValueError("chatroom_not_found")
        # 群成员结果来自客服电脑本机 Hook 11040，服务端只负责按既有成员快照口径落库。
        applied = apply_workwechat_provider_chatroom_members(db, channel, chatroom, adapter_result)
        complete_job(
            db,
            job,
            result={
                "action": action,
                "chatroom_id": chatroom.id,
                "adapter_result": adapter_result,
                "synced_members": applied["synced_members"],
            },
        )
        return {
            "ok": True,
            "job_id": job.id,
            "status": "completed",
            "action": action,
            "chatroom_id": chatroom.id,
            "synced_members": applied["synced_members"],
        }
    if action == "sync_contacts":
        from app.services.workwechat_contacts import apply_workwechat_provider_contact_list

        # 联系人列表同样由本地 provider 调 Hook 11036/11037 后回传，避免云端服务器要求配置内网 API Base。
        applied = apply_workwechat_provider_contact_list(db, channel, adapter_result)
        complete_job(
            db,
            job,
            result={
                "action": action,
                "adapter_result": adapter_result,
                "synced_contacts": applied["synced_contacts"],
            },
        )
        return {
            "ok": True,
            "job_id": job.id,
            "status": "completed",
            "action": action,
            "synced_contacts": applied["synced_contacts"],
        }
    if action == "download_inbound_media":
        # 企微云端客户端模式下，服务端不能直连客服电脑 provider；附件补偿下载通过控制任务在本地执行，
        # 完成后把 provider 上传回主服务得到的 storage_path 写回附件，并补发 file.received 给业务系统。
        attachment_id = int(payload.get("attachment_id") or 0)
        attachment = db.get(MessageAttachment, attachment_id)
        if not attachment or int(attachment.channel_id or 0) != int(channel.id or 0):
            raise ValueError("attachment_not_found")
        storage_path = str(
            adapter_result.get("download_path")
            or adapter_result.get("storage_path")
            or adapter_result.get("file_path")
            or ""
        ).strip()
        if not storage_path:
            raise ValueError("attachment_download_missing_storage_path")
        attachment.status = "downloaded"
        attachment.storage_path = storage_path
        attachment.file_size = _coerce_optional_int(adapter_result.get("file_size")) or attachment.file_size
        attachment.mime_type = str(adapter_result.get("mime_type") or adapter_result.get("content_type") or "application/octet-stream").strip()
        attachment.last_error = None
        metadata = _parse_json_text(attachment.metadata_json)
        metadata["workwechat_cloud_compensation"] = {
            "downloaded_at": now_shanghai().isoformat(),
            "job_id": job.id,
            "download_url": adapter_result.get("download_url"),
            "source_path": adapter_result.get("source_path"),
        }
        attachment.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)

        message = db.get(Message, int(attachment.message_id or 0)) if attachment.message_id else None
        conversation = db.get(Conversation, int(attachment.conversation_id or 0)) if attachment.conversation_id else None
        complete_job(db, job, result={"action": action, "attachment_id": attachment.id, "adapter_result": adapter_result})
        if message is not None and conversation is not None:
            dispatch_open_message_event(
                db,
                event_type="file.received",
                channel=channel,
                conversation=conversation,
                message=message,
                attachments=[attachment],
            )
            db.commit()
        return {"ok": True, "job_id": job.id, "status": "completed", "action": action, "attachment_id": attachment.id}
    complete_job(db, job, result={"action": action, "adapter_result": adapter_result})
    return {"ok": True, "job_id": job.id, "status": "completed", "action": action}


def _create_cloud_outbound_message(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    outbound_payload: dict[str, Any],
    adapter_result: dict[str, Any],
) -> Message:
    message_type = str(outbound_payload.get("message_type") or "text").strip().lower() or "text"
    content = str(outbound_payload.get("content") or "").strip() or f"[{message_type}]"
    message = Message(
        conversation_id=conversation.id,
        direction="out",
        sender_id=f"outbound-cloud:{channel.channel_type}:{channel.id}",
        sender_name=f"{channel.channel_type} · {channel.name} · #{channel.id}",
        content=content,
        message_type=message_type,
        status="sent" if adapter_result.get("ok") else "failed",
        external_status=str(adapter_result.get("external_status") or adapter_result.get("status") or ""),
        external_message_id=str(adapter_result.get("external_message_id") or "") or None,
        raw_response_json=json.dumps(adapter_result, ensure_ascii=False),
    )
    db.add(message)
    db.flush()
    return message
