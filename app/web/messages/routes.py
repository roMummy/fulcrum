"""消息会话、历史、附件和手动发送入口。"""

from itertools import pairwise
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlencode
import base64
import json
import logging
import re

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import format_conversation_time, now_shanghai
from app.models.entities import Channel, Chatroom, Conversation, Message, MessageAttachment
from app.services import message_actions, message_media, message_mentions, message_view
from app.services.channel_adapters import dispatch_outbound_message
from app.services.conversation_kinds import (
    KIND_LABELS,
    apply_kind_filter,
    conversation_kind,
    normalize_kind,
)
from app.services.crud import get_channel
from app.services.identity_directory import IdentityDirectory
from app.services.message_history import latest_history_messages, list_history_messages
from app.services.ops import record_audit
from app.services.outbound_files import (
    build_public_base_url,
    redact_base64_fields,
    sanitize_upload_filename,
    store_outbound_upload,
)
from app.web.messages import inbox
from app.web.auth_common import require_tenant_csrf
from app.services.wechat_ipad.files import delivery_name
from app.services.wechat_ipad import stickers
from app.services.wechat_ipad.videos import MAX_COVER_BYTES, has_mp4_header
from app.web.shared.access import enforce_role, get_session
from app.web.shared.operations import _csrf_from_header
from app.web.shared.presentation import format_beijing_time, render, templates
from app.web.shared.urls import safe_return_to


router = APIRouter()
logger = logging.getLogger(__name__)
SLOW_MESSAGE_PAGE_SECONDS = 1.0


# 消息中心单条发送内容的长度上限(微信文本消息本身有上限,这里取一个稳妥值)。
MANUAL_MESSAGE_MAX_CHARS = 2000


