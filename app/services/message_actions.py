"""消息中心的发送能力与撤回校验，复用现有渠道适配器和命令队列。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Conversation, Message, TaskJob


MEDIA_TYPES = {"image", "voice", "video", "emoji", "file"}
MEDIA_LABELS = {"image": "图片", "voice": "语音", "video": "视频", "emoji": "表情", "file": "文件"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
REVOKE_SECONDS = 120


def object_json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def is_own_message(message: Message, channel: Channel | None) -> bool:
    if message.direction == "out":
        return True
    if channel is None or not message.sender_id:
        return False
    # 微桥会把本账号手机上发出的消息也作为入站事件回传，历史数据按真实账号补判。
    from app.services.account_identity import account_identity

    own_id = account_identity(channel)["external_id"]
    if not own_id and channel.channel_type == "wechat_ipad":
        own_id = str(object_json(channel.adapter_config_json).get("wxid") or "").strip()
    if not own_id and channel.provider_account_id and channel._sa_instance_state.session is not None:
        account = channel.provider_account
        own_id = str(account.external_user_id or "").strip() if account is not None else ""
    return bool(own_id) and str(message.sender_id).strip() == own_id


def capabilities(channel: Channel | None, conversation: Conversation) -> dict[str, bool]:
    result = dict.fromkeys((*sorted(MEDIA_TYPES), "quote", "mention", "revoke"), False)
    if channel is None or channel.deleted_at is not None:
        return result
    kind = channel.channel_type
    if kind == "wechat_webridge":
        reported = object_json(channel.runtime_info_json).get("webridge", {})
        reported = reported.get("capabilities") if isinstance(reported, dict) else None
        commands = set(reported) if isinstance(reported, list) else set()
        names = {"image": "send_image", "voice": "send_voice", "video": "send_video_from_url", "emoji": "send_emoji", "file": "send_file_from_url", "quote": "send_message", "mention": "send_message", "revoke": "revoke_message"}
        result.update({name: not commands or command in commands for name, command in names.items()})
    elif kind in {"workwechat", "wechat_winauto"}:
        result.update(image=True, file=True)
    elif kind == "wechat_ipad":
        # 普通文件/视频上传没有协议需要的 CDN XML/封面，不能宣称可直接发送。
        result.update(image=True, voice=True, mention=True)
    elif kind == "telegram":
        result["image"] = True
    result["mention"] = result["mention"] and conversation.chat_type == "group"
    return result


def revoke_info(message: Message, channel: Channel | None) -> dict[str, Any]:
    saved = object_json(message.raw_request_json).get("revoke", {})
    saved = saved if isinstance(saved, dict) else {}
    state = "revoked" if message.status == "revoked" else str(saved.get("state") or "")
    until = 0
    if is_own_message(message, channel) and message.external_message_id and message.created_at and message.status not in {"failed", "revoked"} and message.external_status != "pending":
        until = int((ensure_shanghai_datetime(message.created_at) + timedelta(seconds=REVOKE_SECONDS)).timestamp() * 1000)
    return {"revoke_until": until, "revoke_state": state, "revoke_error": str(saved.get("error") or "")}


def request_revoke(db: Session, channel: Channel, conversation: Conversation, message: Message) -> dict[str, Any]:
    # 先锁住这条消息再判断"是否已有待执行的撤回":并发双击时第二个请求会等到第一个提交后,
    # 读到 pending 状态直接返回,不会再入队第二条撤回命令。项目会话关闭了 autoflush,
    # 刷新前必须先 flush,否则会用数据库旧值冲掉本事务里还没写入的修改。
    db.flush()
    db.refresh(message, with_for_update=True)
    if message.conversation_id != conversation.id:
        raise ValueError("消息不属于当前会话")
    if not is_own_message(message, channel):
        raise ValueError("只能撤回当前机器人账号自己发送的消息")
    if not capabilities(channel, conversation)["revoke"]:
        raise ValueError("当前渠道不支持撤回消息")
    if message.status == "revoked":
        raise ValueError("这条消息已经撤回")
    if not message.created_at or (now_shanghai() - ensure_shanghai_datetime(message.created_at)).total_seconds() >= REVOKE_SECONDS:
        raise ValueError("消息已超过 2 分钟，无法撤回")
    if message.status == "failed" or message.external_status == "pending" or not message.external_message_id:
        raise ValueError("消息尚未确认发送或缺少渠道消息编号，暂时无法撤回")
    raw = object_json(message.raw_request_json)
    if isinstance(raw.get("revoke"), dict) and raw["revoke"].get("state") == "pending":
        return {"ok": True, "notice": "撤回请求已提交，等待手机执行"}
    from app.services.wechat_webridge.service import enqueue_message_command

    job = enqueue_message_command(db, channel, conversation, {"message_type": "revoke", "message_id": message.external_message_id})
    payload = object_json(job.payload_json)
    payload["revoke_message_id"] = int(message.id)
    payload["revoke_expires_at"] = revoke_info(message, channel)["revoke_until"]
    job.payload_json = json.dumps(payload, ensure_ascii=False)
    raw["revoke"] = {"state": "pending", "job_id": int(job.id)}
    message.raw_request_json = json.dumps(raw, ensure_ascii=False)
    return {"ok": True, "notice": "撤回请求已提交，等待手机执行"}


def settle_revoke(db: Session, job: TaskJob, *, ok: bool, error: str = "") -> bool:
    """手机确认后才隐藏原消息；失败时保留原文并允许期限内重试。"""

    payload = object_json(job.payload_json)
    target_id = payload.get("revoke_message_id")
    if not target_id:
        return False
    message = db.get(Message, int(target_id))
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    if message is None or conversation is None or message.conversation_id != conversation.id or conversation.channel_id != int(payload.get("channel_id") or 0):
        return True
    raw = object_json(message.raw_request_json)
    saved = raw.get("revoke")
    if not isinstance(saved, dict) or saved.get("job_id") != int(job.id) or saved.get("state") != "pending":
        return True
    raw["revoke"] = {**saved, "state": "revoked" if ok else "failed", "error": error}
    message.raw_request_json = json.dumps(raw, ensure_ascii=False)
    if ok:
        message.status = message.external_status = "revoked"
    return True


def apply_recall_event(db: Session, channel: Channel, conversation: Conversation, recalled: dict[str, Any], *, raw_event_id: int) -> list[Message]:
    """只凭同渠道、同会话的明确微信消息编号更新原卡片，不按正文或时间猜测。"""

    if conversation.channel_id != channel.id or conversation.tenant_id != channel.tenant_id:
        return []
    for external_id in recalled.get("message_ids") or []:
        matches = list(db.scalars(select(Message).where(
            Message.conversation_id == conversation.id,
            Message.tenant_id == channel.tenant_id,
            Message.external_message_id == str(external_id),
            Message.status != "echo", Message.message_type != "system",
        ).with_for_update()).all())
        if not matches:
            continue
        for message in matches:
            raw = object_json(message.raw_request_json)
            previous = raw.get("revoke") if isinstance(raw.get("revoke"), dict) else {}
            raw["revoke"] = {**previous, "state": "revoked", "error": "", "raw_event_id": raw_event_id}
            message.raw_request_json = json.dumps(raw, ensure_ascii=False)
            message.status = message.external_status = "revoked"
        return matches
    return []
