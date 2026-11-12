"""WeBridge 长轮询服务端的设备状态、命令队列和结果处理。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation, Message, TaskJob, WebridgeDeviceSample, WorkWechatContact
from app.services import payment_ledger
from app.services.identity_directory import looks_like_raw_id
from app.services.plugin_scopes import cancel_disallowed_plugin_outbound_job
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
# 账号资料(account_status:昵称、头像)同样是内部采样命令,用独有的优先级标记,不算业务积压。
ACCOUNT_STATUS_PRIORITY = 6
# 账号资料多久刷新一次;昵称很少变,没拿到时至少隔这么久才重新请求,避免手机不支持时每 30 秒重试。
ACCOUNT_PROFILE_REFRESH_SECONDS = 6 * 3600
ACCOUNT_PROFILE_RETRY_SECONDS = 600
_last_poll_maintenance: dict[int, float] = {}
# 手机状态采样保留天数；趋势图只看最近 24 小时，多留几天便于排查。
SAMPLE_RETENTION_DAYS = 7


def _num(value: Any) -> int | None:
    """手机侧读不到的值为 null，保留为空而不是 0；布尔值不当作数字。"""

    return int(round(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def count_backlog(db: Session, channel: Channel) -> tuple[int, int]:
    """统计本渠道实时积压：返回（待领取, 执行中）命令数，不含已完成和失败。

    积压任务通常只有个位数，取出后按渠道过滤，不依赖 payload 的 JSON 文本格式。
    device_status、account_status 是内部采样命令，不算业务积压。
    """

    pending = running = 0
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status.in_(("pending", "running")), TaskJob.priority.notin_((DEVICE_STATUS_PRIORITY, ACCOUNT_STATUS_PRIORITY)))).all():
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


def settle_outbound_message(db: Session, job: TaskJob, *, ok: bool, error: str = "") -> None:
    """命令结束后更新对应的出站消息记录:手机执行成功标记已发出,失败或超时标记发送失败。

    出站消息在入队时就已落库(状态为待执行),并把命令的 request_id 记在 raw_request_json 里;
    这里按会话和 request_id 找回它。读取类命令(目录、状态等)没有对应消息,找不到时什么也不做。
    调用方负责提交。
    """

    data = parse_job_payload(job)
    request_id = text(as_object(data.get("command")).get("request_id"))
    conversation_id = int(data.get("conversation_id") or 0)
    if not request_id or not conversation_id:
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
        settle_outbound_message(db, job, ok=False, error="webridge_command_timeout")
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


def _merge_directory_extra(existing: str | None, **updates: Any) -> str:
    """目录同步只更新协议资料,保留群功能、媒体开关及本地成员元数据。"""

    try:
        extra = as_object(json.loads(existing or "{}"))
    except (TypeError, ValueError):
        extra = {}
    return json.dumps({**extra, **updates}, ensure_ascii=False, sort_keys=True)


def persist_directory_result(
    db: Session, channel: Channel, command: dict[str, Any], result: dict[str, Any],
    *, snapshot_revision: int | None = None, snapshot_started_at: datetime | None = None,
) -> dict[str, int]:
    """把好友、群组和群成员同步结果写入现有联系人与群组实体。"""

    command_name = text(command.get("type") or command.get("command_name"))
    if command_name in {"groups_all", "room_members"}:
        db.flush()
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
            room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id).with_for_update().execution_options(populate_existing=True))
            if room is None:
                # 新发现的群默认启用;已存在的群保持后台设置的启用状态,
                # 同步群列表不能把手动停用的群重新打开。
                room = Chatroom(bot_id=channel.bot_id, channel_id=channel.id, external_id=external_id, first_seen_at=now, is_active=True, management_status="enabled")
                db.add(room)
            # 之前被标记为已退出的群又出现在列表里:恢复退群前的启用状态。
            from app.services.wechat_webridge import group_sync

            group_sync.restore_if_returned(room)
            room.status = "synced"
            room.last_seen_at = now
            room.last_synced_at = now
            from app.services.wechat_webridge import member_directory

            latest_event = member_directory.state(room).get("event_received_at")
            newer_event = latest_event and snapshot_started_at is not None and datetime.fromisoformat(latest_event) > ensure_shanghai_datetime(snapshot_started_at)
            if not newer_event:
                room.name = str(item.get("display_name") or item.get("room_name") or item.get("nick_name") or item.get("nickname") or item.get("name") or external_id)[:255]
                room.remark = str(item.get("remark") or "")[:255] or None
                for key in ("member_count", "member_total", "member_num"):
                    value = item.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        room.member_count = value
                        break
            room.extra_json = _merge_directory_extra(room.extra_json, source="wechat_webridge", avatar_url=item.get("avatar_url") or item.get("avatar") or "", raw=item)
            conversation = db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_id == external_id))
            if conversation is not None:
                conversation.title = room.name
            counts["groups"] += 1
    elif command_name == "room_members":
        params = as_object(command.get("params") or command.get("args"))
        room_id = _directory_id(params, "room_id", "chatroom_id", "conversation_id")
        room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == room_id).with_for_update().execution_options(populate_existing=True)) if room_id else None
        if room:
            from app.services.wechat_webridge import member_directory

            # 手机查询期间又发生了成员事件:整份旧快照不落库,完成任务后再安排一次读取。
            if snapshot_revision is not None and snapshot_revision != member_directory.revision(room):
                return {**counts, "stale_snapshot": 1}
            latest_event = member_directory.state(room).get("event_received_at")
            if snapshot_revision is None and latest_event and snapshot_started_at is not None and datetime.fromisoformat(latest_event) > ensure_shanghai_datetime(snapshot_started_at):
                return {**counts, "stale_snapshot": 1}
            if result.get("room_id") and text(result["room_id"]) != room_id:
                return {**counts, "invalid_snapshot": 1}
            items = _result_items(result, "members", "items")
            complete = member_directory.complete_snapshot(result, items)
            if isinstance(result.get("room_name"), str) and result["room_name"]:
                room.name = result["room_name"][:255]
                conversation = db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_id == room_id))
                if conversation is not None:
                    conversation.title = room.name
            if isinstance(result.get("room_remark"), str):
                room.remark = result["room_remark"][:255] or None
            owner = text(result.get("owner_user_name") or result.get("owner_wxid"))
            if owner:
                room.owner_external_id = owner[:128]
            seen_ids: set[str] = set()
            explicitly_left: set[str] = set()
            for item in items:
                external_id = _directory_id(item, "account_id", "user_name", "wxid", "id")
                if not external_id:
                    continue
                seen_ids.add(external_id)
                member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.member_external_id == external_id))
                if member is None:
                    member = ChatroomMember(chatroom_id=room.id, bot_id=channel.bot_id, channel_id=channel.id, member_external_id=external_id)
                    db.add(member)
                member.nickname = str(item.get("nick_name") or item.get("nickname") or member.nickname or item.get("display_name") or external_id)[:255]
                member_state = as_object(member_directory.extra_object(member.extra_json).get(member_directory.STATE_KEY))
                member_state["observed_only"] = False
                if isinstance(item.get("room_nick_name"), str):
                    member_state["room_nickname"] = item["room_nick_name"][:255]
                room_nickname = member_state.get("room_nickname")
                member.display_name = str(room_nickname if room_nickname else (member.nickname if room_nickname == "" else item.get("display_name") or item.get("remark") or member.display_name or member.nickname))[:255]
                # 不完整快照只补资料,不能把明确退群的人重新改成在群。
                if member.status == "left" and member_state.get("membership") == "left":
                    # 明确退群事件的优先级高于快照出现;真实入群事件/新发言会恢复状态。
                    explicitly_left.add(external_id)
                elif complete or not member_state.get("event_at"):
                    member.status = "active"
                # 微桥成员项自带 is_owner / is_admin，群详情的角色概览依赖 role 字段。
                if item.get("is_owner") or external_id == owner:
                    member.role = "owner"
                elif "is_admin" in item:
                    member.role = "admin" if item["is_admin"] else "member"
                elif not member.role or (owner and member.role == "owner"):
                    member.role = "member"
                member.last_synced_at = now
                extra = member_directory.extra_object(member.extra_json)
                member.extra_json = _merge_directory_extra(
                    member.extra_json, avatar_url=item.get("avatar_url") or item.get("avatar") or extra.get("avatar_url") or "", raw=item,
                    **{member_directory.STATE_KEY: member_state},
                )
                counts["members"] += 1
            # 以前的群友要保留:这次名单里已经没有的成员只标记为已退群,不删除,历史发言与积分都还在。
            # 名单为空或返回自带“被截断/还有更多”标记时不是完整名单,不做标记,避免一次异常返回误伤全员。
            if complete:
                counts["members_left"] = _mark_missing_members_left(db, room, seen_ids, now)
                room.member_count = len(seen_ids - explicitly_left)
            else:
                room.member_count = max(room.member_count, len(seen_ids - explicitly_left))
            room.last_synced_at = now
            counts["members_complete"] = int(complete)
            if complete:
                member_directory.save_state(room, synced_at=now.isoformat())
    if any(counts.values()):
        db.flush()
        logger.info("webridge directory persisted", extra={"channel_id": channel.id, "command_name": command_name, **counts})
    return counts


def _mark_missing_members_left(db: Session, room: Chatroom, seen_ids: set[str], now: Any) -> int:
    """把本群成员表中不在 seen_ids 里的在群成员标记为 left,返回标记人数。"""

    marked = 0
    for member in db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.status != "left")).all():
        if member.member_external_id not in seen_ids:
            member.status = "left"
            member.last_synced_at = now
            marked += 1
    return marked


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
    previous = as_object(runtime.get("webridge"))
    if previous.get("account_id") and (
        previous.get("account_id") != identity["account_id"] or previous.get("device_id") != identity["device_id"]
    ):
        raise ValueError("webridge_identity_mismatch")
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
    # bot 已不在这个群里(退群或被移出):不再向它下发发送命令,定时推送等不会反复失败。
    if receiver.endswith("@chatroom") and db.scalar(
        select(Chatroom.id).where(Chatroom.channel_id == channel.id, Chatroom.external_id == receiver, Chatroom.status == "left").limit(1)
    ) is not None:
        raise ValueError("bot 已不在这个群里（退群或被移出），无法发送")
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

def build_upload_url(channel: Channel, attachment_id: int) -> str:
    """手机回传媒体文件的地址:渠道配置里有 upload_url 就用它(相对地址补成渠道绝对地址),否则用渠道的 /agent/upload_media。

    手机必须能直接发起 HTTP 请求,不能是只对浏览器相对当前站点有效的路径。
    """

    try:
        config = json.loads(channel.adapter_config_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        config = {}
    configured = str(config.get("upload_url") or "").strip() if isinstance(config, dict) else ""
    from .access import connection_url

    private_base = connection_url(channel)
    base_url = private_base or str(channel.api_base or "").strip().rstrip("/")
    # 已生成凭据的账号始终使用专属上传入口，旧适配配置不能把请求导回无 Token 地址。
    if private_base or not configured:
        return f"{base_url}/agent/upload_media?attachment_id={int(attachment_id)}"
    # 兼容历史渠道配置:旧的相对地址统一补成微桥渠道绝对地址。
    return f"{base_url}{configured}" if configured.startswith("/") else configured


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



def enqueue_command(
    db: Session,
    channel: Channel,
    *,
    command_name: str,
    args: dict[str, Any],
    request_id: str = "",
    priority: int = 90,
    available_at: datetime | None = None,
) -> TaskJob:
    """把任意 WeBridge 命令放入当前渠道队列，供联系人、群组、媒体和高级能力复用。

    available_at 不为空时命令要到那个时间才会被手机领取(延迟下发);priority 越大越先被领取。
    """

    normalized_name = text(command_name)
    if not normalized_name or normalized_name.startswith("/") or len(normalized_name) > 100:
        raise ValueError("invalid_webridge_command_name")
    if not isinstance(args, dict):
        raise ValueError("webridge_command_args_must_be_object")
    normalized_request_id = text(request_id) or f"cmd_{uuid.uuid4().hex}"
    payload = {
        "channel_id": int(channel.id),
        "command": {"request_id": normalized_request_id, "type": normalized_name, "params": args},
    }
    if normalized_name == "room_members":
        from app.services.wechat_webridge import member_directory

        room = member_directory.find_room(db, channel, text(args.get("room_id")))
        payload["directory_revision"] = member_directory.revision(room)
    return enqueue_job(
        db,
        job_type=JOB_TYPE,
        payload=payload,
        priority=priority,
        dedupe_key=f"webridge:{channel.id}:{normalized_request_id}",
        available_at=available_at,
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
    _enqueue_account_status(db, channel)
    # bot 被拉进新群后自动同步群资料和成员:定期对账(其余触发见 group_sync)。
    from app.services.wechat_webridge import group_sync

    group_sync.reconcile_if_due(db, channel)
    from app.services.wechat_webridge import member_directory

    member_directory.retry_due(db, channel)


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


def _seconds_since(value: Any) -> float | None:
    """距离某个 ISO 时间已过去的秒数;读不到返回 None。"""

    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return (now_shanghai() - ensure_shanghai_datetime(moment)).total_seconds()


def _enqueue_account_status(db: Session, channel: Channel) -> None:
    """定期让手机回报账号资料(昵称、头像),用于后台显示账号昵称而不是 wxid。

    已有资料且未过期不请求;刚请求过(无论成败)隔一段时间再试,手机不支持时不会每 30 秒重试;
    同渠道已有未完成的 account_status 不重复入队。
    """

    cid = int(channel.id)
    profile = as_object(_runtime(channel).get("account_profile"))
    age = _seconds_since(profile.get("fetched_at"))
    if age is not None and age < ACCOUNT_PROFILE_REFRESH_SECONDS:
        return
    since_request = _seconds_since(profile.get("requested_at"))
    if since_request is not None and since_request < ACCOUNT_PROFILE_RETRY_SECONDS:
        return
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.status.in_(("pending", "running")), TaskJob.priority == ACCOUNT_STATUS_PRIORITY)).all():
        if int(parse_job_payload(job).get("channel_id") or 0) == cid:
            return
    job = enqueue_command(db, channel, command_name="account_status", args={})
    job.priority = ACCOUNT_STATUS_PRIORITY
    runtime = _runtime(channel)
    runtime["account_profile"] = {**as_object(runtime.get("account_profile")), "requested_at": now_shanghai().isoformat()}
    _save_runtime(channel, runtime)
    cutoff = now_shanghai() - timedelta(days=1)
    db.execute(delete(TaskJob).where(TaskJob.job_type == JOB_TYPE, TaskJob.priority == ACCOUNT_STATUS_PRIORITY, TaskJob.status == "completed", TaskJob.finished_at < cutoff).execution_options(synchronize_session=False))


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
        # 待确认账号只允许读取基础资料，业务命令需在用户确认后才下发。
        if channel.is_enabled is False and text(as_object(data.get("command")).get("type")) not in {"account_status", "device_status"}:
            continue
        if cancel_disallowed_plugin_outbound_job(db, channel=channel, job=job, payload=data):
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
            if command["type"] == "room_members":
                from app.services.wechat_webridge import member_directory

                room = member_directory.find_room(db, channel, text(command["params"].get("room_id")))
                data["directory_revision"] = member_directory.revision(room)
                job.payload_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
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


# 这些命令的回报是业务结果(领到多少、已被领完、已过期、是否确认到账),不是任务故障。
# 值是 (参数里的业务 ID 键名, 判断"确认成功"的方式):红包看 ok;转账必须 confirmed(文档:ok 与 accepted 都不算到账)。
PAYMENT_COMMANDS = {"receive_red_packet": "red_packet_id", "red_packet_detail": "red_packet_id", "receive_transfer": "transfer_id"}


def _schedule_red_packet_detail(channel: Channel, red_packet_id: str, *, attempt: int) -> None:
    """安排一次红包详情查询(到点入队)。延迟导入:红包模块依赖本模块,不能在模块加载时互相导入。"""

    from app.services.group_features import red_packet

    red_packet.schedule_detail(int(channel.tenant_id), int(channel.id), red_packet_id, attempt)


def _complete_payment_command(db: Session, channel: Channel, job: TaskJob, data: dict[str, Any], payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """领取红包、确认收款的回报:一律记为完成并写日志,按成败区分日志级别。

    "来晚了,已被领完"是抢红包的常态;如果每次都记成失败任务,管理后台的失败任务数和告警会被刷满。
    手机的失败回报里仍带着业务结果(协议文档:失败时 HTTP 400 但响应体含业务结果)。
    转账:只有 confirmed=true(callback_ok)才算确认收到;"confirm_invoked"/"callback_unknown"是结果未知,
    文档要求不要直接重试、应查询 get_transfer_status,所以这里只记警告,由人工核对。
    """

    command = as_object(data.get("command"))
    command_type = text(command.get("type"))
    params = as_object(command.get("params"))
    inner = as_object(result.get("result")) or result
    if command_type == "receive_transfer":
        succeeded = bool(inner.get("confirmed"))
    else:
        succeeded = bool(payload.get("ok")) or bool(result.get("ok"))
    # 回填收款记录(状态、金额、手机的原始回报);功能上线前入队的命令没有对应记录,找不到时什么也不做。
    ok = bool(payload.get("ok")) or bool(result.get("ok"))
    if command_type == "receive_transfer":
        payment_ledger.settle(db, channel_id=int(channel.id), kind=payment_ledger.KIND_TRANSFER, external_id=text(params.get("transfer_id") or params.get("message_id")), ok=ok, result=result)
    elif command_type == "receive_red_packet":
        red_packet_id = text(params.get("red_packet_id"))
        row = payment_ledger.settle(db, channel_id=int(channel.id), kind=payment_ledger.KIND_RED_PACKET, external_id=red_packet_id, ok=ok, result=result)
        if row is not None and row.status == payment_ledger.STATUS_PENDING:
            # 领取回报只说明"已打开",没有金额:稍后查红包详情拿到我领到的金额。
            _schedule_red_packet_detail(channel, red_packet_id, attempt=1)
    else:
        red_packet_id = text(params.get("red_packet_id"))
        request_id = text(command.get("request_id"))
        attempt = int(request_id.rsplit("_", 1)[-1]) if request_id.rsplit("_", 1)[-1].isdigit() else 1
        from app.services.group_features import red_packet

        outcome = payment_ledger.apply_red_packet_detail(
            db, channel_id=int(channel.id), external_id=red_packet_id, ok=ok, result=result, attempt=attempt, max_attempts=red_packet.MAX_DETAIL_ATTEMPTS
        )
        if outcome == "retry":
            _schedule_red_packet_detail(channel, red_packet_id, attempt=attempt + 1)
    log = logger.info if succeeded else logger.warning
    log(
        "payment command result",
        extra={
            "channel_id": channel.id, "job_id": job.id, "command": command_type, "target_id": text(params.get(PAYMENT_COMMANDS[command_type])),
            "succeeded": succeeded, "status": text(inner.get("status")), "result": json.dumps(result, ensure_ascii=False)[:300],
        },
    )
    complete_job(db, job, result={"webridge": {**_compact_result_for_storage(payload, {}), "succeeded": succeeded}})
    return {"ok": True, "job_id": int(job.id), "status": "completed"}


def _complete_moments_command(db: Session, channel: Channel, job: TaskJob, data: dict[str, Any], payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """朋友圈取回的回报:交给 moments 模块入库/安排下一次查询,任务一律记为完成。

    朋友圈内容是好友的隐私,任务结果里只留状态和数量,不保存动态内容(内容在 moments_posts 里)。
    """

    from app.services.wechat_webridge import moments

    ok = bool(payload.get("ok")) or bool(result.get("ok"))
    moments.handle_result(db, channel, as_object(data.get("command")), ok, result)
    inner = as_object(result.get("result")) or result
    items = inner.get("items")
    summary = {"ok": ok, "status": text(inner.get("status")), "count": len(items) if isinstance(items, list) else 0}
    logger.info("moments command result", extra={"channel_id": channel.id, "job_id": job.id, **summary})
    complete_job(db, job, result={"webridge": summary})
    return {"ok": True, "job_id": int(job.id), "status": "completed"}


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
        if text(as_object(data.get("command")).get("type")) in PAYMENT_COMMANDS:
            return _complete_payment_command(db, channel, job, data, payload, result)
        if text(as_object(data.get("command")).get("type")) in {"moments_list", "moments_operation"}:
            return _complete_moments_command(db, channel, job, data, payload, result)
        if bool(payload.get("ok")) or bool(result.get("ok")):
            command = as_object(data.get("command"))
            command_type = text(command.get("type") or command.get("command_name"))
            from app.services.wechat_webridge import group_sync

            # 群列表同步:先记下已有的群,结果写入后对"新发现的群"继续下发成员同步。
            known_before = group_sync.known_room_ids(db, channel) if command_type == "groups_all" else None
            counts = persist_directory_result(
                db, channel, command, result, snapshot_revision=data.get("directory_revision"),
                snapshot_started_at=job.started_at or job.created_at,
            )
            if known_before is not None:
                group_sync.after_groups_result(db, channel, known_before, group_sync.complete_snapshot_ids(command, result))
            if command_type == "device_status":
                _save_device_status(db, channel, as_object(result.get("result")) or result)
            elif command_type == "account_status":
                _save_account_profile(db, channel, as_object(result.get("result")) or result)
            elif command_type == "room_members":
                from app.services.wechat_webridge import member_directory

                job.status = "completed"
                db.flush()
                member_directory.after_result(
                    db, channel, text(as_object(command.get("params")).get("room_id")),
                    complete=bool(counts.get("members_complete")), stale=bool(counts.get("stale_snapshot")),
                )
            settle_outbound_message(db, job, ok=True)
            complete_job(db, job, result={"webridge": _compact_result_for_storage(payload, counts)})
            logger.info("webridge command completed", extra={"channel_id": channel.id, "job_id": job.id, "request_id": request_id})
            return {"ok": True, "job_id": int(job.id), "status": "completed"}
        failed_command = as_object(data.get("command"))
        if failed_command.get("type") == "room_members":
            from app.services.wechat_webridge import member_directory

            job.status = "failed"
            db.flush()
            member_directory.after_result(db, channel, text(as_object(failed_command.get("params")).get("room_id")), complete=False)
        settle_outbound_message(db, job, ok=False, error=text(result.get("error") or payload.get("error") or "webridge_command_failed"))
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


def _save_account_profile(db: Session, channel: Channel, data: dict[str, Any]) -> None:
    """保存 account_status 回报的昵称头像,并把仍是 wxid 的账号名、机器人名换成昵称。

    名字只在“还是内部 ID”或“等于上次自动写入的昵称”时才替换:用户自己改过的名字不动,
    而账号后来改了昵称时,自动写入的名字会跟着更新。
    """

    nickname = text(data.get("nick_name")) or text(data.get("display_name"))
    if not nickname:
        return
    nickname = nickname[:100]
    account_id = text(data.get("account_id") or data.get("user_name"))
    runtime = _runtime(channel)
    previous = as_object(runtime.get("account_profile"))
    previous_nickname = text(previous.get("nickname"))
    runtime["account_profile"] = {
        "nickname": nickname,
        "avatar_url": text(data.get("avatar_url") or data.get("head_img_url")),
        "account_id": account_id,
        "fetched_at": now_shanghai().isoformat(),
        "requested_at": previous.get("requested_at"),
    }

    def replaceable(current: str | None) -> bool:
        value = text(current)
        return not value or looks_like_raw_id(value) or value == account_id or (bool(previous_nickname) and value == previous_nickname)

    if replaceable(channel.name):
        channel.name = nickname
    connection = runtime.get("account_connection")
    if isinstance(connection, dict) and replaceable(connection.get("display_name")):
        connection["display_name"] = nickname
    bot = db.get(Bot, int(channel.bot_id)) if channel.bot_id else None
    if bot is not None and replaceable(bot.name):
        # 机器人名在租户内唯一;已被别的机器人占用时保持原名,不抢占。
        taken = db.scalar(select(Bot.id).where(Bot.name == nickname, Bot.id != bot.id).limit(1))
        if taken is None:
            bot.name = nickname
    _save_runtime(channel, runtime)


def mark_event_seen(channel: Channel, payload: dict[str, Any]) -> str:
    """提取事件 ID；没有 ID 时用稳定正文摘要，避免同一轮重试重复落库。"""

    event_id = text(payload.get("event_id") or payload.get("message_id") or payload.get("id"))
    if event_id:
        return event_id
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "hash_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
