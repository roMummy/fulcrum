"""好友检测页的备注与删除操作，仅按本次插件回报更新本地资料。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob, WorkWechatContact
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import friend_check, service, wakeup
from app.services.wechat_webridge.contract import JOB_TYPE, as_object


RELATED_TYPE = "webridge_friend_action"
COMMANDS = {"remark": "set_friend_remark", "delete": "delete_friend"}
FriendCheckError = friend_check.FriendCheckError


def start_action(
    db: Session,
    channel: Channel,
    contact_id: int,
    action: str,
    suffix: str = "- 单删",
    *,
    commit: bool = True,
) -> TaskJob:
    friend_check._validate_channel(channel)
    if channel.is_enabled is False:
        raise FriendCheckError(400, "账号尚未启用，请先完成接入并启用账号")
    if action not in COMMANDS:
        raise FriendCheckError(400, "不支持的好友操作")
    contact = db.scalar(select(WorkWechatContact).where(
        WorkWechatContact.id == contact_id,
        WorkWechatContact.tenant_id == channel.tenant_id,
        WorkWechatContact.channel_id == channel.id,
        WorkWechatContact.contact_kind == "friend",
        WorkWechatContact.deleted_at.is_(None),
    ))
    if contact is None or not contact.contact_external_id.strip():
        raise FriendCheckError(404, "未找到该账号下的好友")
    args = {"id": contact.contact_external_id}
    if action == "remark":
        if not isinstance(suffix, str) or not suffix.strip():
            raise FriendCheckError(400, "请输入备注后缀")
        if any(ord(character) < 32 for character in suffix):
            raise FriendCheckError(400, "备注后缀不能包含换行或控制字符")
        base = contact.display_name or contact.name or contact.contact_external_id
        remark = base + suffix
        # 只保护资料库字段长度；微信原生 charLength/formatter 规则由插件最终校验，不截断姓名。
        if len(remark) > 255:
            raise FriendCheckError(400, "备注加上后缀后过长，请缩短后缀")
        args["remark"] = remark
    active_key = f"friend-check-active:{channel.id}"
    try:
        previous = db.scalar(select(TaskJob).where(
            TaskJob.tenant_id == channel.tenant_id, TaskJob.dedupe_key == active_key,
        ))
        if previous is not None:
            friend_check._expire_if_stale(db, previous)
            if previous.status in friend_check.ACTIVE_STATUSES:
                raise FriendCheckError(409, "该账号正在检查或操作好友，请等待完成后继续")
            previous.dedupe_key = None
            db.flush()
        job = service.enqueue_command(db, channel, command_name=COMMANDS[action], args=args)
        friend_check._use_job_request_id(job)
        job.related_type = RELATED_TYPE
        job.related_id = int(contact.id)
        # 检测、复查和写操作共用同一占位，防止不同页面同时操作同一账号。
        job.dedupe_key = active_key
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise FriendCheckError(409, "该账号已有好友操作在进行，请稍后重试") from exc
    if commit:
        wakeup.notify(int(channel.id))
    return job


def _operation_error(action: str, value: Any, fallback: str = "插件未确认操作成功") -> str:
    message = str(value or fallback)[:200]
    if action == "delete" and "service class not found" in message.lower():
        return "手机端微桥无法找到微信的删除服务（service class not found），删除未获成功确认。"
    return message


def _outcome(job: TaskJob, action: str, target: str) -> tuple[bool, str, str]:
    if job.status != "completed":
        return False, _operation_error(action, job.error_message, "操作未成功完成，请核对微信后再操作"), ""
    try:
        payload = as_object(as_object(json.loads(job.result_json or "{}")).get("webridge"))
    except (TypeError, ValueError):
        return False, "插件返回无法识别，请核对微信后再操作", ""
    if payload.get("command_name") not in (None, "", COMMANDS[action]):
        return False, "插件返回的命令与本次操作不匹配", ""
    if not friend_check._is_ok(payload.get("ok")) or payload.get("error"):
        return False, _operation_error(action, payload.get("error")), ""
    business = as_object(payload.get("result"))
    if isinstance(business.get("result"), dict):
        if not friend_check._is_ok(business.get("ok")) or business.get("error"):
            return False, _operation_error(action, business.get("error")), ""
        business = business["result"]
    if business.get("id") != target:
        return False, "插件返回的目标好友不匹配", ""
    if ("ok" in business and not friend_check._is_ok(business["ok"])) or business.get("error"):
        return False, _operation_error(action, business.get("error")), ""
    if action == "remark":
        # accepted/sync_requested 仅表示原生调用，必须有本地回读成功的业务状态。
        if business.get("status") not in {"applied_locally", "unchanged"}:
            return False, "备注结果未确认，请核对微信后再操作", ""
        remark = business.get("remark")
        if not isinstance(remark, str) or not remark.strip() or len(remark) > 255:
            return False, "插件返回的备注无法识别，请核对微信后再操作", ""
        return True, "备注已更新", remark
    # 长轮询成功信封为外层 ok=true、result={id}；HTTP 才会在业务结果内重复携带 ok。
    if business.get("status") not in (None, ""):
        return False, "删除结果未确认，请核对微信后再操作", ""
    return True, "好友已删除", ""


def poll_action(db: Session, channel: Channel, job_id: int, *, commit: bool = True) -> dict[str, Any]:
    friend_check._validate_channel(channel)
    job = db.scalar(select(TaskJob).where(
        TaskJob.id == job_id,
        TaskJob.tenant_id == channel.tenant_id,
        TaskJob.job_type == JOB_TYPE,
        TaskJob.related_type == RELATED_TYPE,
    ))
    if job is None:
        raise FriendCheckError(404, "未找到该好友操作任务")
    data = parse_job_payload(job)
    command = as_object(data.get("command"))
    args = as_object(command.get("params"))
    action = next((name for name, value in COMMANDS.items() if value == command.get("type")), "")
    contact = db.scalar(select(WorkWechatContact).where(
        WorkWechatContact.id == job.related_id,
        WorkWechatContact.tenant_id == channel.tenant_id,
        WorkWechatContact.channel_id == channel.id,
        WorkWechatContact.contact_kind == "friend",
    ))
    if data.get("channel_id") != channel.id or not action or contact is None or args.get("id") != contact.contact_external_id:
        raise FriendCheckError(404, "未找到该账号对应的好友操作任务")
    if action == "remark" and (not isinstance(args.get("remark"), str) or not args["remark"]):
        raise FriendCheckError(404, "未找到该账号对应的好友操作任务")
    if friend_check._expire_if_stale(db, job):
        db.commit() if commit else db.flush()
    if job.status in friend_check.ACTIVE_STATUSES:
        return {"state": job.status, "result": None}
    ok, message, remark = _outcome(job, action, contact.contact_external_id)
    if ok and not data.get("friend_action_applied"):
        original = job.payload_json
        data["friend_action_applied"] = True
        # 标记与资料更新处于同一事务；重复或并发轮询不能覆盖后续修改的备注。
        applied = db.execute(update(TaskJob).where(
            TaskJob.id == job.id, TaskJob.tenant_id == channel.tenant_id,
            TaskJob.payload_json == original,
        ).values(payload_json=json.dumps(data, ensure_ascii=False)), execution_options={"synchronize_session": False}).rowcount
        if applied:
            if action == "remark":
                contact.display_name = remark
            elif contact.deleted_at is None:
                contact.deleted_at = now_shanghai()
                contact.sync_status = "deleted"
                contact.delete_reason = "webridge_delete_friend"
            db.commit() if commit else db.flush()
        db.refresh(job)
        db.refresh(contact)
    return {"state": "completed", "result": {
        "ok": ok, "action": action, "contact": friend_check._contact_row(contact), "message": message,
    }}
