"""WeBridge 长轮询服务端的设备状态、命令队列和结果处理。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, TaskJob
from app.services.task_queue import complete_job, enqueue_job, fail_job_terminal, parse_job_payload

from .contract import JOB_TYPE, as_object, identity_from_payload, text


def _runtime(channel: Channel) -> dict[str, Any]:
    """读取渠道运行信息；损坏 JSON 必须按空对象处理，不能阻断后台页面。"""

    try:
        value = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save_runtime(channel: Channel, value: dict[str, Any]) -> None:
    """写回运行信息，并保留其他运行命名空间。"""

    channel.runtime_info_json = json.dumps(value, ensure_ascii=False, sort_keys=True)


def _identity_matches(channel: Channel, payload: dict[str, Any]) -> bool:
    """校验请求身份与最近注册设备一致，禁止跨设备领取同一渠道任务。"""

    incoming = identity_from_payload(payload)
    current = _runtime(channel).get("webridge")
    if not isinstance(current, dict):
        return False
    return bool(
        incoming["device_id"]
        and incoming["session_id"]
        and incoming["device_id"] == current.get("device_id")
        and incoming["account_id"] == current.get("account_id")
    )


def register_device(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """保存手机注册快照；register 是后续 state/event/poll 的身份前置条件。"""

    identity = identity_from_payload(payload)
    if not identity["device_id"] or not identity["session_id"] or not identity["account_id"]:
        raise ValueError("invalid_webridge_identity")
    runtime = _runtime(channel)
    runtime["webridge"] = {
        **identity,
        "capabilities": [text(item) for item in payload.get("capabilities", []) if text(item)],
        "state": as_object(payload.get("state")),
        "registered_at": now_shanghai().isoformat(),
        "last_seen_at": now_shanghai().isoformat(),
        "online": True,
    }
    _save_runtime(channel, runtime)
    db.commit()
    return {"ok": True, "channel_id": int(channel.id), "device_id": identity["device_id"]}


def accept_state(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """更新状态心跳；状态失败不影响插件继续 poll，但必须保留最后一次快照。"""

    if not _identity_matches(channel, payload):
        raise ValueError("webridge_identity_mismatch")
    runtime = _runtime(channel)
    bridge = runtime.setdefault("webridge", {})
    bridge.update({"state": as_object(payload.get("state")), "last_seen_at": now_shanghai().isoformat(), "online": True})
    _save_runtime(channel, runtime)
    db.commit()
    return {"ok": True}


def enqueue_text_message(db: Session, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> TaskJob:
    """把统一文本消息转换为 WeBridge 命令任务，任务归属固定到渠道。"""

    content = str(payload.get("content") or payload.get("text") or "").strip()
    receiver = str(payload.get("receiver_id") or payload.get("external_id") or conversation.external_id or "").strip()
    if not content or not receiver:
        raise ValueError("webridge_text_message_requires_content_and_receiver")
    return enqueue_job(
        db,
        job_type=JOB_TYPE,
        payload={
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "command": {
                "request_id": f"job_{uuid.uuid4().hex}",
                "type": "send_message",
                "params": {"receiver": receiver, "content": content},
            },
        },
        priority=90,
        related_type="conversation",
        related_id=int(conversation.id),
    )



def enqueue_command(db: Session, channel: Channel, *, command_name: str, args: dict[str, Any], request_id: str = "") -> TaskJob:
    """把任意 WeBridge 命令放入当前渠道队列，供联系人、群组、媒体和高级能力复用。"""

    normalized_name = text(command_name)
    if not normalized_name or normalized_name.startswith("/") or len(normalized_name) > 100:
        raise ValueError("invalid_webridge_command_name")
    if not isinstance(args, dict):
        raise ValueError("webridge_command_args_must_be_object")
    normalized_request_id = text(request_id) or f"cmd_{uuid.uuid4().hex}"
    return enqueue_job(
        db,
        job_type=JOB_TYPE,
        payload={
            "channel_id": int(channel.id),
            "command": {"request_id": normalized_request_id, "type": normalized_name, "params": args},
        },
        priority=90,
        dedupe_key=f"webridge:{channel.id}:{normalized_request_id}",
    )


def get_command_result(db: Session, channel: Channel, request_id: str) -> dict[str, Any] | None:
    """读取指定命令的最终任务结果，未知或尚未回报时返回 None。"""

    normalized = text(request_id)
    if not normalized:
        return None
    jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.tenant_id == channel.tenant_id)).all()
    for job in jobs:
        payload = parse_job_payload(job)
        command = as_object(payload.get("command"))
        if text(command.get("request_id")) != normalized and normalized != f"job_{int(job.id)}":
            continue
        raw_result = {}
        try:
            raw_result = json.loads(job.result_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_result = {}
        return {"request_id": normalized, "job_id": int(job.id), "status": job.status, "result": raw_result, "error": job.error_message}
    return None

def claim_commands(db: Session, channel: Channel, payload: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """领取当前设备的待发送命令，并将任务置为 running 防止重复下发。"""

    if not _identity_matches(channel, payload):
        raise ValueError("webridge_identity_mismatch")
    current = now_shanghai()
    jobs = db.scalars(
        select(TaskJob)
        .where(TaskJob.job_type == JOB_TYPE, TaskJob.status == "pending", TaskJob.available_at <= current)
        .order_by(TaskJob.priority.desc(), TaskJob.id.asc())
        .limit(max(1, min(int(limit or 5), 20)) * 3)
    ).all()
    commands: list[dict[str, Any]] = []
    for job in jobs:
        data = parse_job_payload(job)
        if int(data.get("channel_id") or 0) != int(channel.id):
            continue
        job.status = "running"
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = current
        job.locked_at = current
        # 现有统一出站入口把内容放在 payload 字段；这里在协议边界转换成 WeBridge 命令。
        command = as_object(data.get("command"))
        if not command:
            outbound = as_object(data.get("payload"))
            message_type = text(outbound.get("message_type") or "text")
            if message_type != "text":
                job.status = "failed"
                job.error_message = "WeBridge 首期只支持文本消息"
                job.finished_at = current
                continue
            receiver = text(outbound.get("receiver_id") or outbound.get("external_id"))
            content = str(outbound.get("content") or outbound.get("text") or "").strip()
            command = {
                "request_id": f"job_{int(job.id)}",
                "type": "send_message",
                "params": {"receiver": receiver, "content": content},
            }
        else:
            command["request_id"] = text(command.get("request_id")) or f"job_{int(job.id)}"
        if command.get("params", {}).get("receiver") and command.get("params", {}).get("content"):
            commands.append(command)
        if len(commands) >= max(1, min(int(limit or 5), 20)):
            break
    db.commit()
    return commands


def accept_command_result(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """按 command request_id 完成任务；重复回报返回已处理状态。"""

    if not _identity_matches(channel, payload):
        raise ValueError("webridge_identity_mismatch")
    command = as_object(payload.get("command"))
    request_id = text(payload.get("request_id") or command.get("request_id"))
    if not request_id:
        raise ValueError("missing_webridge_request_id")
    job_id = 0
    if request_id.startswith("job_"):
        try:
            job_id = int(request_id[4:])
        except ValueError:
            job_id = 0
    jobs = [db.get(TaskJob, job_id)] if job_id > 0 else db.scalars(
        select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status.in_(("running", "completed", "failed")))
    ).all()
    for job in jobs:
        if job is None or str(job.job_type or "") != JOB_TYPE:
            continue
        data = parse_job_payload(job)
        stored_request_id = text(as_object(data.get("command")).get("request_id"))
        if stored_request_id and stored_request_id != request_id and int(job.id) != job_id:
            continue
        if job.status in {"completed", "failed"}:
            return {"ok": True, "deduplicated": True, "job_id": int(job.id), "status": job.status}
        result = as_object(payload.get("result"))
        if bool(payload.get("ok")) or bool(result.get("ok")):
            complete_job(db, job, result={"webridge": payload})
            return {"ok": True, "job_id": int(job.id), "status": "completed"}
        fail_job_terminal(db, job, error_message=text(result.get("error") or payload.get("error") or "webridge_command_failed"), result={"webridge": payload})
        return {"ok": True, "job_id": int(job.id), "status": "failed"}
    raise ValueError("webridge_request_not_found")


def mark_event_seen(channel: Channel, payload: dict[str, Any]) -> str:
    """提取事件 ID；没有 ID 时用稳定正文摘要，避免同一轮重试重复落库。"""

    event_id = text(payload.get("event_id") or payload.get("message_id") or payload.get("id"))
    if event_id:
        return event_id
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "hash_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