@router.post("/messages/send")
async def send_manual_message_action(
    request: Request, conversation_id: int = Form(...), content: str = Form(""), csrf_token: str = Form(""),
    message_type: str = Form("text"), file: UploadFile | None = File(None),
    video_cover: UploadFile | None = File(None), play_length: int = Form(0),
    reply_to_id: int = Form(0), mention_ids: str = Form("[]"),
):
    """消息中心发送框提交的消息;由页面脚本 fetch 调用,返回 JSON,页面不刷新。

    无论发送成败,经渠道适配器出站都会留下一条消息记录(失败的标记为 failed),前端据此把它拉进对话里。
    """

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        text_content = content.strip()
        if len(text_content) > MANUAL_MESSAGE_MAX_CHARS:
            return JSONResponse({"ok": False, "notice": f"消息太长了，最多 {MANUAL_MESSAGE_MAX_CHARS} 字"}, status_code=400)
        channel = get_channel(db, int(conversation.channel_id or 0)) if conversation.channel_id else None
        if channel is None:
            return JSONResponse({"ok": False, "notice": "会话未绑定渠道，无法发送"}, status_code=400)
        normalized_type = message_type.strip().lower()
        supported = message_actions.capabilities(channel, conversation)
        if normalized_type != "text" and (normalized_type not in message_actions.MEDIA_TYPES or not supported.get(normalized_type)):
            label = message_actions.MEDIA_LABELS.get(normalized_type, "此类型消息")
            return JSONResponse({"ok": False, "notice": f"当前渠道不支持发送{label}"}, status_code=400)
        if normalized_type == "text" and not text_content:
            return JSONResponse({"ok": False, "notice": "消息内容不能为空"}, status_code=400)
        payload: dict[str, Any] = {"message_type": normalized_type, "content": text_content}
        reply = None
        try:
            mentions = json.loads(mention_ids)
        except (TypeError, ValueError):
            mentions = None
        if not isinstance(mentions, list) or len(mentions) > 20 or any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in mentions):
            return JSONResponse({"ok": False, "notice": "@ 成员列表无效，最多选择 20 人"}, status_code=400)
        mentions = list(dict.fromkeys(value.strip() for value in mentions))
        selected_mentions = []
        placeholders = re.findall(r"\{\{mention:([^}]+)\}\}", text_content)
        if any(value not in mentions for value in placeholders):
            return JSONResponse({"ok": False, "notice": "@ 对象无效，请重新选择群成员"}, status_code=400)
        if reply_to_id:
            reply = db.get(Message, reply_to_id)
            if reply is None or reply.conversation_id != conversation.id:
                return JSONResponse({"ok": False, "notice": "引用的消息不属于当前会话"}, status_code=400)
            if not supported["quote"] or normalized_type != "text":
                return JSONResponse({"ok": False, "notice": "当前渠道不支持此类型的引用回复"}, status_code=400)
            try:
                payload.update(message_actions.quote_payload_for(db, channel, reply))
            except ValueError as exc:
                return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        if mentions:
            if channel.channel_type == "wechat_ipad" and reply is not None:
                return JSONResponse({"ok": False, "notice": "iPad 暂不支持同时引用并 @ 成员"}, status_code=400)
            if not supported["mention"] or normalized_type != "text":
                return JSONResponse({"ok": False, "notice": "当前渠道不支持此类型的 @ 发送"}, status_code=400)
            if channel.channel_type == "wechat_gugatool" and payload.get("quote_message_id"):
                # 手机 SendQuote 没有 @ 字段，两者同时提交会静默丢掉 @，直接拒绝并说明原因。
                return JSONResponse({"ok": False, "notice": "咕嘎助手暂不支持同时引用并 @ 成员"}, status_code=400)
            try:
                selected_mentions = message_mentions.validate_members(db, channel, conversation, mentions)
            except ValueError as exc:
                return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
            labels = {person["id"]: person["name"] for person in selected_mentions}
            visible_content = re.sub(r"\{\{mention:([^}]+)\}\}", lambda match: "@" + labels[match[1]] + "\u2005", text_content)
            missing = [person for person in selected_mentions if person["id"] not in placeholders]
            visible_content = "".join("@" + person["name"] + "\u2005" for person in missing) + visible_content
            if len(visible_content) > MANUAL_MESSAGE_MAX_CHARS:
                return JSONResponse({"ok": False, "notice": f"消息太长了，最多 {MANUAL_MESSAGE_MAX_CHARS} 字"}, status_code=400)
            # 展示正文从首次入库就保留 @，手机协议仍使用能在原位置替换昵称的占位符。
            payload["display_content"] = visible_content
            if channel.channel_type in {"wechat_ipad", "wechat_gugatool"}:
                # 这两类渠道的发送接口用 At 字段 + 正文里的 @昵称，正文不能保留占位符。
                payload["at"] = ",".join(mentions)
                payload["content"] = visible_content
            else:
                payload["mention_user_list"] = mentions
                payload["content"] = text_content.replace("{{mention:notify@all}}", "@所有人\u2005")
                if "notify@all" in mentions and "notify@all" not in placeholders:
                    payload["content"] = "@所有人\u2005" + payload["content"]
        stored = None
        if normalized_type in message_actions.MEDIA_TYPES:
            if file is None or not file.filename:
                return JSONResponse({"ok": False, "notice": "请先选择要发送的文件"}, status_code=400)
            if channel.channel_type == "wechat_ipad" and normalized_type in {"file", "video"}:
                try:
                    delivery_name(sanitize_upload_filename(file.filename))
                except ValueError as exc:
                    return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
            if channel.channel_type == "wechat_ipad" and normalized_type == "voice" and Path(file.filename).suffix.lower() not in {".silk", ".slik", ".amr", ".wav", ".mp3"}:
                return JSONResponse({"ok": False, "notice": "当前渠道不支持此语音格式，请选择 SILK、AMR、WAV 或 MP3 文件"}, status_code=400)
            ipad_emoji = channel.channel_type == "wechat_ipad" and normalized_type == "emoji"
            binary = await file.read((stickers.MAX_BYTES if ipad_emoji else message_actions.MAX_UPLOAD_BYTES) + 1)
            content_type = file.content_type
            if ipad_emoji:
                try:
                    content_type = stickers.media_type(binary)
                except ValueError as exc:
                    return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
            if not binary or len(binary) > message_actions.MAX_UPLOAD_BYTES:
                return JSONResponse({"ok": False, "notice": "文件不能为空，且不能超过 25 MB"}, status_code=400)
            ipad_video = channel.channel_type == "wechat_ipad" and normalized_type == "video"
            if ipad_video:
                cover = await video_cover.read(MAX_COVER_BYTES + 1) if video_cover else b""
                if (not file.filename.lower().endswith(".mp4") or not has_mp4_header(binary)
                        or not 0 < len(cover) <= MAX_COVER_BYTES or not 0 < play_length <= 86400):
                    return JSONResponse({"ok": False, "notice": "请选择带有效封面和时长的 MP4 视频"}, status_code=400)
            stored = store_outbound_upload(tenant_id=int(channel.tenant_id), channel_id=int(channel.id), file_name=file.filename, content=binary, content_type="video/mp4" if ipad_video else content_type, message_type=normalized_type)
            download_url = f"{build_public_base_url(request)}/outbound-files/{channel.id}/{stored['share_id']}/{quote(stored['file_name'])}"
            payload["file_name"] = stored["file_name"]
            if channel.channel_type in {"wechat_ipad", "telegram"} and normalized_type == "image":
                payload["image_base64"] = base64.b64encode(binary).decode("ascii")
            elif channel.channel_type == "wechat_ipad" and normalized_type == "file":
                payload["file_base64"] = base64.b64encode(binary).decode("ascii")
            elif ipad_video:
                payload.update(video_base64=base64.b64encode(binary).decode("ascii"),
                               image_base64=base64.b64encode(cover).decode("ascii"), play_length=play_length)
            elif ipad_emoji:
                payload["emoji_base64"] = base64.b64encode(binary).decode("ascii")
            elif channel.channel_type == "wechat_gugatool":
                # 咕嘎适配器把内容转成手机要的原始字节；平台对外地址多数是内网地址，
                # 再按 URL 回抓会被 SSRF 防护拒绝，这里直接传 base64。
                payload[{"image": "image_base64", "voice": "voice_base64", "video": "video_base64", "emoji": "emoji_base64", "file": "file_base64"}[normalized_type]] = base64.b64encode(binary).decode("ascii")
            elif channel.channel_type == "wechat_ipad" and normalized_type == "voice":
                payload["voice_path"] = str(stored["target_path"])
            else:
                payload[{"image": "image_url", "voice": "voice_url", "video": "video_url", "emoji": "emoji", "file": "file_url"}[normalized_type]] = download_url
        # 必须经过渠道适配器统一出站：微桥会在这里创建手机 poll 要领取的命令任务，
        # 之前只写消息表不入队，所以手机永远收不到后台发出的消息。
        message, adapter_result = dispatch_outbound_message(
            db, channel, conversation, payload
        )
        raw_request = message_actions.object_json(message.raw_request_json)
        if selected_mentions:
            raw_request["mentions"] = selected_mentions
        if reply is not None:
            who = IdentityDirectory(db).for_messages([reply]).get(int(reply.id), {})
            raw_request["reply_to"] = {"id": int(reply.id), "name": who.get("name") or "联系人", "text": message_view.preview(reply.content, reply.message_type, limit=200)}
        if raw_request:
            message.raw_request_json = json.dumps(redact_base64_fields(raw_request), ensure_ascii=False)
        if stored is not None:
            db.add(MessageAttachment(tenant_id=channel.tenant_id, message_id=message.id, channel_id=channel.id, conversation_id=conversation.id, direction="out", attachment_type=normalized_type, provider=channel.channel_type, status="downloaded", storage_path=str(stored["target_path"]), mime_type=stored["content_type"], file_size=stored["file_size"], metadata_json=json.dumps(stored["metadata"], ensure_ascii=False)))
        db.commit()
        record_audit(db, "message.manual-send", "message", message.id, f"conversation={conversation.id}")
        if not adapter_result.get("ok"):
            if adapter_result.get("external_status") == "unknown":
                return JSONResponse({"ok": False, "unconfirmed": True, "notice": adapter_result["error_message"], "message_id": int(message.id)})
            notice = f"发送失败：{adapter_result.get('error_message') or adapter_result.get('error_code') or '未知错误'}"
            return JSONResponse({"ok": False, "notice": notice, "message_id": int(message.id)})
        notice = "消息已提交，等待渠道执行" if adapter_result.get("external_status") == "pending" else "消息已发送"
        return JSONResponse({"ok": True, "notice": notice, "message_id": int(message.id)})
    finally:
        db.close()


