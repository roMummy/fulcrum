"""只读取本企业目录同步任务的实际回报，供列表页展示进度。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, TaskJob
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge.contract import JOB_TYPE, as_object
from app.services.wechat_webridge.service import COMMAND_RUNNING_TIMEOUT_SECONDS


class DirectorySyncError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def parse_job_ids(value: str) -> list[int]:
    parts = str(value or "").split(",")
    if not parts or len(parts) > 200 or any(len(part) > 18 or not part.isascii() or not part.isdigit() or int(part) < 1 for part in parts):
        raise DirectorySyncError(400, "同步任务编号无效")
    return list(dict.fromkeys(int(part) for part in parts))


def _is_ok(value: Any) -> bool:
    return value is True or type(value) is int and value == 1


def _completed_count(job: TaskJob, kind: str) -> int | None:
    try:
        payload = as_object(as_object(json.loads(job.result_json or "{}")).get("webridge"))
    except (TypeError, ValueError):
        return None
    result = as_object(payload.get("result"))
    command = "friends_all" if kind == "contacts" else "groups_all"
    if (
        not (_is_ok(payload.get("ok")) or _is_ok(result.get("ok")))
        or payload.get("error") or result.get("error")
        or "ok" in payload and not _is_ok(payload["ok"])
        or "ok" in result and not _is_ok(result["ok"])
        or payload.get("command_name") not in (None, "", command)
    ):
        return None
    count_key = "contacts" if kind == "contacts" else "groups"
    counts = as_object(result.get("persisted_counts"))
    keys = ("items", "friends", "contacts") if kind == "contacts" else ("groups", "items", "rooms")
    recognized = result.get("directory_result_keys")
    if (result.get("truncated") is True and isinstance(recognized, list) and any(key in recognized for key in keys)
            and type(counts.get(count_key)) is int and counts[count_key] >= 0):
        return counts[count_key]
    id_keys = ("account_id", "user_name", "wxid", "username", "id") if kind == "contacts" else ("account_id", "room_id", "chatroom_id", "user_name", "wxid", "id")
    for key in keys:
        if isinstance(result.get(key), list):
            # 和资料落库一致，只统计有稳定账号编号的条目；空数组也代表一次有效的空目录同步。
            count = sum(isinstance(item, dict) and any(str(item.get(name) or "").strip() for name in id_keys) for item in result[key])
            return count if count or not result[key] else None
    return None


def sync_status(db: Session, job_ids: list[int], kind: str) -> dict[str, Any]:
    if kind not in {"contacts", "chatrooms"}:
        raise DirectorySyncError(400, "同步类型无效")
    if not job_ids or len(job_ids) > 200 or any(type(value) is not int or value < 1 for value in job_ids):
        raise DirectorySyncError(400, "同步任务编号无效")
    job_ids = list(dict.fromkeys(job_ids))
    tenant_id = require_tenant_id()
    jobs = {job.id: job for job in db.scalars(select(TaskJob).where(
        TaskJob.id.in_(job_ids), TaskJob.tenant_id == tenant_id, TaskJob.job_type == JOB_TYPE,
    )).all()}
    command_name = "friends_all" if kind == "contacts" else "groups_all"
    channels = {channel.id: channel for channel in db.scalars(select(Channel).where(
        Channel.tenant_id == tenant_id, Channel.channel_type == "wechat_webridge", Channel.deleted_at.is_(None),
    )).all()}
    if len(jobs) != len(job_ids):
        raise DirectorySyncError(404, "未找到本企业的同步任务")
    now = now_shanghai()
    rows = []
    label, unit = ("好友", "位") if kind == "contacts" else ("群聊", "个")
    for job_id in job_ids:
        job = jobs[job_id]
        payload = parse_job_payload(job)
        channel_id = payload.get("channel_id")
        command = as_object(payload.get("command"))
        if type(channel_id) is not int or channel_id not in channels or command.get("type") != command_name:
            raise DirectorySyncError(404, "未找到本企业的同步任务")
        elapsed = max(0, int((now - ensure_shanghai_datetime(job.created_at)).total_seconds()))
        state, count = str(job.status or ""), None
        if state in {"pending", "running"}:
            anchor = job.created_at if state == "pending" else job.locked_at or job.started_at or job.created_at
            if (now - ensure_shanghai_datetime(anchor)).total_seconds() > COMMAND_RUNNING_TIMEOUT_SECONDS:
                # 监控超时仅结束本页等待，不改写任务，避免覆盖手机刚提交的回报。
                state, message = "timeout", "等待手机响应超时，可稍后重新同步"
            else:
                message = "等待手机领取" if state == "pending" else "手机正在同步"
        elif state == "completed":
            count = _completed_count(job, kind)
            if count is None:
                state, message = "failed", "插件未返回可识别的同步结果"
            else:
                message = f"{label}同步完成，{count} {unit}"
        else:
            state = "failed"
            error = str(job.error_message or "")
            message = "手机执行超时，可重新同步" if error == "webridge_command_timeout" else (f"同步失败：{error[:160]}" if error else "同步失败，请重新尝试")
        if job.finished_at is not None:
            elapsed = max(0, int((ensure_shanghai_datetime(job.finished_at) - ensure_shanghai_datetime(job.created_at)).total_seconds()))
        rows.append({"id": int(job.id), "channel_id": channel_id, "channel_name": channels[channel_id].name or f"账号 #{channel_id}",
                     "state": state, "count": count, "message": message, "elapsed_seconds": elapsed})
    successful = sum(row["state"] == "completed" for row in rows)
    failed = sum(row["state"] in {"failed", "timeout"} for row in rows)
    done = successful + failed == len(rows)
    count = sum(row["count"] or 0 for row in rows)
    if not done:
        summary = f"{label}同步中，已完成 {successful + failed} / {len(rows)} 个账号"
    elif failed == 0:
        summary = f"{label}同步完成，{len(rows)} 个账号共 {count} {unit}"
    elif successful:
        summary = f"{label}同步结束，{successful} 个账号成功、{failed} 个未完成，共 {count} {unit}"
    else:
        summary = f"{label}同步未完成，请查看账号状态后重试"
    return {"ok": True, "jobs": rows, "done": done, "successful": successful, "failed": failed, "count": count, "summary": summary}
