"""好友检测只读取本次强制刷新结果，不使用好友资料里的支付姓名缓存。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, TaskJob, WorkWechatContact
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service, wakeup
from app.services.wechat_webridge.contract import JOB_TYPE, as_object


COMMAND = "friend_pay_name"
RELATED_TYPE = "webridge_friend_check"
TIMEOUT_SECONDS = 60
ACTIVE_STATUSES = {"pending", "running"}


class FriendCheckError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _validate_channel(channel: Channel) -> None:
    if (
        channel.tenant_id != require_tenant_id()
        or channel.channel_type != "wechat_webridge"
        or channel.deleted_at is not None
    ):
        raise FriendCheckError(404, "未找到可检测的微桥账号")


def _contact_row(contact: WorkWechatContact) -> dict[str, Any]:
    return {
        "id": int(contact.id),
        "external_id": contact.contact_external_id,
        "name": contact.name or contact.display_name or contact.contact_external_id,
        "remark": (contact.display_name or "") if contact.name and contact.display_name != contact.name else "",
        "avatar_url": contact.avatar_url or "",
    }


def list_check_channels(db: Session) -> list[dict[str, Any]]:
    tenant_id = require_tenant_id()
    counts = dict(db.execute(
        select(WorkWechatContact.channel_id, func.count(WorkWechatContact.id))
        .where(
            WorkWechatContact.tenant_id == tenant_id,
            WorkWechatContact.contact_kind == "friend",
            WorkWechatContact.deleted_at.is_(None),
        )
        .group_by(WorkWechatContact.channel_id)
    ).all())
    channels = db.scalars(select(Channel).where(
        Channel.tenant_id == tenant_id,
        Channel.channel_type == "wechat_webridge",
        Channel.deleted_at.is_(None),
    ).order_by(Channel.name, Channel.id)).all()
    return [{"id": int(channel.id), "name": channel.name or f"账号 #{channel.id}", "total": int(counts.get(channel.id, 0))} for channel in channels]


def list_check_contacts(db: Session, channel: Channel) -> list[dict[str, Any]]:
    _validate_channel(channel)
    contacts = db.scalars(select(WorkWechatContact).where(
        WorkWechatContact.tenant_id == channel.tenant_id,
        WorkWechatContact.channel_id == channel.id,
        WorkWechatContact.contact_kind == "friend",
        WorkWechatContact.deleted_at.is_(None),
    ).order_by(WorkWechatContact.display_name, WorkWechatContact.id)).all()
    return [_contact_row(contact) for contact in contacts]


def _expire_if_stale(db: Session, job: TaskJob) -> bool:
    if job.status not in ACTIVE_STATUSES:
        return False
    started = job.created_at if job.status == "pending" else job.locked_at or job.started_at or job.created_at
    cutoff = now_shanghai() - timedelta(seconds=TIMEOUT_SECONDS)
    if ensure_shanghai_datetime(started) > cutoff:
        return False
    anchor = TaskJob.created_at if job.status == "pending" else func.coalesce(TaskJob.locked_at, TaskJob.started_at, TaskJob.created_at)
    # 用状态和时间一起限制更新，避免检测超时覆盖手机刚刚完成的结果。
    error_message = "检测超时，请确认手机在线后重试"
    if job.related_type == "webridge_friend_action":
        error_message = "操作超时，命令未执行" if job.status == "pending" else "操作超时，结果未确认，请先核对微信"
    changed = db.execute(update(TaskJob).where(
        TaskJob.id == job.id,
        TaskJob.tenant_id == job.tenant_id,
        TaskJob.status == job.status,
        anchor <= cutoff,
    ).values(
        status="failed", finished_at=now_shanghai(),
        error_message=error_message, result_json="{}",
    ), execution_options={"synchronize_session": False}).rowcount
    db.refresh(job)
    return bool(changed)


def start_check(db: Session, channel: Channel, contact_id: int) -> TaskJob:
    _validate_channel(channel)
    if channel.is_enabled is False:
        raise FriendCheckError(400, "账号尚未启用，请先完成接入并启用账号")
    contact = db.scalar(select(WorkWechatContact).where(
        WorkWechatContact.id == contact_id,
        WorkWechatContact.tenant_id == channel.tenant_id,
        WorkWechatContact.channel_id == channel.id,
        WorkWechatContact.contact_kind == "friend",
        WorkWechatContact.deleted_at.is_(None),
    ))
    if contact is None or not contact.contact_external_id.strip():
        raise FriendCheckError(404, "未找到该账号下的好友")
    active_key = f"friend-check-active:{channel.id}"
    try:
        previous = db.scalar(select(TaskJob).where(
            TaskJob.tenant_id == channel.tenant_id,
            TaskJob.dedupe_key == active_key,
        ))
        if previous is not None:
            _expire_if_stale(db, previous)
            if previous.status in ACTIVE_STATUSES:
                raise FriendCheckError(409, "该账号正在检测另一位好友，请等待完成后继续")
            previous.dedupe_key = None
            db.flush()
        job = service.enqueue_command(db, channel, command_name=COMMAND, args={"id": contact.contact_external_id, "refresh": True})
        job.related_type = RELATED_TYPE
        job.related_id = int(contact.id)
        # 复用已有唯一约束，让不同页面或并发请求也只能占用一个检测位置。
        job.dedupe_key = active_key
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise FriendCheckError(409, "该账号已有检测在进行，请稍后重试") from exc
    wakeup.notify(int(channel.id))
    return job


def _is_ok(value: Any) -> bool:
    return value is True or type(value) is int and value == 1


def _classify_result(job: TaskJob, target: str) -> dict[str, str]:
    outcome = {"category": "unknown", "pay_name_last_char": "", "reason": "插件没有返回可识别的检测结果"}
    if job.status != "completed":
        outcome["reason"] = (job.error_message or "检测未成功完成，请稍后重试")[:200]
        return outcome
    try:
        payload = as_object(as_object(json.loads(job.result_json or "{}")).get("webridge"))
    except (TypeError, ValueError):
        return outcome
    if payload.get("command_name") not in (None, "", COMMAND):
        outcome["reason"] = "插件返回的命令与本次检测不匹配"
        return outcome
    business = as_object(payload.get("result"))
    if not _is_ok(payload.get("ok")) or payload.get("error"):
        outcome["reason"] = str(payload.get("error") or "插件报告检测失败")[:200]
        return outcome
    # 旧插件把 HTTP 响应额外包在 result 中，只兼容这一层既有结构。
    if isinstance(business.get("result"), dict):
        if not _is_ok(business.get("ok")) or business.get("error"):
            outcome["reason"] = str(business.get("error") or "插件报告检测失败")[:200]
            return outcome
        business = business["result"]
    if business.get("account_id") != target:
        outcome["reason"] = "插件返回的目标好友不匹配"
        return outcome
    status = business.get("pay_name_status")
    last_char = business.get("pay_name_last_char", "")
    if not isinstance(last_char, str):
        return outcome
    last_char = last_char.strip()
    if status == "ok" and len(last_char) == 1:
        return {"category": "friend", "pay_name_last_char": last_char, "reason": "已取得姓名末字"}
    if status == "unavailable" and not last_char:
        return {"category": "deleted", "pay_name_last_char": "", "reason": "未取得姓名末字"}
    return outcome


def poll_check(db: Session, channel: Channel, job_id: int) -> dict[str, Any]:
    _validate_channel(channel)
    job = db.scalar(select(TaskJob).where(
        TaskJob.id == job_id,
        TaskJob.tenant_id == channel.tenant_id,
        TaskJob.job_type == JOB_TYPE,
        TaskJob.related_type == RELATED_TYPE,
    ))
    if job is None:
        raise FriendCheckError(404, "未找到该好友检测任务")
    payload = parse_job_payload(job)
    command = as_object(payload.get("command"))
    args = as_object(command.get("params"))
    contact = db.scalar(select(WorkWechatContact).where(
        WorkWechatContact.id == job.related_id,
        WorkWechatContact.tenant_id == channel.tenant_id,
        WorkWechatContact.channel_id == channel.id,
        WorkWechatContact.contact_kind == "friend",
    ))
    if (
        payload.get("channel_id") != channel.id or command.get("type") != COMMAND
        or args.get("refresh") is not True or contact is None
        or args.get("id") != contact.contact_external_id
    ):
        raise FriendCheckError(404, "未找到该账号对应的好友检测任务")
    if _expire_if_stale(db, job):
        db.commit()
    if job.status in ACTIVE_STATUSES:
        return {"state": job.status, "result": None}
    return {"state": "completed", "result": {**_classify_result(job, contact.contact_external_id), "contact": _contact_row(contact)}}
