"""WeBridge 长轮询服务端的设备状态、命令队列和结果处理。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, TaskJob, WebridgeDeviceSample, WorkWechatContact
from app.services.task_queue import complete_job, enqueue_job, fail_job_terminal, parse_job_payload
from app.core.logging import get_logger

from .contract import COMMAND_GROUPS, COMMANDS, JOB_TYPE, as_object, identity_from_payload, text

logger = get_logger(__name__)

# 手机领取命令后超过该时长仍未回传结果，视为插件卡死或断线，任务置为失败以免链路一直显示进行中。
COMMAND_RUNNING_TIMEOUT_SECONDS = 180
# poll 每次都会进入 claim_commands；超时回收和在线心跳不需要每次执行，按渠道最多每 30 秒一次。
POLL_MAINTENANCE_INTERVAL_SECONDS = 30
# 设备状态（电量、内存等）通过 device_status 命令读取，随 poll 维护（30 秒节流）入队。
# 用较低且独有的优先级标记这类任务，既让业务消息先发，也便于识别和清理。
DEVICE_STATUS_PRIORITY = 7
_last_poll_maintenance: dict[int, float] = {}
# 手机状态采样保留天数；趋势图只看最近 24 小时，多留几天便于排查。
SAMPLE_RETENTION_DAYS = 7


def _num(value: Any) -> int | None:
    """手机侧读不到的值为 null，保留为空而不是 0；布尔值不当作数字。"""

    return int(round(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def count_backlog(db: Session, channel: Channel) -> tuple[int, int]:
    """统计本渠道实时积压：返回（待领取, 执行中）命令数，不含已完成和失败。

    积压任务通常只有个位数，取出后按渠道过滤，不依赖 payload 的 JSON 文本格式。
    device_status 是内部采样命令，不算业务积压。
    """

    pending = running = 0
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status.in_(("pending", "running")), TaskJob.priority != DEVICE_STATUS_PRIORITY)).all():
        if int(parse_job_payload(job).get("channel_id") or 0) != int(channel.id):
            continue
        if job.status == "pending":
            pending += 1
        else:
            running += 1
    return pending, running


def record_device_sample(db: Session, channel: Channel, device: dict[str, Any] | None = None) -> None:
    """写入（或更新）本分钟的手机状态采样，并顺带清理过期采样。

    任务积压数从任务队列实时统计，手机侧缓存数和事件队列数取最近一次 state 上报。
    """

    now = now_shanghai()
    bucket = now.replace(second=0, microsecond=0)
    cid = int(channel.id)
    pending, running = count_backlog(db, channel)
    bridge = _runtime(channel).get("webridge")
    state = as_object(bridge.get("state")) if isinstance(bridge, dict) else {}

    def _int_or_none(key: str) -> int | None:
        value = state.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    sample = db.scalar(select(WebridgeDeviceSample).where(WebridgeDeviceSample.channel_id == cid, WebridgeDeviceSample.bucket_start == bucket))
    if sample is None:
        sample = WebridgeDeviceSample(tenant_id=channel.tenant_id, channel_id=cid, bucket_start=bucket)
        db.add(sample)
    sample.online = True
    sample.pending_commands = pending
    sample.running_commands = running
    sample.cached_message_count = _int_or_none("cached_message_count")
    sample.pending_event_queue_count = _int_or_none("pending_event_queue_count")
    # 设备指标只在 device_status 回报时更新，普通 poll 采样不覆盖已有值。
    if device:
        sample.battery_percent = _num(as_object(device.get("battery")).get("percent"))
        sample.memory_used_percent = _num(as_object(device.get("system_memory")).get("used_estimate_percent"))
        sample.process_memory_mb = _num(as_object(device.get("process_memory")).get("phys_footprint_mb"))
    db.execute(delete(WebridgeDeviceSample).where(WebridgeDeviceSample.channel_id == cid, WebridgeDeviceSample.bucket_start < now - timedelta(days=SAMPLE_RETENTION_DAYS)).execution_options(synchronize_session=False))


def reap_stale_commands(db: Session, channel: Channel) -> int:
    """回收本渠道领取后超时未回报的命令，返回回收数量。

    不自动重试：发消息类命令无法证明手机是在发送前还是发送后卡死，重试可能造成重复发送；
    读取类命令失败后由操作人在后台重新提交即可。
    """

    # 用 SQL 比较时间，避免数据库返回的无时区时间与带时区时间直接比较。
    cutoff = now_shanghai() - timedelta(seconds=COMMAND_RUNNING_TIMEOUT_SECONDS)
    jobs = db.scalars(
        select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status == "running", TaskJob.locked_at < cutoff)
    ).all()
    reaped = 0
    for job in jobs:
        if int(parse_job_payload(job).get("channel_id") or 0) != int(channel.id):
            continue
        fail_job_terminal(db, job, error_message="webridge_command_timeout", result={"timeout_seconds": COMMAND_RUNNING_TIMEOUT_SECONDS})
        reaped += 1
    if reaped:
        logger.warning("webridge stale commands reaped", extra={"channel_id": channel.id, "count": reaped})
    return reaped


def _result_items(result: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    """从微桥不同版本的列表返回结构中提取对象数组。"""

    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _directory_id(item: dict[str, Any], *keys: str) -> str:
    """提取目录实体稳定 ID，页面展示优先使用昵称和头像。"""

    return next((str(item.get(key) or "").strip() for key in keys if str(item.get(key) or "").strip()), "")


def persist_directory_result(db: Session, channel: Channel, command: dict[str, Any], result: dict[str, Any]) -> dict[str, int]:
    """把好友、群组和群成员同步结果写入现有联系人与群组实体。"""

    command_name = text(command.get("type") or command.get("command_name"))
    now = now_shanghai()
    counts = {"contacts": 0, "groups": 0, "members": 0}
    if command_name in {"friends", "friends_all"}:
        for item in _result_items(result, "items", "friends", "contacts"):
            external_id = _directory_id(item, "account_id", "user_name", "wxid", "username", "id")
            if not external_id:
                continue
            contact = db.scalar(select(WorkWechatContact).where(WorkWechatContact.channel_id == channel.id, WorkWechatContact.contact_external_id == external_id))
            if contact is None:
                contact = WorkWechatContact(channel_id=channel.id, contact_external_id=external_id, first_seen_at=now)
                db.add(contact)
            contact.contact_kind = "friend"
            contact.name = str(item.get("nick_name") or item.get("nickname") or item.get("display_name") or item.get("remark") or external_id)[:255]
            contact.display_name = str(item.get("display_name") or item.get("remark") or item.get("nick_name") or item.get("nickname") or external_id)[:255]
            contact.avatar_url = str(item.get("avatar_url") or item.get("avatar") or item.get("head_img_url") or "") or None
            contact.source = "wechat_webridge"
            contact.sync_status = "active"
            contact.last_seen_at = now
            contact.last_synced_at = now
            contact.raw_contact_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
            conversation = db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_id == external_id))
            if conversation is not None:
                conversation.title = contact.display_name
            counts["contacts"] += 1
    elif command_name == "groups_all":
        for item in _result_items(result, "groups", "items", "rooms"):
            external_id = _directory_id(item, "account_id", "room_id", "chatroom_id", "user_name", "wxid", "id")
            if not external_id:
                continue
            room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id))
            if room is None:
                room = Chatroom(bot_id=channel.bot_id, channel_id=channel.id, external_id=external_id, first_seen_at=now)
                db.add(room)
            room.name = str(item.get("display_name") or item.get("room_name") or item.get("nick_name") or item.get("nickname") or item.get("name") or external_id)[:255]
            room.remark = str(item.get("remark") or "")[:255] or None
            room.status = "synced"
            room.last_seen_at = now
            room.last_synced_at = now
            room.member_count = int(item.get("member_count") or item.get("member_total") or item.get("member_num") or room.member_count or 0)
            room.extra_json = json.dumps({"source": "wechat_webridge", "avatar_url": item.get("avatar_url") or item.get("avatar") or "", "raw": item}, ensure_ascii=False, sort_keys=True)
            conversation = db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_id == external_id))
            if conversation is not None:
                conversation.title = room.name
            counts["groups"] += 1
    elif command_name == "room_members":
        params = as_object(command.get("params") or command.get("args"))
        room_id = _directory_id(params, "room_id", "chatroom_id", "conversation_id")
        room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == room_id)) if room_id else None
        if room:
            for item in _result_items(result, "members", "items"):
                external_id = _directory_id(item, "account_id", "user_name", "wxid", "id")
                if not external_id:
                    continue
                member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.member_external_id == external_id))
                if member is None:
                    member = ChatroomMember(chatroom_id=room.id, bot_id=channel.bot_id, channel_id=channel.id, member_external_id=external_id)
                    db.add(member)
                member.nickname = str(item.get("nick_name") or item.get("nickname") or item.get("display_name") or external_id)[:255]
                member.display_name = str(item.get("display_name") or item.get("remark") or item.get("nick_name") or item.get("nickname") or external_id)[:255]
                member.status = "active"
                # 微桥成员项自带 is_owner / is_admin，群详情的角色概览依赖 role 字段。
                member.role = "owner" if item.get("is_owner") else ("admin" if item.get("is_admin") else "member")
                member.last_synced_at = now
                member.extra_json = json.dumps({"avatar_url": item.get("avatar_url") or item.get("avatar") or "", "raw": item}, ensure_ascii=False, sort_keys=True)
                counts["members"] += 1
            room.member_count = max(room.member_count, counts["members"])
            room.last_synced_at = now
    if any(counts.values()):
        db.flush()
        logger.info("webridge directory persisted", extra={"channel_id": channel.id, "command_name": command_name, **counts})
    return counts


# task_jobs.result_json 是 TEXT（上限 65535 字节），大群成员列表可达 6 万字符以上，
# 超出时写入报 Data too long，连同成员落库一起回滚，任务永远完成不了。
RESULT_INLINE_LIMIT_BYTES = 40000


def _compact_result_for_storage(payload: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    """目录类命令的明细已写入各自业务表，任务结果过大时只保存摘要。"""

    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= RESULT_INLINE_LIMIT_BYTES:
        return payload
    result = as_object(payload.get("result"))
    summary = {key: value for key, value in result.items() if not isinstance(value, (list, dict))}
    summary["truncated"] = True
    summary["persisted_counts"] = counts
    return {**{key: value for key, value in payload.items() if key != "result"}, "result": summary}


def command_catalog(channel: Channel | None = None) -> dict[str, Any]:
    """返回稳定命令目录，并附带当前手机注册时上报的能力集合。"""

    runtime = _runtime(channel).get("webridge") if channel is not None else None
    reported = [text(item) for item in (runtime or {}).get("capabilities", []) if text(item)] if isinstance(runtime, dict) else []
    return {
        "groups": {name: list(commands) for name, commands in COMMAND_GROUPS.items()},
        "commands": list(COMMANDS),
        "reported_capabilities": reported,
        "online": bool((runtime or {}).get("online")) if isinstance(runtime, dict) else False,
    }


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


def identity_matches(channel: Channel, payload: dict[str, Any]) -> bool:
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

    if not identity_matches(channel, payload):
        raise ValueError("webridge_identity_mismatch")
    runtime = _runtime(channel)
    bridge = runtime.setdefault("webridge", {})
    bridge.update({"state": as_object(payload.get("state")), "last_seen_at": now_shanghai().isoformat(), "online": True})
    _save_runtime(channel, runtime)
    record_device_sample(db, channel)
    db.commit()
    return {"ok": True}



def enqueue_message_command(db: Session, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> TaskJob:
    """把统一消息类型映射为 WeBridge 原生命令，供普通出站接口直接使用。"""

    message_type = text(payload.get("message_type") or "text").lower()
    receiver = text(payload.get("receiver_id") or payload.get("external_id") or conversation.external_id)
    if not receiver:
        raise ValueError("webridge_message_requires_receiver")
    command_type = {
        "text": "send_message",
        "image": "send_image",
        "emoji": "send_emoji",
        "voice": "send_voice",
        "video": "send_video_from_url",
        "file": "send_file_from_url",
        "link": "send_link_card",
        "card": "send_contact_card",
        "location": "send_location",
        "app": "send_xml",
        "mini_program": "send_xml",
        "signal": "send_signal",
        "pat": "send_pat",
        "music": "send_music_card",
        "livephoto": "send_livephoto",
        "forward": "forward_message",
        "revoke": "revoke_message",
    }.get(message_type)
    if not command_type:
        raise ValueError(f"webridge_message_type_unsupported:{message_type}")
    params: dict[str, Any] = {"receiver": receiver}
    if message_type == "text":
        params["content"] = str(payload.get("content") or payload.get("text") or "").strip()
        # 引用回复和 @ 是微桥 send_message 的原生能力：只在上游明确给出时透传，
        # 引用目标必须是微信侧消息 ID，而不是库内自增 ID。
        quote_message_id = text(payload.get("quote_message_id"))
        if quote_message_id:
            params["quote_message_id"] = quote_message_id
        mention_user_list = [text(item) for item in payload.get("mention_user_list") or [] if text(item)]
        if mention_user_list:
            params["mention_user_list"] = mention_user_list
    elif message_type == "image":
        # 微桥 send_image 的字段是 image_path(本地路径或远程 URL),不接受 base64。
        params["image_path"] = payload.get("image_url") or payload.get("content")
    elif message_type in {"voice", "video"}:
        # 语音、视频用 source(本地路径或远程 URL);大文件不能走 base64,统一传文件地址。
        params["source"] = payload.get("voice_url") or payload.get("video_url")
    elif message_type == "file":
        params["file_url"] = payload.get("file_url") or payload.get("file_path")
    elif message_type == "emoji":
        params["source"] = payload.get("emoji") or payload.get("image_url")
    elif message_type == "link":
        params.update({"title": payload.get("link_title") or payload.get("title") or "", "url": payload.get("link_url") or "", "description": payload.get("link_description") or "", "thumb_url": payload.get("thumb_url") or ""})
    elif message_type == "card":
        params["contact_id"] = payload.get("card_user_id") or payload.get("contact_id") or ""
    elif message_type == "location":
        params.update({key: payload[key] for key in ("latitude", "longitude", "title", "address") if key in payload})
    elif message_type in {"signal", "pat"}:
        params.update({key: payload[key] for key in ("room_id", "content") if key in payload})
    elif message_type == "music":
        params.update({key: payload[key] for key in ("title", "desc", "url", "data_url", "song_album_url", "app_id", "app_name", "song_lyric") if key in payload})
    elif message_type == "livephoto":
        params.update({key: payload[key] for key in ("video_path", "image_path", "still_image_time_ms", "prefered_hevc") if key in payload})
    elif message_type == "forward":
        params["message_id"] = payload.get("message_id") or payload.get("external_message_id") or ""
    elif message_type == "revoke":
        params["message_id"] = payload.get("message_id") or payload.get("external_message_id") or ""
    else:
        params["xml"] = payload.get("xml") or payload.get("cdn_xml") or payload.get("content") or ""
    if not any(value not in (None, "") for key, value in params.items() if key != "receiver"):
        raise ValueError("webridge_message_payload_missing")
    return enqueue_job(db, job_type=JOB_TYPE, payload={"channel_id": int(channel.id), "conversation_id": int(conversation.id), "command": {"request_id": f"job_{uuid.uuid4().hex}", "type": command_type, "params": params}}, priority=90, related_type="conversation", related_id=int(conversation.id))

def enqueue_media_upload_command(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    message_id: str,
    media_type: str,
    upload_url: str,
    attachment_id: int | None = None,
) -> TaskJob:
    """为手机侧媒体准备上传任务。

    WeBridge 的媒体文件只存在手机本地，服务端不能假设自己能读取手机路径；
    因此入站事件先落附件，再由手机执行 upload_message_media 回传字节。
    """

    normalized_message_id = text(message_id)
    normalized_media_type = text(media_type).lower()
    if not normalized_message_id or normalized_media_type not in {"image", "voice", "video", "emoji", "file"}:
        raise ValueError("invalid_webridge_media_upload_request")
    params = {
        "message_id": normalized_message_id,
        "media_type": normalized_media_type,
        "upload_url": text(upload_url),
    }
    if attachment_id:
        params["attachment_id"] = int(attachment_id)
    return enqueue_job(
        db,
        job_type=JOB_TYPE,
        payload={
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "command": {"request_id": f"media_{uuid.uuid4().hex}", "type": "upload_message_media", "params": params},
        },
        priority=95,
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


def list_command_results(
    db: Session,
    channel: Channel,
    *,
    status: str = "",
    command_type: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按渠道列出最近的通用命令结果，便于控制台展示异步操作历史。"""

    normalized_status = text(status).lower()
    normalized_type = text(command_type)
    safe_limit = max(1, min(int(limit or 50), 100))
    stmt = select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.tenant_id == channel.tenant_id).order_by(TaskJob.id.desc()).limit(safe_limit * 3)
    rows: list[dict[str, Any]] = []
    for job in db.scalars(stmt).all():
        payload = parse_job_payload(job)
        if int(payload.get("channel_id") or 0) != int(channel.id):
            continue
        command = as_object(payload.get("command"))
        if normalized_status and str(job.status or "").lower() != normalized_status:
            continue
        if normalized_type and text(command.get("type")) != normalized_type:
            continue
        raw_result = {}
        try:
            raw_result = json.loads(job.result_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_result = {}
        rows.append({
            "request_id": text(command.get("request_id")) or f"job_{int(job.id)}",
            "job_id": int(job.id),
            "command_type": text(command.get("type")),
            "status": job.status,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "result": raw_result,
            "error": job.error_message,
        })
        if len(rows) >= safe_limit:
            break
    return rows

def _poll_maintenance(db: Session, channel: Channel) -> None:
    """按渠道节流执行：回收超时命令，并刷新手机在线时间。

    last_seen_at 以前只在 register/state 时更新，而手机实际只发 poll，页面会误判离线；
    这里用 poll 作为存活证据。
    """

    now = time.monotonic()
    if now - _last_poll_maintenance.get(int(channel.id), 0.0) < POLL_MAINTENANCE_INTERVAL_SECONDS:
        return
    _last_poll_maintenance[int(channel.id)] = now
    reap_stale_commands(db, channel)
    runtime = _runtime(channel)
    bridge = runtime.get("webridge")
    if isinstance(bridge, dict):
        bridge.update({"last_seen_at": now_shanghai().isoformat(), "online": True})
        _save_runtime(channel, runtime)
    record_device_sample(db, channel)
    _enqueue_device_status(db, channel)


def _enqueue_device_status(db: Session, channel: Channel) -> None:
    """定期让手机回报设备状态：同渠道已有未完成的 device_status 时不重复入队。

    已完成的 device_status 任务只保留最近一天，避免每分钟一条把任务表撑大。
    """

    cid = int(channel.id)
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status.in_(("pending", "running")), TaskJob.priority == DEVICE_STATUS_PRIORITY)).all():
        if int(parse_job_payload(job).get("channel_id") or 0) == cid:
            return
    job = enqueue_command(db, channel, command_name="device_status", args={})
    job.priority = DEVICE_STATUS_PRIORITY
    cutoff = now_shanghai() - timedelta(days=1)
    db.execute(delete(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.priority == DEVICE_STATUS_PRIORITY, TaskJob.status == "completed", TaskJob.finished_at < cutoff).execution_options(synchronize_session=False))