@router.get("/media/attachments/{attachment_id}")
async def message_attachment_media(request: Request, attachment_id: int):
    """以当前租户权限提供已落库媒体，供消息中心直接展示图片、音频和视频。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        attachment = db.get(MessageAttachment, int(attachment_id))
        if attachment is None or not attachment.storage_path:
            raise HTTPException(status_code=404, detail="Attachment not found")
        path = Path(str(attachment.storage_path))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file not found")
        return FileResponse(path, media_type=attachment.mime_type or "application/octet-stream", filename=path.name)
    finally:
        db.close()


# 消息中心一次加载的消息条数:首屏、每次向上滚动加载更早的消息、发送后拉取新消息都用这个数。
MESSAGE_PAGE_SIZE = 30


def load_conversation_messages(db: Session, conversation_id: int, *, before_id: int = 0, after_id: int = 0, limit: int = MESSAGE_PAGE_SIZE, message_ids: list[int] | None = None) -> dict[str, object]:
    """读取会话里的一段消息,按时间从早到晚返回,并附带显示所需的身份和附件。

    - 不带参数:最新的 limit 条(首屏);
    - before_id:比这条更早的最近 limit 条(向上滚动加载更早);
    - after_id:比这条更新的最多 limit 条(发送后拉取新消息)。
    多取一条用来判断两个方向上是否还有更多。
    """

    rows, has_more = list_history_messages(
        db, conversation_id, before_id=before_id, after_id=after_id,
        limit=limit, message_ids=message_ids,
    )
    identities = IdentityDirectory(db).for_messages(rows)
    attachments: dict[int, list[MessageAttachment]] = {}
    if rows:
        for attachment in db.scalars(
            select(MessageAttachment).where(MessageAttachment.message_id.in_([int(item.id) for item in rows])).order_by(MessageAttachment.id.asc())
        ).all():
            if attachment.message_id is not None:
                attachments.setdefault(int(attachment.message_id), []).append(attachment)
    # 每条消息解析成展示结构(文字/引用/卡片/媒体占位…),媒体类再补上附件状态(已下载/未下载)
    conversation = db.get(Conversation, conversation_id)
    channel = db.get(Channel, conversation.channel_id) if conversation and conversation.channel_id else None
    views = {}
    for item in rows:
        raw = message_actions.object_json(item.raw_request_json)
        view = message_view.describe(item.content, item.message_type)
        if attachments.get(int(item.id)) and item.message_type in message_actions.MEDIA_TYPES and view["kind"] != "media":
            view = {"kind": "media", "media_type": item.message_type, "label": message_actions.MEDIA_LABELS[item.message_type]}
        view = message_view.attach_media(view, attachments.get(int(item.id), []))
        quote = raw.get("quote")
        if view["kind"] == "text" and isinstance(quote, dict):
            # 入站引用按快照还原卡片:正文是回复内容,引用块显示原发送人和原文。
            view = message_view.quote_view(quote, item.content)
        reply = raw.get("reply_to")
        if view["kind"] == "text" and isinstance(reply, dict):
            view = {"kind": "quote", "title": item.content, "refer_name": str(reply.get("name") or ""), "refer_text": str(reply.get("text") or "")}
        if view["kind"] == "media" and attachments.get(int(item.id)):
            meta = message_actions.object_json(attachments[int(item.id)][-1].metadata_json)
            if not view.get("title"):
                view["title"] = str(meta.get("file_name") or "")
        view.update(message_actions.revoke_info(item, channel))
        if item.status == "revoked":
            view.update(kind="system", text="你撤回了一条消息")
        # 客户端只替换状态改变的行，避免轮询打断正在播放的音视频。
        view["version"] = ":".join(str(value or "") for value in (item.updated_at, item.status, item.external_status, item.external_message_id, view["revoke_state"], identities.get(int(item.id), {}).get("is_own")))
        views[int(item.id)] = view
    return {"messages": rows, "has_more": has_more, "identities": identities, "attachments": attachments, "views": views}


def _media_message(db: Session, message_id: int) -> Message:
    message = db.get(Message, int(message_id)) if message_id else None
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.message_type not in message_media.MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Not a media message")
    return message


@router.post("/messages/media/load")
async def load_message_media(request: Request, message_id: int = Form(...)):
    """点击媒体占位卡片:让手机把这条消息的图片/视频/语音/表情/文件传上来(已有文件或已在获取中则直接返回当前状态)。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        message = _media_message(db, message_id)
        outcome = message_media.request(db, message)
        db.commit()
        return JSONResponse({"ok": outcome["state"] != "failed", **outcome, "notice": outcome["error"]})
    finally:
        db.close()


