"""消息中心的发送能力与撤回校验，复用现有渠道适配器和命令队列。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Conversation, Message, RawWebhookEvent, TaskJob


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
    elif kind == "wechat_gugatool":
        # 手机注册时上报实际支持的命令；老版本没有清单时按适配器已实现的发送能力放行。
        reported = object_json(channel.runtime_info_json).get("guga_agent", {})
        reported = reported.get("capabilities") if isinstance(reported, dict) else None
        commands = set(reported) if isinstance(reported, list) else set()
        names = {
            "image": "send_image",
            "voice": "send_voice",
            "video": "send_video",
            "emoji": "send_emoji",
            "file": "send_file",
            "quote": "send_quote",
            "mention": "send_text",
            "revoke": "revoke_message",
        }
        result.update({name: not commands or command in commands for name, command in names.items()})
    elif kind == "telegram":
        result["image"] = True
    result["mention"] = result["mention"] and conversation.chat_type == "group"
    return result


def guga_quote_ids(db: Session, channel_id: int, external_message_id: str, *, raw_request_json: str | None = None) -> dict[str, int]:
    """咕嘎手机端的引用接口要本地/服务端消息 ID。

    入站时已把这两个编号存进消息快照;老消息或历史补拉没有快照时,
    再按渠道消息编号回查原始事件。
    """

    event_id = str(external_message_id or "").strip()
    if not event_id:
        return {}
    snapshot = object_json(raw_request_json)
    native = snapshot.get("native_ids") if isinstance(snapshot.get("native_ids"), dict) else {}
    ids: dict[str, int] = {}
    for source, target in (("local", "quote_local_id"), ("server", "quote_msg_svr_id")):
        value = native.get(source)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            ids[target] = number
    if ids:
        return ids
    event = db.scalar(
        select(RawWebhookEvent)
        .where(RawWebhookEvent.channel_id == int(channel_id), RawWebhookEvent.event_key == event_id)
        .order_by(RawWebhookEvent.id.desc())
    )
    payload = object_json(event.payload_json) if event is not None else {}
    for source, target in (("localMessageID", "quote_local_id"), ("serverMessageID", "quote_msg_svr_id")):
        value = payload.get(source)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            ids[target] = number
    return ids


def guga_quote_ids_from_job(db: Session, channel: Channel, message: Message) -> dict[str, int]:
    """出站消息没有事件快照时,从手机命令回执里找本地/服务端编号。

    咕嘎手机的回执形如 ``{"Data": {"LocalId": 33300, "MsgSvrId": 0}}``;
    老消息加这层兜底,不用等重新发送也能被引用。
    """

    request_id = str(object_json(message.raw_request_json).get("request_id") or "").strip()
    if not request_id:
        return {}
    from app.services.guga_agent.contract import JOB_TYPE, message_ids

    job = db.scalar(
        select(TaskJob).where(
            TaskJob.job_type == JOB_TYPE,
            TaskJob.dedupe_key == f"guga:{int(channel.id)}:{request_id}",
        )
    )
    if job is None or not job.result_json:
        return {}
    try:
        result = json.loads(job.result_json)
    except (TypeError, ValueError):
        return {}
    native = message_ids(result)
    ids: dict[str, int] = {}
    if native.get("local"):
        ids["quote_local_id"] = int(native["local"])
    if native.get("server"):
        ids["quote_msg_svr_id"] = int(native["server"])
    return ids


def guga_message_ids(db: Session, channel: Channel, message: Message) -> dict[str, int]:
    """咕嘎发送引用/撤回都要本地或服务端编号:优先消息快照,其次命令回执。"""

    ids = guga_quote_ids(
        db,
        int(channel.id),
        str(message.external_message_id or "").strip(),
        raw_request_json=message.raw_request_json,
    )
    if ids:
        return ids
    return guga_quote_ids_from_job(db, channel, message)


def quote_payload_for(db: Session, channel: Channel, message: Message) -> dict[str, Any]:
    """把消息中心的引用目标翻译成当前渠道发送接口认识的字段。

    微信协议按渠道分两种:微桥直接收被引用消息的渠道编号;咕嘎手机只收
    本地/服务端编号,拿不到就明确报错,不能把事件 UUID 当引用编号发出去。
    """

    if message.status in {"failed", "revoked"} or message.external_status == "pending":
        raise ValueError("这条消息尚未确认发送、已撤回或缺少渠道消息编号，无法引用")
    external_id = str(message.external_message_id or "").strip()
    if channel.channel_type == "wechat_gugatool":
        ids = guga_message_ids(db, channel, message)
        if not ids:
            raise ValueError("这条消息缺少微信本地/服务端消息编号，无法引用")
        # 出站消息可能只有本地编号;引用接口接受 local_id,不需要事件 UUID。
        quote_id = external_id or str(ids.get("quote_msg_svr_id") or ids.get("quote_local_id"))
        return {"quote_message_id": quote_id, **ids}
    if not external_id:
        raise ValueError("这条消息尚未确认发送、已撤回或缺少渠道消息编号，无法引用")
    return {"quote_message_id": external_id}


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
    if message.status == "failed" or message.external_status == "pending":
        raise ValueError("消息尚未确认发送或缺少渠道消息编号，暂时无法撤回")
    raw = object_json(message.raw_request_json)
    if isinstance(raw.get("revoke"), dict) and raw["revoke"].get("state") == "pending":
        return {"ok": True, "notice": "撤回请求已提交，等待手机执行"}
    if channel.channel_type == "wechat_gugatool":
        ids = guga_message_ids(db, channel, message)
        if not ids:
            raise ValueError("这条消息缺少微信本地/服务端消息编号，暂时无法撤回")
        from app.services.guga_agent.service import enqueue_command

        # 手机端 /api/v1/Msg/Revoke 只认 ToWxid + LocalId/MsgSvrId,字段名不能改成 snake_case。
        params: dict[str, Any] = {"ToWxid": str(conversation.external_id or "").strip()}
        if ids.get("quote_local_id"):
            params["LocalId"] = int(ids["quote_local_id"])
        if ids.get("quote_msg_svr_id"):
            params["MsgSvrId"] = int(ids["quote_msg_svr_id"])
        job = enqueue_command(
            db,
            channel,
            command_name="revoke_message",
            params=params,
            priority=95,
            conversation_id=int(conversation.id),
            purpose="revoke",
        )
    else:
        if not message.external_message_id:
            raise ValueError("消息尚未确认发送或缺少渠道消息编号，暂时无法撤回")
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


def apply_gugatool_recall_event(
    db: Session,
    channel: Channel,
    conversation: Conversation,
    recalled: dict[str, Any],
    *,
    raw_event_id: int,
) -> list[Message]:
    """只按同账号会话的明确微信编号撤回，缺失或歧义证据必须留作系统通知。"""

    if conversation.channel_id != channel.id or conversation.tenant_id != channel.tenant_id:
        return []
    # 服务端编号优先，本地编号不能冒充服务端编号。通知不带编号时即使正文唯一也不猜。
    server_ids = {str(int(value)) for value in recalled.get("server_message_ids", [])
        if str(value).isdigit() and int(value) > 0}
    local_ids = {str(int(value)) for value in recalled.get("local_message_ids", [])
        if str(value).isdigit() and int(value) > 0}
    identifiers = server_ids or local_ids
    if not identifiers or len(server_ids) > 1 or len(local_ids) > 1:
        return []
    matches: list[Message] = []
    # 先在已索引的会话范围内按编号文本收窄，再解析快照核对完整整数。
    # 不在数据库中转数值，避免 SQLite/MySQL 对超大微信编号的浮点转换丢失精度。
    candidates = db.scalars(select(Message).where(
        Message.conversation_id == conversation.id,
        Message.tenant_id == channel.tenant_id,
        Message.status != "echo", Message.message_type != "system",
        or_(Message.external_message_id.in_(identifiers),
            *(Message.raw_request_json.contains(value) for value in identifiers)),
    ).with_for_update()).all()
    for message in candidates:
        native = guga_message_ids(db, channel, message)
        value = native.get("quote_msg_svr_id" if server_ids else "quote_local_id")
        if value is not None and str(value) in identifiers:
            matches.append(message)
    # 本地编号可能重复，异常快照也可能保存同一服务器编号；不能一次改动多条历史消息。
    if len(matches) != 1:
        return []
    for message in matches:
        raw = object_json(message.raw_request_json)
        previous = raw.get("revoke") if isinstance(raw.get("revoke"), dict) else {}
        raw["revoke"] = {**previous, "state": "revoked", "error": "", "raw_event_id": raw_event_id}
        message.raw_request_json = json.dumps(raw, ensure_ascii=False)
        message.status = message.external_status = "revoked"
    return matches
