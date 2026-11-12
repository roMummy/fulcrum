"""咕嘎助手 Agent 设备注册、命令队列与事件去重。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel, Message, TaskJob, WorkWechatContact
from app.services.task_queue import enqueue_job, parse_job_payload
from app.services.wechat_gugatool_directory import save_account_profile
from app.services.wechat_webridge import wakeup

from .contract import (
    COMMAND_NAMES,
    COMMAND_RUNNING_TIMEOUT_SECONDS,
    JOB_TYPE,
    RUNTIME_KEY,
    SEND_COMMANDS,
    GugaAgentError,
    as_object,
    identity_from_payload,
    text,
)

logger = get_logger(__name__)


def _runtime(channel: Channel) -> dict[str, Any]:
    try:
        value = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save_runtime(channel: Channel, value: dict[str, Any]) -> None:
    channel.runtime_info_json = json.dumps(value, ensure_ascii=False, sort_keys=True)


def registration(channel: Channel) -> dict[str, Any]:
    return as_object(_runtime(channel).get(RUNTIME_KEY))


def register_device(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """保存手机注册快照;同设备同账号重复注册复用原会话。"""

    identity = identity_from_payload(payload)
    if not identity["device_id"] or not identity["account_id"]:
        raise GugaAgentError("invalid_guga_identity", "缺少 device_id 或 account_id")
    runtime = _runtime(channel)
    current = as_object(runtime.get(RUNTIME_KEY))
    changed = bool(
        current.get("device_id") and (current.get("device_id") != identity["device_id"] or current.get("account_id") != identity["account_id"])
    )
    if changed and channel.is_enabled:
        raise GugaAgentError("identity_mismatch", "该账号已绑定其他设备或微信账号", status=409)
    session_id = ""
    if not changed:
        session_id = text(current.get("session_id"))
    session_id = session_id or f"guga_{uuid.uuid4().hex}"
    runtime[RUNTIME_KEY] = {
        "device_id": identity["device_id"],
        "account_id": identity["account_id"],
        "session_id": session_id,
        "runtime_version": text(payload.get("runtime_version")),
        "build_marker": text(payload.get("build_marker")),
        "capabilities": [text(item) for item in payload.get("capabilities", []) if text(item)],
        "state": as_object(payload.get("state")),
        "transport": "http",
        "registered_at": text(current.get("registered_at")) or now_shanghai().isoformat(),
        "last_seen_at": now_shanghai().isoformat(),
        "online": True,
    }
    _save_runtime(channel, runtime)
    _apply_profile_payload(channel, payload, identity["account_id"])
    db.commit()
    # 首次注册(或好友表为空)时自动补一次通讯录,手机重启后无需人工点同步。
    if channel.is_enabled and _needs_friend_sync(db, channel):
        enqueue_command(db, channel, command_name="get_friend_list", params={}, priority=45, purpose="friend_sync")
        db.commit()
    logger.info(
        "guga agent device registered",
        extra={"channel_id": channel.id, "device_id": identity["device_id"][:12], "account_id": identity["account_id"][:12], "changed": changed},
    )
    return {"session_id": session_id, "device_id": identity["device_id"], "account_id": identity["account_id"]}


def _needs_friend_sync(db: Session, channel: Channel) -> bool:
    """渠道还没有任何有效好友时补一次通讯录同步。"""

    count = db.scalar(
        select(func.count(WorkWechatContact.id)).where(
            WorkWechatContact.channel_id == channel.id,
            WorkWechatContact.contact_kind == "friend",
            WorkWechatContact.sync_status == "active",
        )
    )
    return int(count or 0) == 0


def identity_matches(channel: Channel, payload: dict[str, Any], *, require_session: bool = False) -> bool:
    identity = identity_from_payload(payload)
    current = registration(channel)
    if not current or not identity["device_id"]:
        return False
    if identity["device_id"] != current.get("device_id"):
        return False
    # 手机端的 poll/event 只带 device_id + session_id，不带 account_id；
    # 有 account_id 时必须与注册快照一致，没有时以设备和会话为准。
    if identity["account_id"] and identity["account_id"] != current.get("account_id"):
        return False
    if require_session and identity["session_id"] != text(current.get("session_id")):
        return False
    return True


def accept_state(db: Session, channel: Channel, payload: dict[str, Any], *, transport: str = "http") -> dict[str, Any]:
    """更新心跳与状态快照;失败不影响手机继续轮询。"""

    if not identity_matches(channel, payload):
        raise GugaAgentError("identity_mismatch", "设备身份与注册快照不一致", status=409)
    runtime = _runtime(channel)
    current = as_object(runtime.get(RUNTIME_KEY))
    current.update(
        {
            "state": as_object(payload.get("state")),
            "transport": text(transport) or "http",
            "last_seen_at": now_shanghai().isoformat(),
            "online": True,
        }
    )
    runtime[RUNTIME_KEY] = current
    _save_runtime(channel, runtime)
    _apply_profile_payload(channel, payload, str(current.get("account_id") or ""))
    channel.last_heartbeat_at = now_shanghai()
    db.commit()
    return {"ok": True}


def health_snapshot(channel: Channel) -> dict[str, Any]:
    current = registration(channel)
    from .access import account_info

    return account_info(channel)


def _apply_profile_payload(channel: Channel, payload: dict[str, Any], account_id: str) -> None:
    """手机注册/心跳可携带账号资料;兼容嵌套 profile 和扁平字段。"""

    profile = as_object(payload.get("profile"))
    nickname = text(
        profile.get("nickname")
        or profile.get("NickName")
        or payload.get("nickname")
        or payload.get("nick_name")
    )
    avatar_url = text(
        profile.get("avatar_url")
        or profile.get("HeadImgUrl")
        or profile.get("head_img_url")
        or payload.get("avatar_url")
        or payload.get("head_img_url")
    )
    if not nickname and not avatar_url:
        return
    wxid = text(profile.get("wxid") or profile.get("Wxid")) or account_id
    save_account_profile(channel, account_id=wxid, name=nickname, avatar_url=avatar_url)


def enqueue_command(
    db: Session,
    channel: Channel,
    *,
    command_name: str,
    params: dict[str, Any],
    request_id: str = "",
    priority: int = 90,
    conversation_id: int | None = None,
    purpose: str = "",
) -> TaskJob:
    """把一条 Agent 命令写入队列,由手机 poll/WS 领取。"""

    name = text(command_name)
    if name not in COMMAND_NAMES:
        raise GugaAgentError("unsupported_guga_command", f"手机端不支持命令 {name}")
    if not isinstance(params, dict):
        raise GugaAgentError("guga_command_params_must_be_object")
    rid = text(request_id) or f"guga_{uuid.uuid4().hex}"
    job = enqueue_job(
        db,
        job_type=JOB_TYPE,
        payload={
            "channel_id": int(channel.id),
            "conversation_id": int(conversation_id or 0),
            "purpose": text(purpose),
            "command": {"request_id": rid, "command_name": name, "params": params},
        },
        priority=priority,
        related_type="wechat_gugatool_command",
        dedupe_key=f"guga:{int(channel.id)}:{rid}",
        max_attempts=1,
    )
    db.flush()
    # 事务提交后再通知;即使调用方回滚,多一次空唤醒也无副作用。
    wakeup.notify(int(channel.id))
    return job


def claim_commands(
    db: Session,
    channel: Channel,
    payload: dict[str, Any],
    *,
    limit: int = 5,
    max_payload_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """按设备会话语义领取待执行命令,并置为 running 防止重复下发。"""

    if not identity_matches(channel, payload, require_session=True):
        current = registration(channel)
        if current and text(payload.get("device_id")) == current.get("device_id") and text(payload.get("account_id")) == current.get("account_id"):
            raise GugaAgentError("session_expired", "设备会话已失效,请重新注册", status=410)
        raise GugaAgentError("identity_mismatch", "设备身份与注册快照不一致", status=409)
    reap_stale_commands(db, channel)
    if not channel.is_enabled:
        return []
    current_time = now_shanghai()
    jobs = db.scalars(
        select(TaskJob)
        .where(TaskJob.job_type == JOB_TYPE, TaskJob.status == "pending", TaskJob.available_at <= current_time)
        .order_by(TaskJob.priority.desc(), TaskJob.id.asc())
        .limit(max(1, min(int(limit or 5), 20)) * 3)
    ).all()
    commands: list[dict[str, Any]] = []
    for job in jobs:
        data = parse_job_payload(job)
        if int(data.get("channel_id") or 0) != int(channel.id):
            continue
        command = as_object(data.get("command"))
        name = text(command.get("command_name"))
        if not name:
            continue
        if max_payload_bytes is not None:
            encoded = json.dumps(command, ensure_ascii=False).encode("utf-8")
            if len(encoded) > int(max_payload_bytes):
                # 大命令留给 HTTP 长轮询,避免 WS 大帧断连。
                continue
        job.status = "running"
        job.attempts = int(job.attempts or 0) + 1
        job.started_at = current_time
        job.locked_at = current_time
        commands.append(
            {
                "request_id": text(command.get("request_id")) or f"job_{int(job.id)}",
                "command_name": name,
                "params": as_object(command.get("params")),
            }
        )
        if len(commands) >= max(1, min(int(limit or 5), 20)):
            break
    db.commit()
    if commands:
        logger.info(
            "guga agent commands claimed",
            extra={"channel_id": channel.id, "command_count": len(commands), "command_names": [item["command_name"] for item in commands]},
        )
    return commands


def find_job(db: Session, channel: Channel, request_id: str) -> TaskJob | None:
    normalized = text(request_id)
    if not normalized:
        return None
    job = db.scalar(select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.dedupe_key == f"guga:{int(channel.id)}:{normalized}"))
    return job


def accept_command_result(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """手机回报命令结果;重复回报幂等,未知 request 不报错以免客户端重试风暴。"""

    current = registration(channel)
    if not current or text(payload.get("device_id")) != current.get("device_id"):
        raise GugaAgentError("identity_mismatch", "设备身份与注册快照不一致", status=409)
    request_id = text(payload.get("request_id"))
    if not request_id:
        raise GugaAgentError("missing_request_id", "缺少 request_id")
    job = find_job(db, channel, request_id)
    if job is None:
        return {"ok": True, "accepted": False, "deduplicated": False}
    if job.status in {"completed", "failed"}:
        return {"ok": True, "accepted": True, "deduplicated": True, "job_id": int(job.id), "status": job.status}
    data = parse_job_payload(job)
    command = as_object(data.get("command"))
    command_name = text(command.get("command_name"))
    ok = bool(payload.get("ok"))
    result = as_object(payload.get("result"))
    error = text(payload.get("error"))
    job.status = "completed" if ok else "failed"
    job.result_json = json.dumps(result, ensure_ascii=False) if result else None
    job.error_message = None if ok else (error or f"{command_name}_failed")
    job.finished_at = now_shanghai()
    _settle_outbound_message(db, job, ok=ok, error=error)
    if ok and command_name == "get_chatroom_info":
        room_id = text(as_object(command.get("params")).get("chatroom_wxid"))
        if room_id:
            from app.services.wechat_gugatool_chatrooms import apply_wechat_gugatool_roster_result

            apply_wechat_gugatool_roster_result(db, channel, room_id=room_id, result=result)
    if ok and command_name == "get_friend_list":
        from app.services.wechat_gugatool_contacts import apply_wechat_gugatool_friend_list

        apply_wechat_gugatool_friend_list(db, channel, result)
    if ok and command_name == "get_friend_detail":
        from app.services.wechat_gugatool_contacts import apply_wechat_gugatool_friend_detail

        apply_wechat_gugatool_friend_detail(db, channel, result)
    if ok and command_name == "get_profile":
        profile = result.get("Data") if isinstance(result.get("Data"), dict) else result
        if isinstance(profile, dict) and profile:
            _apply_profile_payload(channel, {"profile": profile}, text(registration(channel).get("account_id")))
    if ok and command_name in {"get_recent_messages", "sync_messages"}:
        _ingest_history_result(db, channel, result)
    db.commit()
    return {"ok": True, "accepted": True, "deduplicated": False, "job_id": int(job.id), "status": job.status}


def get_command_result(db: Session, channel: Channel, request_id: str) -> dict[str, Any] | None:
    job = find_job(db, channel, request_id)
    if job is None:
        return None
    result: dict[str, Any] = {}
    try:
        parsed = json.loads(job.result_json or "{}")
        result = parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {}
    return {"request_id": request_id, "job_id": int(job.id), "status": job.status, "result": result, "error": job.error_message}


def reap_stale_commands(db: Session, channel: Channel) -> int:
    """回收领取后超时未回报的命令;发送类不自动重试,避免重复消息。"""

    cutoff = now_shanghai() - timedelta(seconds=COMMAND_RUNNING_TIMEOUT_SECONDS)
    jobs = db.scalars(
        select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status == "running", TaskJob.locked_at < cutoff)
    ).all()
    reaped = 0
    for job in jobs:
        data = parse_job_payload(job)
        if int(data.get("channel_id") or 0) != int(channel.id):
            continue
        job.status = "failed"
        job.error_message = "guga_agent_command_timeout"
        job.finished_at = now_shanghai()
        _settle_outbound_message(db, job, ok=False, error="guga_agent_command_timeout")
        reaped += 1
    if reaped:
        db.commit()
    return reaped


def mark_event_seen(channel: Channel, payload: dict[str, Any]) -> str:
    """事件去重键优先用手机生成的 event_id。"""

    event_id = text(payload.get("event_id") or payload.get("message_id") or payload.get("id"))
    return event_id


def ingest_event(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """把手机上报的 Agent 事件交给统一入站链路。"""

    if not identity_matches(channel, payload):
        raise GugaAgentError("identity_mismatch", "设备身份与注册快照不一致", status=409)
    if not channel.is_enabled:
        return {"ok": True, "ignored": True}
    from app.services.channel_events import ingest_inbound_event

    event_name = text(payload.get("event_name")) or "message.received"
    body = as_object(payload.get("body"))
    # 手机只上报消息/撤回两类事件;body 与直连 Msg/Sync 的 item 结构一致,
    # 因此可以直接复用适配器里已有的 normalize 实现。
    body.setdefault("kind", "revoke" if event_name == "message.revoke" else "incoming")
    event_key = mark_event_seen(channel, payload) or text(body.get("id"))
    if event_key:
        body.setdefault("id", event_key)
    result = ingest_inbound_event(db, channel, body, headers={}, source="wechat_gugatool_agent")
    normalized = as_object(result.get("normalized"))
    conversation = as_object(normalized.get("conversation"))
    room_id = text(conversation.get("external_id"))
    if (
        not result.get("deduplicated")
        and text(conversation.get("chat_type")) == "group"
        and room_id.endswith("@chatroom")
    ):
        from app.services.wechat_gugatool_directory import cached_group, request_group_refresh

        if not cached_group(int(channel.id), room_id) and request_group_refresh(int(channel.id), room_id):
            try:
                # 事件响应后再由手机领取,避免在 HTTP 事件请求里同步等待命令。
                enqueue_command(
                    db,
                    channel,
                    command_name="get_chatroom_info",
                    params={"chatroom_wxid": room_id},
                    priority=6,
                    purpose="chatroom_roster",
                )
            except Exception:  # noqa: BLE001
                from app.services.wechat_gugatool_directory import finish_group_refresh

                finish_group_refresh(int(channel.id), room_id)
    db.commit()
    return {"ok": True, "deduplicated": bool(result.get("deduplicated")), "event_id": event_key}


def _ingest_history_result(db: Session, channel: Channel, result: dict[str, Any]) -> None:
    """把手机补拉的历史消息交给统一入站链路;重复批次由事件键去重。"""

    payload = result.get("Data") if isinstance(result.get("Data"), dict) else result
    items = (payload.get("Items") or payload.get("items")) if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return
    from app.services.channel_events import ingest_inbound_event

    ingested = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        body = dict(item)
        event_key = text(body.get("id"))
        if not event_key:
            continue
        body.setdefault("kind", "incoming")
        try:
            ingest_inbound_event(db, channel, body, headers={}, source="wechat_gugatool_history")
        except Exception:  # noqa: BLE001
            # 单条历史消息异常不应中断整批补拉。
            logger.warning("guga history item ingest failed", extra={"channel_id": channel.id, "event_id": event_key})
            continue
        ingested += 1
    logger.info("guga history ingested", extra={"channel_id": channel.id, "ingested": ingested})


def _settle_outbound_message(db: Session, job: TaskJob, *, ok: bool, error: str = "") -> None:
    """按 request_id 找回入队时落库的出站消息,更新最终发送状态。"""

    data = parse_job_payload(job)
    command = as_object(data.get("command"))
    request_id = text(command.get("request_id"))
    command_name = text(command.get("command_name"))
    conversation_id = int(data.get("conversation_id") or 0)
    if not request_id or not conversation_id or command_name not in SEND_COMMANDS:
        return
    pending = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.direction == "out", Message.external_status == "pending")
        .order_by(Message.id.desc())
        .limit(50)
    ).all()
    for message in pending:
        if request_id in (message.raw_request_json or ""):
            message.status = "sent" if ok else "failed"
            message.external_status = "sent" if ok else "failed"
            if error:
                message.raw_response_json = json.dumps({"error": error}, ensure_ascii=False)
            return