@router.get("/messages/media/status")
async def message_media_status(request: Request, message_id: int):
    """媒体获取进度(页面轮询):none / loading / ready(带地址)/ failed(带原因)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        message = _media_message(db, message_id)
        outcome = message_media.status(db, message)
        db.commit()  # 状态检查会把失败/超时落实到附件上
        return JSONResponse({"ok": True, **outcome})
    finally:
        db.close()


@router.get("/messages/history")
async def message_history(request: Request, conversation_id: int, before_id: int = 0, after_id: int = 0, sync_ids: str = ""):
    """消息中心的增量加载接口:返回渲染好的消息行 HTML,前端直接插入到对话里。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        page = load_conversation_messages(db, conversation_id, before_id=max(before_id, 0), after_id=max(after_id, 0))
        macro = templates.env.get_template("_message_rows.html").module.message_rows
        html = str(macro(page["messages"], page["identities"], page["views"])) if page["messages"] else ""
        result = {"html": html, "has_more": page["has_more"], "count": len(page["messages"])}
        if sync_ids:
            try:
                identifiers = list(dict.fromkeys(int(value) for value in sync_ids.split(",") if value))
            except ValueError:
                raise HTTPException(status_code=400, detail="消息编号无效")
            if len(identifiers) > 100 or any(value <= 0 for value in identifiers):
                raise HTTPException(status_code=400, detail="一次最多同步 100 条消息")
            updates = load_conversation_messages(db, conversation_id, message_ids=identifiers)
            result["updates_html"] = str(macro(updates["messages"], updates["identities"], updates["views"])) if updates["messages"] else ""
            result["removed_ids"] = list(db.scalars(select(Message.id).where(Message.conversation_id == conversation_id, Message.id.in_(identifiers), Message.status == "echo")).all())
        return JSONResponse(result)
    finally:
        db.close()


