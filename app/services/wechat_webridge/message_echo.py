"""把手机的本账号回显关联到原出站记录，避免网页发送后出现两张消息卡片。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message, MessageAttachment, RawWebhookEvent, TaskJob, WorkWechatContact
from app.services.message_actions import object_json
from app.services.message_view import MAX_XML_CHARS, strip_sender


def _xml(content: str) -> ElementTree.Element | None:
    if len(content) > MAX_XML_CHARS:
        return None
    try:
        return ElementTree.fromstring(strip_sender(content))
    except (ElementTree.ParseError, ValueError):
        return None


def _mention_names(db: Session, message: Message, mention_ids: list) -> dict[str, set[str]]:
    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or not mention_ids:
        return {}
    names = {member: set() for member in mention_ids if member != "notify@all"}

    def add(member, *values):
        if member in names:
            names[member].update(str(value).strip() for value in values if value and str(value).strip())

    for member in db.scalars(select(ChatroomMember).join(Chatroom).where(
        Chatroom.tenant_id == conversation.tenant_id, Chatroom.bot_id == conversation.bot_id,
        Chatroom.channel_id == conversation.channel_id, Chatroom.external_id == conversation.external_id,
        ChatroomMember.tenant_id == conversation.tenant_id, ChatroomMember.bot_id == conversation.bot_id,
        ChatroomMember.channel_id == conversation.channel_id, ChatroomMember.member_external_id.in_(names),
    )).all():
        add(member.member_external_id, member.nickname, member.display_name)
    for contact in db.scalars(select(WorkWechatContact).where(
        WorkWechatContact.tenant_id == conversation.tenant_id, WorkWechatContact.channel_id == conversation.channel_id,
        WorkWechatContact.contact_external_id.in_(names), WorkWechatContact.deleted_at.is_(None),
    )).all():
        add(contact.contact_external_id, contact.display_name, contact.name)
    for member in object_json(message.raw_request_json).get("mentions") or []:
        if isinstance(member, dict):
            add(member.get("id"), member.get("name"))
    # 手机优先用好友备注/昵称，取不到才显示账号；保留无成员目录时的旧版分隔格式匹配。
    return {member: values | {member} for member, values in names.items() if values}


def _matches_text(expected: str, content: str, mention_ids: list, name_variants: dict[str, set[str]] | None = None) -> bool:
    if content == expected:
        return True
    if not mention_ids:
        return False
    # 内联占位符只允许昵称部分变化；其余正文和插入位置必须完全相同。
    token = r"\{\{mention:([^}]+)\}\}"
    slots = list(re.finditer(token, expected))
    nickname = r"@(?:[^\r\n@\u2005]+\u2005|[^\s@]+[ \t])"

    def rendered(member, *, inline=False, last=False):
        variants = (name_variants or {}).get(member)
        if variants:
            # 内联 @ 不保证分隔符，昵称也可含 @；只枚举真实名字，不能用通配符吞掉正文。
            return "@(?:" + "|".join(re.escape(value) for value in sorted(variants)) + ")" + ("" if inline else r"[ \t\u2005]")
        legacy = r"@(?:[^\r\n@\u2005]+\u2005?|[^\s@]+[ \t]?)" if last else nickname
        return "(?:@" + re.escape(member) + ("" if inline else r"[ \t\u2005]") + "|" + legacy + ")"

    if slots:
        pattern, offset = "", 0
        for slot in slots:
            if slot[1] not in mention_ids or slot[1] == "notify@all":
                return False
            pattern += re.escape(expected[offset:slot.start()])
            pattern += rendered(slot[1], inline=True, last=slot.end() == len(expected))
            offset = slot.end()
        pattern += re.escape(expected[offset:])
        # 插件遇到内联占位符时，名单里其余成员只加入通知名单，不再添加可见前缀。
        return re.fullmatch(pattern, content) is not None
    prefixes = ""
    for member in mention_ids:
        if member == "notify@all":
            continue
        prefixes += rendered(member)
    return bool(expected) and re.fullmatch(prefixes + re.escape(expected), content) is not None


def _matches_content(db: Session, message: Message, params: dict, payload: dict, details: dict) -> bool:
    kind = str(payload.get("message_type") or "text")
    content = str(payload.get("content") or "")
    if message.message_type == "text":
        mention_ids = params.get("mention_user_list") or []
        names = _mention_names(db, message, mention_ids) if mention_ids else {}
        if params.get("quote_message_id"):
            root = _xml(content)
            if kind != "quote" or root is None:
                return False
            quoted_id = str(details.get("quote_message_id") or root.findtext(".//refermsg/svrid") or "")
            if not _matches_text(str(params.get("content") or message.content), root.findtext(".//appmsg/title") or "", mention_ids, names):
                return False
            if quoted_id == str(params["quote_message_id"]):
                return True
            # 手机发送按本地编号引用，回显 XML 有时只有服务器编号；用唯一的原文和发送者核对。
            referenced_content = root.findtext(".//refermsg/content")
            referenced_sender = root.findtext(".//refermsg/fromusr")
            if not referenced_content or not referenced_sender:
                return False
            # 手机引用会保留 @ 昵称等渲染结果，先定位目标，再核对本地正文或原始回显正文。
            targets = db.scalars(select(Message).where(
                Message.conversation_id == message.conversation_id, Message.message_type == "text",
                Message.external_message_id == str(params["quote_message_id"]),
                Message.sender_id == referenced_sender,
            )).all()
            target = None
            for candidate in targets:
                if candidate.content == referenced_content:
                    target = candidate
                    break
                echo = object_json(candidate.raw_request_json).get("echo")
                if not isinstance(echo, dict) or not echo.get("event_key"):
                    continue
                conversation = db.get(Conversation, candidate.conversation_id)
                event = db.scalar(select(RawWebhookEvent).where(
                    RawWebhookEvent.channel_id == conversation.channel_id, RawWebhookEvent.event_key == str(echo["event_key"]),
                ))
                event_data = object_json(event.payload_json) if event is not None else {}
                body = event_data.get("body")
                if not isinstance(body, dict):
                    continue
                # 仅使用已关联、同会话同发送者同消息编号的原始事件，不能用其他同文消息兜底。
                if (str(body.get("message_id") or "") == candidate.external_message_id
                    and str(body.get("chat_id") or body.get("room_id") or "") == conversation.external_id
                    and str(body.get("sender_id") or "") == referenced_sender
                    and body.get("content") == referenced_content):
                    target = candidate
                    break
            if target is None:
                return False
            ambiguous = db.scalar(select(Message.id).where(
                Message.conversation_id == target.conversation_id, Message.status != "echo", Message.external_message_id != target.external_message_id,
                Message.sender_id == target.sender_id, Message.content == target.content, Message.message_type == "text",
            ).limit(1))
            return ambiguous is None
        if kind != "text":
            return False
        expected = str(params.get("content") or message.content)
        if mention_ids:
            incoming = details.get("mention_user_list") or []
            return set(incoming) == set(mention_ids) and _matches_text(expected, content, mention_ids, names)
        return content == expected
    if message.message_type != kind:
        return False
    attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == message.id).order_by(MessageAttachment.id.desc()))
    if kind == "file":
        root = _xml(content)
        if root is None or attachment is None:
            return False
        metadata = object_json(attachment.metadata_json)
        try:
            size = int(root.findtext(".//appattach/totallen") or 0)
        except ValueError:
            return False
        if not size or size != attachment.file_size or root.findtext(".//appmsg/title") != metadata.get("file_name"):
            return False
        incoming_md5 = root.findtext(".//md5") or root.findtext(".//attachmd5") or root.findtext(".//filemd5")
        if incoming_md5:
            if not attachment.storage_path:
                return False
            try:
                digest = hashlib.md5()
                with Path(attachment.storage_path).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                return digest.hexdigest() == incoming_md5.lower()
            except OSError:
                return False
        return True
    node = {"image": "img", "voice": "voicemsg", "video": "videomsg", "emoji": "emoji"}.get(kind)
    root = _xml(content)
    return bool(node) and root is not None and root.find(".//" + node) is not None


def reconcile_echo(
    db: Session, channel: Channel, conversation: Conversation, normalized: dict, *, received_at: datetime | None = None,
) -> Message | None:
    """优先用微信编号复用记录；旧手机无发送编号时，按实际执行窗口一对一关联。"""

    details = normalized.get("details") or {}
    payload = normalized.get("message") or {}
    if channel.channel_type != "wechat_webridge" or conversation.channel_id != channel.id or not details.get("from_self"):
        return None
    external_id = str(payload.get("external_message_id") or "").strip()
    if not external_id:
        return None
    existing = db.scalar(select(Message).where(
        Message.conversation_id == conversation.id, Message.direction == "out", Message.external_message_id == external_id, Message.status != "echo",
    ).with_for_update())
    if existing is not None:
        return existing
    current = ensure_shanghai_datetime(received_at or now_shanghai())
    candidates = db.scalars(select(Message).where(
        Message.conversation_id == conversation.id, Message.direction == "out",
        Message.external_message_id.is_(None), Message.status.in_(("sent", "queued")),
        Message.created_at >= current - timedelta(minutes=30), Message.created_at <= current,
    ).order_by(Message.id.asc()).with_for_update()).all()
    matched = []
    for message in candidates:
        raw = object_json(message.raw_request_json)
        job_id = raw.get("job_id")
        if not isinstance(job_id, int):
            continue
        job = db.get(TaskJob, job_id)
        if job is None or job.status not in {"running", "completed"} or job.started_at is None:
            continue
        job_data = object_json(job.payload_json)
        command = job_data.get("command") or {}
        params = command.get("params") or {}
        if job_data.get("channel_id") != channel.id or job_data.get("conversation_id") != conversation.id or params.get("receiver") != conversation.external_id:
            continue
        if raw.get("request_id") != command.get("request_id") or not raw.get("request_id"):
            continue
        start = ensure_shanghai_datetime(job.started_at)
        end = ensure_shanghai_datetime(job.finished_at) if job.finished_at else current
        if not start <= current <= end + timedelta(seconds=5) or current - start > timedelta(seconds=180):
            continue
        if payload.get("message_type") in {"image", "voice", "video", "emoji"} and job.status == "running" and current - start > timedelta(seconds=5):
            continue
        # 历史积压事件不能误占新任务；手机时间精确到秒，且与服务器有少量时钟偏差。
        created = details.get("create_time")
        if isinstance(created, (int, float)) and created > 0:
            try:
                event_time = datetime.fromtimestamp(created / 1000 if created > 10**11 else created, tz=current.tzinfo)
            except (OverflowError, OSError, ValueError):
                continue
            if not start - timedelta(seconds=5) <= event_time <= end + timedelta(seconds=5):
                continue
        if _matches_content(db, message, params, payload, details):
            matched.append(message)
    if not matched:
        return None
    # 图片、语音可能被微信转码，无法比原文件哈希；有多个并发候选时保留回显，不猜测归属。
    if payload.get("message_type") in {"image", "voice", "video", "emoji"} and len(matched) != 1:
        return None
    message = matched[0]
    raw = object_json(message.raw_request_json)
    raw["echo"] = {"event_key": normalized.get("event_key"), "message_id": external_id}
    message.raw_request_json = json.dumps(raw, ensure_ascii=False)
    message.external_message_id = external_id
    message.sender_id = str(payload.get("sender_id") or message.sender_id or "")
    message.sender_name = payload.get("sender_name") or message.sender_name
    return message