def claim_commands(db: Session, channel: Channel, payload: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """领取当前设备的待发送命令，并将任务置为 running 防止重复下发。"""

    if not identity_matches(channel, payload):
        logger.error("webridge command claim identity mismatch", extra={"channel_id": channel.id})
        raise ValueError("webridge_identity_mismatch")
    current = now_shanghai()
    # 手机恢复后第一次 poll 必定执行维护，让链路尽快解除阻塞；之后按间隔节流。
    _poll_maintenance(db, channel)
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
        if command:
            # 通用命令桥允许 friends、groups_all、profile_update、媒体和高级命令，
            # 这些命令并不携带 receiver/content，不能套用文本消息的字段校验。
            command["request_id"] = text(command.get("request_id")) or f"job_{int(job.id)}"
            command["type"] = text(command.get("type") or command.get("command_name"))
            command["params"] = as_object(command.get("params") or command.get("args"))
            if command["type"]:
                # 微桥 8080 插件按 command_name/args 读取任务；保留 type/params 供旧客户端和内部调试使用。
                command["command_name"] = command["type"]
                command["args"] = command["params"]
                commands.append(command)
        else:
            outbound = as_object(data.get("payload"))
            message_type = text(outbound.get("message_type") or "text")
            if message_type != "text":
                job.status = "failed"
                job.error_message = "WeBridge 普通出站链路暂只支持文本，请使用通用命令桥"
                job.finished_at = current
                continue
            receiver = text(outbound.get("receiver_id") or outbound.get("external_id"))
            content = str(outbound.get("content") or outbound.get("text") or "").strip()
            if not receiver or not content:
                job.status = "failed"
                job.error_message = "webridge_text_message_requires_content_and_receiver"
                job.finished_at = current
                continue
            # 插件自动回复走这条分支：同样必须带 command_name/args，手机插件只认这两个字段，
            # 缺失会返回 command_name is required 而导致回复发不出去。
            commands.append({
                "request_id": f"job_{int(job.id)}",
                "type": "send_message",
                "params": {"receiver": receiver, "content": content},
                "command_name": "send_message",
                "args": {"receiver": receiver, "content": content},
            })
        if len(commands) >= max(1, min(int(limit or 5), 20)):
            break
    db.commit()
    # 空轮询占绝大多数，只记录真正下发了命令的情况，避免日志被刷满。
    if commands:
        logger.info("webridge commands claimed", extra={"channel_id": channel.id, "command_count": len(commands), "command_names": [str(item.get("command_name") or item.get("type") or "") for item in commands]})
    return commands


def accept_command_result(db: Session, channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    """按 command request_id 完成任务；重复回报返回已处理状态。"""

    if not identity_matches(channel, payload):
        logger.error("webridge command result identity mismatch", extra={"channel_id": channel.id})
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
        # 只允许回报本渠道的任务，禁止一个渠道的手机改写其他渠道的命令结果。
        if int(data.get("channel_id") or 0) != int(channel.id):
            continue
        # 非 job_ 前缀的回报必须与任务保存的 request_id 严格相等。
        # 之前"保存的 request_id 为空也放行"，会让无 command 的普通出站任务被误匹配，
        # 回报被当成重复提交吞掉，真正的命令永远停在 running。
        stored_request_id = text(as_object(data.get("command")).get("request_id"))
        if job_id <= 0 and stored_request_id != request_id:
            continue
        if job.status in {"completed", "failed"}:
            return {"ok": True, "deduplicated": True, "job_id": int(job.id), "status": job.status}
        result = as_object(payload.get("result"))
        if bool(payload.get("ok")) or bool(result.get("ok")):
            command = as_object(data.get("command"))
            counts = persist_directory_result(db, channel, command, result)
            if text(command.get("type") or command.get("command_name")) == "device_status":
                _save_device_status(db, channel, as_object(result.get("result")) or result)
            complete_job(db, job, result={"webridge": _compact_result_for_storage(payload, counts)})
            logger.info("webridge command completed", extra={"channel_id": channel.id, "job_id": job.id, "request_id": request_id})
            return {"ok": True, "job_id": int(job.id), "status": "completed"}
        fail_job_terminal(db, job, error_message=text(result.get("error") or payload.get("error") or "webridge_command_failed"), result={"webridge": payload})
        logger.error("webridge command failed", extra={"channel_id": channel.id, "job_id": job.id, "request_id": request_id, "error_code": text(result.get("error") or payload.get("error") or "webridge_command_failed")})
        return {"ok": True, "job_id": int(job.id), "status": "failed"}
    raise ValueError("webridge_request_not_found")


def _save_device_status(db: Session, channel: Channel, device: dict[str, Any]) -> None:
    """保存 device_status 快照到渠道运行信息（供页面展示）并写入当前分钟采样。"""

    runtime = _runtime(channel)
    bridge = runtime.setdefault("webridge", {})
    bridge["device"] = {key: device[key] for key in ("device", "battery", "power", "thermal", "network", "application", "process_memory", "system_memory", "ts_ms") if key in device}
    _save_runtime(channel, runtime)
    record_device_sample(db, channel, device)


def mark_event_seen(channel: Channel, payload: dict[str, Any]) -> str:
    """提取事件 ID；没有 ID 时用稳定正文摘要，避免同一轮重试重复落库。"""

    event_id = text(payload.get("event_id") or payload.get("message_id") or payload.get("id"))
    if event_id:
        return event_id
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "hash_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
