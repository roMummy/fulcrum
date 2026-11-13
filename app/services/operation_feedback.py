"""只读渠道操作的实际任务回报，供页面自动显示进度。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, TaskJob
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, parse_job_payload
from app.services.wechat_webridge.contract import JOB_TYPE, as_object
from app.services.wechat_webridge.service import COMMAND_RUNNING_TIMEOUT_SECONDS


class OperationFeedbackError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


_LABELS = {
    "friends": "好友同步", "friends_all": "好友同步", "sync_contacts": "好友同步",
    "groups_all": "群组同步", "sync_chatrooms": "群组同步",
    "room_members": "群成员同步", "sync_chatroom_members": "群成员同步",
    "account_status": "账号状态检查", "bridge_status": "连接状态检查", "device_status": "设备状态检查",
    "get_group_notice": "群公告查询", "get_chatroom_qrcode": "群二维码查询", "get_self_qrcode": "账号二维码查询",
    "search_contact": "联系人查询", "official_accounts": "公众号同步",
    "create_group": "创建群聊", "rename_group": "修改群名称", "set_group_name": "修改群名称",
    "set_group_self_nickname": "修改群昵称", "quit_group": "退出群聊",
    "set_friend_remark": "修改好友备注", "accept_friend_request": "好友请求处理",
}
_DIRECTORIES = {
    "friends": ("contacts", ("items", "friends", "contacts"), ("account_id", "user_name", "wxid", "username", "id"), "位好友"),
    "friends_all": ("contacts", ("items", "friends", "contacts"), ("account_id", "user_name", "wxid", "username", "id"), "位好友"),
    "groups_all": ("groups", ("groups", "items", "rooms"), ("account_id", "room_id", "chatroom_id", "user_name", "wxid", "id"), "个群聊"),
    "room_members": ("members", ("members", "items"), ("account_id", "user_name", "wxid", "id"), "位成员"),
}
_CLOUD_COUNTS = {
    "sync_contacts": ("synced_contacts", "位好友"),
    "sync_chatrooms": ("total", "个群聊"),
    "sync_chatroom_members": ("synced_members", "位成员"),
}


def _is_ok(value: Any) -> bool:
    # 不使用 bool(value)，否则字符串 "false" 也会被当成成功。
    return value is True or type(value) is int and value == 1


def _has_failure(result: dict[str, Any]) -> bool:
    return bool(
        "ok" in result and not _is_ok(result["ok"])
        or result.get("error")
        or result.get("status") in ("failed", "error", "cancelled")
    )


def _directory_count(result: dict[str, Any], command: str) -> tuple[int, str] | None:
    count_key, keys, id_keys, unit = _DIRECTORIES[command]
    counts = as_object(result.get("persisted_counts"))
    recognized = result.get("directory_result_keys")
    if (
        result.get("truncated") is True
        and (recognized is None or isinstance(recognized, list) and any(key in recognized for key in keys))
        and type(counts.get(count_key)) is int and counts[count_key] >= 0
    ):
        # 旧版压缩结果只有落库计数，新版额外保留目录字段名，两种都可读取。
        return counts[count_key], unit
    for key in keys:
        items = result.get(key)
        if isinstance(items, list):
            count = sum(isinstance(item, dict) and any(str(item.get(name) or "").strip() for name in id_keys) for item in items)
            return (count, unit) if count or not items else None
    return None


def _completed_message(job: TaskJob, action: str, label: str) -> str | None:
    try:
        result = json.loads(job.result_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(result, dict) or _has_failure(result):
        return None
    if job.job_type == JOB_TYPE:
        envelope = result.get("webridge")
        if not isinstance(envelope, dict) or _has_failure(envelope):
            return None
        detail = envelope.get("result")
        if not isinstance(detail, dict) or _has_failure(detail):
            return None
        if not (_is_ok(envelope.get("ok")) or _is_ok(detail.get("ok"))):
            return None
        if envelope.get("command_name") not in (None, "", action):
            return None
        if action in _DIRECTORIES:
            count = _directory_count(detail, action)
            return f"{label}完成，{count[0]} {count[1]}" if count is not None else None
        return f"{label}完成"
    adapter_result = result.get("adapter_result")
    if result.get("action") != action or not isinstance(adapter_result, dict) or _has_failure(adapter_result):
        return None
    detail = adapter_result.get("result")
    if isinstance(detail, dict) and _has_failure(detail):
        return None
    if action in _CLOUD_COUNTS:
        key, unit = _CLOUD_COUNTS[action]
        count = result.get(key)
        return f"{label}完成，{count} {unit}" if type(count) is int and count >= 0 else None
    if _is_ok(adapter_result.get("ok")) or isinstance(detail, dict) and _is_ok(detail.get("ok")):
        return f"{label}完成"
    return None


def build_operation_status(db: Session, channel: Channel, job_ids: list[int]) -> dict[str, Any]:
    if not isinstance(job_ids, list) or not job_ids or len(job_ids) > 200 or any(type(value) is not int or value < 1 for value in job_ids):
        raise OperationFeedbackError(400, "操作任务编号无效")
    job_ids = list(dict.fromkeys(job_ids))
    tenant_id = require_tenant_id()
    # 显式校验租户和渠道，裸 Session 也不能用任务编号越权读取其他企业的回报。
    with db.no_autoflush:
        if channel.tenant_id != tenant_id or channel.deleted_at is not None:
            raise OperationFeedbackError(404, "未找到本账号的操作任务")
        expected_type = {"wechat_webridge": JOB_TYPE, "workwechat": WORKWECHAT_CLOUD_CONTROL_JOB}.get(channel.channel_type)
        if expected_type is None:
            raise OperationFeedbackError(400, "此账号不支持任务进度查询")
        jobs = {job.id: job for job in db.scalars(select(TaskJob).where(
            TaskJob.id.in_(job_ids), TaskJob.tenant_id == tenant_id, TaskJob.job_type == expected_type,
        )).all()}
        if len(jobs) != len(job_ids):
            raise OperationFeedbackError(404, "未找到本账号的操作任务")
        payloads = {job_id: parse_job_payload(jobs[job_id]) for job_id in job_ids}
        for payload in payloads.values():
            if type(payload.get("channel_id")) is not int or payload["channel_id"] != channel.id:
                raise OperationFeedbackError(404, "未找到本账号的操作任务")

    now = now_shanghai()
    rows = []
    for job_id in job_ids:
        job, payload = jobs[job_id], payloads[job_id]
        action = as_object(payload.get("command")).get("type") if job.job_type == JOB_TYPE else payload.get("action")
        action = action if isinstance(action, str) else ""
        if not action:
            raise OperationFeedbackError(404, "未找到本账号的操作任务")
        label = _LABELS.get(action, "操作")
        if action == "sync_chatrooms" and payload.get("target_chatroom_id"):
            label = "群资料同步"
        created = ensure_shanghai_datetime(job.created_at)
        state = str(job.status or "")
        end = ensure_shanghai_datetime(job.finished_at) if state in {"completed", "failed", "cancelled"} and job.finished_at else now
        elapsed = max(0, int((end - created).total_seconds()))
        if state in {"pending", "running"}:
            if job.job_type == JOB_TYPE:
                anchor = job.created_at if state == "pending" else job.locked_at or job.started_at or job.created_at
                timed_out = (now - ensure_shanghai_datetime(anchor)).total_seconds() > COMMAND_RUNNING_TIMEOUT_SECONDS
            else:
                timed_out = elapsed > 300 or bool(job.lease_expires_at and now > ensure_shanghai_datetime(job.lease_expires_at))
            if timed_out:
                # 只结束页面等待，不修改后台任务，避免覆盖刚到达的手机或客户端回报。
                state, message = "timeout", f"{label}等待响应超时，请检查设备连接后重试"
            elif state == "pending":
                message = f"{label}等待重试" if job.error_message else f"{label}等待设备领取"
            else:
                message = f"设备正在执行{label}"
        elif state == "completed":
            message = _completed_message(job, action, label)
            if message is None:
                state, message = "failed", f"{label}未完成，设备未返回有效结果"
        else:
            state = "failed"
            message = f"{label}已取消" if job.status == "cancelled" else f"{label}失败，请检查设备连接后重试"
        rows.append({"id": int(job.id), "channel_name": channel.name or "当前账号", "state": state,
                     "message": message, "elapsed_seconds": elapsed})

    successful = sum(row["state"] == "completed" for row in rows)
    failed = sum(row["state"] in {"failed", "timeout"} for row in rows)
    done = successful + failed == len(rows)
    if len(rows) == 1:
        summary = rows[0]["message"]
    elif not done:
        summary = f"正在执行，已完成 {successful + failed} / {len(rows)} 项"
    elif not failed:
        summary = f"操作完成，{successful} 项成功"
    else:
        summary = f"操作结束，{successful} 项成功、{failed} 项未完成"
    return {"ok": True, "done": done, "successful": successful, "failed": failed, "jobs": rows, "summary": summary}