@router.get("/messages/capabilities")
async def message_capabilities(request: Request, conversation_id: int):
    enforce_role(request, "readonly")
    db = get_session()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        channel = get_channel(db, int(conversation.channel_id or 0)) if conversation.channel_id else None
        return JSONResponse({"ok": True, "capabilities": message_actions.capabilities(channel, conversation)})
    finally:
        db.close()


@router.get("/messages/mention-members")
async def message_mention_members(request: Request, conversation_id: int, q: str = ""):
    enforce_role(request, "readonly")
    db = get_session()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        channel = get_channel(db, int(conversation.channel_id or 0)) if conversation.channel_id else None
        if channel is None or not message_actions.capabilities(channel, conversation)["mention"]:
            return JSONResponse({"ok": False, "notice": "当前渠道不支持 @ 群成员"}, status_code=400)
        return JSONResponse({"ok": True, **message_mentions.members(db, channel, conversation, query=q[:100])})
    finally:
        db.close()


@router.post("/messages/revoke")
async def revoke_manual_message(request: Request, conversation_id: int = Form(...), message_id: int = Form(...), csrf_token: str = Form("")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        conversation = db.get(Conversation, conversation_id)
        message = db.get(Message, message_id)
        if conversation is None or message is None or message.conversation_id != conversation.id:
            raise HTTPException(status_code=404, detail="消息不存在")
        channel = get_channel(db, int(conversation.channel_id or 0)) if conversation.channel_id else None
        if channel is None:
            return JSONResponse({"ok": False, "notice": "会话未绑定渠道，无法撤回"}, status_code=400)
        try:
            result = message_actions.request_revoke(db, channel, conversation, message)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        record_audit(db, "message.manual-revoke", "message", message.id, f"conversation={conversation.id}")
        db.commit()
        return JSONResponse(result)
    finally:
        db.close()


def _message_conversation_summaries(db: Session, scope, *, selected_id=0):
    if selected_id:
        scope = scope.where(Conversation.id == selected_id)
    conversations = db.scalars(scope.order_by(
        Conversation.last_message_at.is_(None), Conversation.last_message_at.desc(), Conversation.id.desc(),
    ).limit(100)).all()
    # 专用目录确认的非 gh_ 公众号也需要显示类型徽标；按当前页批量查询，避免逐条读取联系人。
    official_ids = set(db.scalars(apply_kind_filter(
        select(Conversation.id).where(Conversation.id.in_([item.id for item in conversations])), "official",
    )).all()) if conversations else set()
    conversation_summaries = []
    summary_directory = IdentityDirectory(db)
    summary_identities = summary_directory.for_conversations(conversations)
    latest_messages = latest_history_messages(db, [int(row.id) for row in conversations])
    # 首屏与自动刷新共用这份时间摘要；一次取当前业务时间，避免跨午夜时同批会话分界不一致。
    summary_now = now_shanghai()
    for item in conversations:
        latest = latest_messages.get(int(item.id))
        latest_at = latest.created_at if latest else None
        identity = summary_identities[int(item.id)]
        display_name = identity["name"] or item.title or item.external_id or f"会话 #{item.id}"
        conversation_summaries.append(
            {
                "conversation": item,
                "latest": latest,
                "display_name": display_name,
                "initial": display_name[:1].upper(),
                "identity": identity,
                "kind": "official" if item.id in official_ids else conversation_kind(item.chat_type, item.external_id),
                "time": format_conversation_time(latest_at, now=summary_now),
                "time_title": format_beijing_time(latest_at),
            }
        )
    return conversation_summaries


def _message_kind_tabs(*, bot_id: int, channel_id: int, current: str) -> list[dict[str, Any]]:
    """消息页顶部的类型切换:全部 / 群聊 / 私聊 / 公众号。切换时保留当前的机器人、账号筛选。"""

    tabs = []
    for value, icon in (("", "grid"), ("group", "users"), ("direct", "message"), ("official", "newspaper")):
        query = {key: val for key, val in (("bot_id", bot_id), ("channel_id", channel_id), ("chat_type", value)) if val}
        tabs.append({
            "value": value, "label": KIND_LABELS.get(value, "全部"), "icon": icon,
            "href": "/messages" + (("?" + urlencode(query)) if query else ""), "active": value == current,
        })
    return tabs


@router.get("/messages/conversations")
async def message_conversations(request: Request, bot_id: int = 0, channel_id: int = 0, chat_type: str = ""):
    enforce_role(request, "readonly")
    db = get_session()
    try:
        _, channel_id = inbox.account_scope(db, bot_id, channel_id, include_accounts=False)
        scope = inbox.inbox_query(bot_id, channel_id, normalize_kind(chat_type))
        summaries = _message_conversation_summaries(db, scope)
        items = []
        for row in summaries:
            latest = row["latest"]
            items.append({
                "id": int(row["conversation"].id), "display_name": row["display_name"],
                "preview": ("你撤回了一条消息" if latest.status == "revoked" else message_view.preview(latest.content, latest.message_type, limit=80)) if latest else "暂无消息",
                "time": row["time"], "time_title": row["time_title"],
                "avatar_url": row["identity"].get("avatar_url") or "", "initial": row["initial"],
                "kind": row["kind"],
            })
        return JSONResponse({"ok": True, "conversations": items})
    finally:
        db.close()


@router.get("/messages/count")
def message_count(request: Request, conversation_id: int):
    # 精确统计可能很慢。同步路由在线程池执行，避免阻塞聊天轮询。
    enforce_role(request, "readonly")
    db = get_session()
    try:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        total = db.scalar(select(func.count(Message.id)).where(
            Message.conversation_id == conversation_id, Message.status != "echo",
        ))
        return JSONResponse({"total": int(total)}, headers={"Cache-Control": "private, no-store"})
    finally:
        db.close()


@router.get("/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    bot_id: int = 0,
    channel_id: int = 0,
    chat_type: str = "",
    conversation_id: int = 0,
):
    enforce_role(request, "readonly")
    db = get_session()
    stages = [("summaries_ms", perf_counter())]
    try:
        accounts, channel_id = inbox.account_scope(db, bot_id, channel_id, conversation_id)
        scope = inbox.inbox_query(bot_id, channel_id, normalize_kind(chat_type))
        conversation_summaries = _message_conversation_summaries(db, scope)

        # 只有选中了会话才读消息;没有分页,更早的消息由页面向上滚动时通过 /messages/history 继续加载。
        if conversation_id:
            stages.append(("recent_messages_ms", perf_counter()))
            page = load_conversation_messages(db, conversation_id)
        else:
            page = {"messages": [], "has_more": False, "identities": {}, "attachments": {}, "views": {}}
        # 选中群会话时,找到对应的群记录,线程头据此提供“群设置”入口。
        stages.append(("thread_header_ms", perf_counter()))
        selected_chatroom = None
        selected_summary = next((row for row in conversation_summaries if int(row["conversation"].id) == conversation_id), None)
        if conversation_id and not selected_summary:
            selected_scope = inbox.inbox_query(bot_id, channel_id, "")
            selected_summary = next(iter(_message_conversation_summaries(db, selected_scope, selected_id=conversation_id)), None)
        if selected_summary and selected_summary["conversation"].chat_type == "group":
            selected_chatroom = db.scalar(
                select(Chatroom).where(
                    Chatroom.channel_id == selected_summary["conversation"].channel_id,
                    Chatroom.external_id == selected_summary["conversation"].external_id,
                )
            )
        stages.append(("render_ms", perf_counter()))
        return render(
            request,
            "messages.html",
            page_title="消息中心",
            messages=page["messages"],
            message_identities=page["identities"],
            message_views=page["views"],
            has_more_messages=page["has_more"],
            conversation_summaries=conversation_summaries,
            selected_conversation_id=conversation_id,
            selected_chatroom=selected_chatroom, selected_summary=selected_summary,
            inbox_accounts=accounts,
            back_href=safe_return_to(request.query_params.get("return_to"), ""),
            bot_id=bot_id,
            channel_id=channel_id,
            chat_type=normalize_kind(chat_type),
            kind_labels=KIND_LABELS,
            kind_tabs=_message_kind_tabs(bot_id=bot_id, channel_id=channel_id, current=normalize_kind(chat_type)),
            selected_kind=selected_summary["kind"] if selected_summary else "",
        )
    finally:
        stages.append(("session_close_ms", perf_counter()))
        db.close()
        finished = perf_counter()
        elapsed = finished - stages[0][1]
        if elapsed >= SLOW_MESSAGE_PAGE_SECONDS:
            # 只记录实际执行阶段的耗时，不写会话标识、查询参数或消息内容。
            stages.append(("end", finished))
            timings = {name: round((end - start) * 1000, 2) for (name, start), (_, end) in pairwise(stages)}
            logger.warning("slow messages page", extra={"duration_ms": round(elapsed * 1000, 2), **timings})
