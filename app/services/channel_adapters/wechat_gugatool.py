"""微信 iOS（GugaTool）适配器。

GugaTool 是注入微信进程的 tweak，在手机本地监听 HTTP 服务（默认 19090）。
本适配器只使用它的公开 HTTP 契约，不依赖微信私有类名：

- ``GET  /api/v1/health`` 存活检查
- ``POST /api/v1/Msg/Sync`` 按 Synckey 增量拉取消息
- ``POST /api/v1/Msg/SendTxt|SendLink`` JSON 发文本和链接卡片
- ``POST /api/v1/Msg/SendImage|SendVideo|SendFile|SendVoice`` **raw binary + query**
- ``POST /api/v1/Tools/DownloadMessageMedia`` 换取 ``DownloadURL`` 后 GET 取媒体

两个踩过的坑（与 AstrBot 侧适配器一致）：

1. 手机端的微 HTTP 服务不支持 ``Expect: 100-continue``，大包会直接连接中断，
   因此所有请求都显式发送空的 ``Expect`` 头。
2. 媒体一律用 raw binary 上传，不走 base64 JSON：省一次 33% 膨胀，也避开
   手机端一次性构造大 JSON 的内存峰值。
"""

from __future__ import annotations

import base64
import io
import json
import re
import uuid
from html import unescape
from typing import Any
from urllib import parse as urlparse
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.core.logging import get_logger
from app.models.entities import Channel, Conversation

from .base import BaseChannelAdapter


DEFAULT_TIMEOUT_SECONDS = 15
SEND_TIMEOUT_SECONDS = 30
SYNC_TIMEOUT_SECONDS = 10
MEDIA_TIMEOUT_SECONDS = 30
PROFILE_TIMEOUT_SECONDS = 4
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_SEND_BYTES = 32 * 1024 * 1024
DEFAULT_VOICE_MS = 3000

# 微信原生 messageType 到通用消息类型的映射；49 是 app 消息（文件/链接/小程序）。
MSG_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    37: "friend_request",
    42: "card",
    43: "video",
    47: "emoji",
    48: "location",
    50: "voip",
    10000: "system",
    10002: "system",
}
APP_MESSAGE_TYPE = "app"

# 媒体类消息在会话列表里只展示中文占位，原始 XML/JSON 保留在 details 中。
MEDIA_CONTENT_LABELS = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "emoji": "[表情]",
    "location": "[位置]",
    "voip": "[通话]",
    "card": "[名片]",
    "friend_request": "[好友请求]",
}

# 语音接口用扩展名决定编码：0=amr 1=speex 2=mp3 3=wave 4=silk。
VOICE_TYPE_BY_EXT = {".amr": 0, ".spx": 1, ".speex": 1, ".mp3": 2, ".wav": 3, ".wave": 3, ".silk": 4}
VOICE_MIME_BY_EXT = {
    ".amr": "audio/amr",
    ".spx": "audio/speex",
    ".speex": "audio/speex",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".silk": "audio/silk",
}
IMAGE_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
VIDEO_MIME_BY_EXT = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}

AT_USER_LIST_RE = re.compile(r"<atuserlist>(.*?)</atuserlist>", re.IGNORECASE | re.DOTALL)
APPMSG_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
XML_ATTR_RE = re.compile(r'([A-Za-z0-9_:-]+)\s*=\s*"([^"]*)"')
XML_MEDIA_TAG_RE = re.compile(r"<(img|video|emoji|appmsg)[^>]*>", re.IGNORECASE)

logger = get_logger(__name__)


class WechatGugatoolAdapter(BaseChannelAdapter):
    """把 GugaTool HTTP 接口映射成 Fulcrum 的统一渠道能力。"""

    adapter_type = "wechat_gugatool"

    # ---------------------------------------------------------------- 基础设施

    def _load_adapter_config(self, channel: Channel) -> dict[str, Any]:
        try:
            payload = json.loads(channel.adapter_config_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _enqueue(
        self,
        channel: Channel,
        conversation: Conversation,
        command_name: str,
        params: dict[str, Any],
        *,
        priority: int = 90,
    ) -> tuple[str, int]:
        """把发送命令写入 Agent 队列,手机下次 poll/WS 立即领取。"""

        from sqlalchemy.orm import object_session

        from app.services.guga_agent.service import enqueue_command

        db = object_session(channel)
        if db is None:
            raise RuntimeError("渠道不在数据库会话中，无法入队发送")
        request_id = f"guga_{uuid.uuid4().hex}"
        job = enqueue_command(
            db,
            channel,
            command_name=command_name,
            params=params,
            request_id=request_id,
            conversation_id=int(conversation.id or 0),
            priority=priority,
        )
        db.flush()
        return request_id, int(job.id)

    # ---------------------------------------------------------------- 能力实现

    def health_check(self, channel: Channel) -> dict[str, Any]:
        """健康状态来自手机最近一次注册/心跳,不再反向探测手机。"""

        from app.services.guga_agent.access import account_info

        info = account_info(channel)
        online = bool(info.get("account_id")) and bool(info.get("online"))
        return {
            "ok": online,
            "adapter_type": self.adapter_type,
            "status": "online" if online else "offline",
            "summary": "手机端已连接" if online else "手机端未连接",
            "details": info,
        }

    # ---------------------------------------------------------------- 入站媒体

    def download_message_media(
        self,
        channel: Channel,
        *,
        to_wxid: str,
        local_id: int | None = None,
        msg_svr_id: int | None = None,
        timeout: float = MEDIA_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """通过 Agent 命令让手机回传媒体 Base64。"""

        if not (local_id or msg_svr_id):
            raise RuntimeError("缺少本地或服务端消息 ID，无法下载媒体")
        params: dict[str, Any] = {
            "to_wxid": str(to_wxid or "").strip(),
            "timeout_seconds": int(max(float(timeout), 1.0)),
        }
        if local_id:
            params["local_id"] = int(local_id)
        if msg_svr_id:
            params["msg_svr_id"] = int(msg_svr_id)
        from app.services.guga_agent.bridge import call_command

        response = call_command(channel, "download_media", params, timeout=timeout + 30, priority=4)
        data = response.get("Data") if isinstance(response.get("Data"), dict) else {}
        return {"ok": True, "data": data, "raw": response}

    # ---------------------------------------------------------------- 出站发送

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        message_type = str(payload.get("message_type") or "text").strip().lower() or "text"
        to_wxid = str(conversation.external_id or "").strip()
        if not to_wxid:
            return self._fail(conversation, message_type, "conversation_external_id_missing", "会话缺少微信 ID")

        try:
            if message_type in {"text", "markdown"}:
                return self._send_text(channel, conversation, payload, to_wxid)
            if message_type == "image":
                return self._send_image(channel, conversation, payload, to_wxid)
            if message_type == "voice":
                return self._send_voice(channel, conversation, payload, to_wxid)
            if message_type == "video":
                return self._send_video(channel, conversation, payload, to_wxid)
            if message_type == "file":
                return self._send_file(channel, conversation, payload, to_wxid)
            if message_type in {"link", "card"}:
                return self._send_link(channel, conversation, payload, to_wxid)
            return self._fail(
                conversation,
                message_type,
                "message_type_unsupported",
                f"咕嘎助手渠道暂不支持发送 {message_type} 消息",
            )
        except RuntimeError as exc:
            return self._fail(conversation, message_type, "send_failed", str(exc))

    def _send_text(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            return self._fail(conversation, "text", "empty_content", "文本内容为空")
        params: dict[str, Any] = {"to_wxid": to_wxid, "content": content, "type": 1}
        at_targets = self._resolve_at_targets(payload)
        if at_targets:
            params["at"] = at_targets
        return self._queued_result(channel, conversation, "text", "send_text", params)

    def _send_image(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        content, extension = self._load_media_bytes(
            payload, url_keys=("image_url", "file_url", "url"), b64_keys=("image_base64", "base64")
        )
        return self._queued_media(
            channel,
            conversation,
            message_type="image",
            command_name="send_image",
            to_wxid=to_wxid,
            content=content,
            content_type=IMAGE_MIME_BY_EXT.get(extension, "application/octet-stream"),
            file_name=self._resolve_file_name(payload, extension or ".jpg"),
        )

    def _send_video(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        content, extension = self._load_media_bytes(
            payload, url_keys=("video_url", "file_url", "url"), b64_keys=("video_base64", "base64")
        )
        return self._queued_media(
            channel,
            conversation,
            message_type="video",
            command_name="send_video",
            to_wxid=to_wxid,
            content=content,
            content_type=VIDEO_MIME_BY_EXT.get(extension, "video/mp4"),
            file_name=self._resolve_file_name(payload, extension or ".mp4"),
        )

    def _send_voice(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        """语音按扩展名选择编码；微信只认 amr/speex/silk，mp3 是否能播由客户端决定。"""

        content, extension = self._load_media_bytes(
            payload, url_keys=("voice_url", "audio_url", "url"), b64_keys=("voice_base64", "audio_base64", "base64")
        )
        voice_time = self._resolve_voice_duration_ms(content, payload)
        voice_type = VOICE_TYPE_BY_EXT.get(extension, 3)
        return self._queued_media(
            channel,
            conversation,
            message_type="voice",
            command_name="send_voice",
            to_wxid=to_wxid,
            content=content,
            content_type=VOICE_MIME_BY_EXT.get(extension, "application/octet-stream"),
            file_name=self._resolve_file_name(payload, extension or ".silk"),
            extra={"voice_type": voice_type, "voice_time_ms": voice_time},
        )

    def _send_file(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        content, extension = self._load_media_bytes(
            payload, url_keys=("file_url", "download_url", "url"), b64_keys=("file_base64", "file_content", "base64")
        )
        return self._queued_media(
            channel,
            conversation,
            message_type="file",
            command_name="send_file",
            to_wxid=to_wxid,
            content=content,
            content_type="application/octet-stream",
            file_name=self._resolve_file_name(payload, extension),
        )

    def _send_link(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        url = str(payload.get("link_url") or payload.get("url") or "").strip()
        if not url:
            return self._fail(conversation, "link", "link_url_missing", "链接消息缺少 link_url")
        params: dict[str, Any] = {
            "to_wxid": to_wxid,
            "title": str(payload.get("title") or payload.get("content") or url).strip(),
            "description": str(payload.get("description") or payload.get("content") or "").strip(),
            "url": url,
        }
        thumb_url = str(payload.get("thumb_url") or payload.get("image_url") or "").strip()
        if thumb_url:
            params["thumb_url"] = thumb_url
        thumb_base64 = str(payload.get("thumb_base64") or "").strip()
        if thumb_base64:
            params["thumb_base64"] = thumb_base64
        return self._queued_result(channel, conversation, "link", "send_link", params)

    def _queued_media(
        self,
        channel: Channel,
        conversation: Conversation,
        *,
        message_type: str,
        command_name: str,
        to_wxid: str,
        content: bytes,
        content_type: str,
        file_name: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not content:
            raise RuntimeError("媒体内容为空")
        if len(content) > MAX_SEND_BYTES:
            raise RuntimeError(f"媒体超过 {MAX_SEND_BYTES // (1024 * 1024)}MB 上限")
        params: dict[str, Any] = {
            "to_wxid": to_wxid,
            "file_name": file_name,
            "content_type": content_type,
            "data_base64": base64.b64encode(content).decode("ascii"),
        }
        if extra:
            params.update(extra)
        return self._queued_result(channel, conversation, message_type, command_name, params)

    def _queued_result(
        self,
        channel: Channel,
        conversation: Conversation,
        message_type: str,
        command_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id, job_id = self._enqueue(channel, conversation, command_name, params)
        return self.build_result(
            ok=True,
            conversation=conversation,
            message_type=message_type,
            transport_status="queued",
            status="pending",
            external_status="pending",
            raw_request={"job_id": job_id, "request_id": request_id},
            raw_response={"queued": True, "command_name": command_name},
            extra={"job_id": job_id, "command_name": command_name},
        )

    # ---------------------------------------------------------------- 资料查询

    def fetch_chatroom_info(self, channel: Channel, room_id: str) -> dict[str, Any]:
        """群资料：通过 Agent 命令让手机查询群名和成员名册。"""

        from app.services.guga_agent.bridge import call_command

        result = call_command(
            channel,
            "get_chatroom_info",
            {"chatroom_wxid": str(room_id or "").strip()},
            timeout=PROFILE_TIMEOUT_SECONDS * 3,
            purpose="chatroom_roster",
        )
        data = result.get("Data") if isinstance(result.get("Data"), dict) else {}
        return data

    def fetch_chatroom_member(self, channel: Channel, room_id: str, member_wxid: str) -> dict[str, Any]:
        """单个群成员资料，用于群成员列表里没出现过的发送者。"""

        from app.services.guga_agent.bridge import call_command

        result = call_command(
            channel,
            "get_chatroom_member_detail",
            {"chatroom_wxid": str(room_id or "").strip(), "member_wxid": str(member_wxid or "").strip()},
            timeout=PROFILE_TIMEOUT_SECONDS * 3,
            purpose="chatroom_member",
        )
        data = result.get("Data") if isinstance(result.get("Data"), dict) else {}
        return data

    # ---------------------------------------------------------------- 入站归一化

    def normalize_inbound_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """把 GugaTool 的 MessageEvent 映射成统一入站事件。

        微信侧字段语义：群消息 ``fromUser`` 是 ``xxx@chatroom``，``realChatUser``
        才是真正的发送者；单聊 ``fromUser`` 就是对方 wxid，``toUser`` 是自己。
        """

        item = payload if isinstance(payload, dict) else {}
        kind = str(item.get("kind") or "incoming").strip() or "incoming"
        from_user = str(item.get("fromUser") or "").strip()
        to_user = str(item.get("toUser") or "").strip()
        real_chat_user = str(item.get("realChatUser") or "").strip()
        is_group = from_user.endswith("@chatroom") or bool(str(item.get("roomID") or "").strip())

        conversation_id = from_user or real_chat_user or "manual"
        raw_type = self._coerce_int(item.get("messageType"))
        message_type = MSG_TYPE_MAP.get(raw_type, APP_MESSAGE_TYPE)
        raw_content = str(item.get("content") or "")
        content, content_xml = self._resolve_content(message_type, raw_content)
        revoke_summary = str(item.get("revokeSummary") or "").strip()

        sender_id = (real_chat_user or from_user) if is_group else (from_user or real_chat_user)
        sender_name = (
            str(item.get("memberDisplayName") or "").strip()
            or str(item.get("memberNickName") or "").strip()
            or str(item.get("roomNickName") or "").strip()
            or None
        )
        # 群消息里的 roomNickName/memberNickName 是发送者昵称，不是群名；
        # 群名由异步 get_chatroom_info 补齐，这里只在单聊里把它当作会话标题。
        conversation_title = None if is_group else sender_name

        # Agent 模式不在事件请求内同步查询手机资料;群名和成员由事件入库后的
        # get_chatroom_info 命令异步补齐,避免与手机的事件上报互相等待。
        event_type = "revoke" if kind == "revoke" else "message"
        if kind == "revoke":
            message_type = "system"
            content = revoke_summary or "对方撤回了一条消息"

        external_message_id = str(item.get("id") or "").strip() or self._build_message_key(item)
        local_message_id = self._coerce_int(item.get("localMessageID"))
        server_message_id = self._coerce_int(item.get("serverMessageID"))
        message_source = str(item.get("messageSource") or "")

        details: dict[str, Any] = {
            "headers_used": sorted((headers or {}).keys()),
            "raw_message_type": raw_type,
            "kind": kind,
            "is_history": bool(item.get("isHistory")),
            "create_time": self._coerce_int(item.get("createTime")),
            "sequence": self._coerce_int(item.get("sequence")),
            "local_message_id": local_message_id,
            "server_message_id": server_message_id,
            "self_wxid": to_user or None,
            "room_id": str(item.get("roomID") or "") or None,
            "member_id": str(item.get("memberID") or "") or None,
            "content_xml": content_xml,
            "message_source": message_source[:2000] or None,
            # 事件自带的昵称/头像字段；群名册由异步命令补齐后写入群成员表。
            "sender_avatar_url": None,
            "profile_source": "event" if sender_name else None,
            "member_profile": None,
            # 群里 @ 自己（或 @所有人）时置位，插件与 AI 调度据此判断是否被点名。
            "mentioned_self": self._is_self_mentioned(message_source, to_user),
        }
        if message_type in {"image", "voice", "video"}:
            # 入站媒体下载任务只依赖这三个 ID 定位手机端的消息缓存。
            details["media"] = {
                "to_wxid": conversation_id,
                "local_message_id": local_message_id or None,
                "server_message_id": server_message_id or None,
                "message_type": raw_type,
            }
        return {
            "adapter_type": self.adapter_type,
            "event_key": external_message_id or None,
            "event_type": event_type,
            "should_persist_message": bool(str(content or "").strip())
            or message_type in {"image", "voice", "video", "app", "system"},
            "conversation": {
                "external_id": conversation_id,
                "title": conversation_title or conversation_id,
                "chat_type": "group" if is_group else "direct",
            },
            "message": {
                "content": content,
                "sender_id": sender_id or None,
                "sender_name": sender_name,
                "message_type": message_type,
                "external_message_id": external_message_id or None,
            },
            "details": details,
        }

    # ---------------------------------------------------------------- 内部工具

    def _resolve_content(self, message_type: str, raw_content: str) -> tuple[str, str | None]:
        """媒体消息正文是整段 XML，列表里只展示中文占位，原文放进 details。"""

        label = MEDIA_CONTENT_LABELS.get(message_type)
        if label:
            return label, raw_content or None
        if message_type == "system":
            # 撤回在微信里是 10002 系统消息，正文同样是 XML，这里只保留可读结论。
            if "revokemsg" in (raw_content or ""):
                return "对方撤回了一条消息", raw_content or None
            return "[系统消息]", raw_content or None
        if message_type == APP_MESSAGE_TYPE:
            return self._extract_app_title(raw_content) or "[链接/文件]", raw_content or None
        return raw_content, None

    def _extract_app_title(self, xml: str) -> str:
        """app 消息的 XML 可能被转义多层，逐层反转义后再取标题。"""

        current = str(xml or "")
        for _ in range(3):
            current = current.strip()
            if not current:
                return ""
            match = APPMSG_TITLE_RE.search(current)
            if match:
                title = unescape(match.group(1)).strip()
                title = re.sub(r"^<!\[CDATA\[|\]\]>$", "", title).strip()
                if title:
                    return title[:200]
            unescaped = unescape(current)
            if unescaped == current:
                return ""
            current = unescaped
        return ""

    def _is_self_mentioned(self, message_source: str, self_wxid: str) -> bool:
        """从 ``<atuserlist>`` 里判断是否 @ 了当前账号或 @所有人。"""

        match = AT_USER_LIST_RE.search(message_source or "")
        if not match:
            return False
        raw = match.group(1).strip()
        if not raw:
            return False
        targets = {segment.strip() for segment in raw.split(",") if segment.strip()}
        return "notify@all" in targets or bool(self_wxid and self_wxid in targets)

    def _resolve_voice_duration_ms(self, content: bytes, payload: dict[str, Any]) -> int:
        explicit = payload.get("voice_time_ms") or payload.get("duration_ms")
        try:
            if explicit:
                return max(1, int(float(explicit)))
        except (TypeError, ValueError):
            pass
        try:
            from pydub import AudioSegment

            segment = AudioSegment.from_file(io.BytesIO(content))
            return max(1, int(len(segment)))
        except Exception:
            # 手机端只要求一个时长用于气泡显示，取不到时用保守默认值。
            return DEFAULT_VOICE_MS

    def _resolve_at_targets(self, payload: dict[str, Any]) -> str:
        """@ 目标统一成手机端要求的逗号分隔 wxid 串。"""

        raw = payload.get("at") or payload.get("at_list") or payload.get("at_wxids")
        if isinstance(raw, (list, tuple)):
            return ",".join(str(item).strip() for item in raw if str(item).strip())
        return str(raw or "").strip()

    def _load_media_bytes(
        self,
        payload: dict[str, Any],
        *,
        url_keys: tuple[str, ...],
        b64_keys: tuple[str, ...],
    ) -> tuple[bytes, str]:
        """优先用 base64，其次下载地址；两者都没有时给出可读错误。"""

        for key in b64_keys:
            encoded = str(payload.get(key) or "").strip()
            if not encoded:
                continue
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                content = base64.b64decode(encoded, validate=False)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("媒体 base64 解析失败") from exc
            if content:
                return content, self._sniff_extension(content)

        for key in url_keys:
            url = str(payload.get(key) or "").strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                raise RuntimeError(f"{key} 不是可下载的 http(s) 地址")
            request = urlrequest.Request(url, method="GET")
            request.add_header("Expect", "")
            try:
                with urlrequest.urlopen(request, timeout=20) as response:
                    content = response.read(MAX_MEDIA_BYTES + 1)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(f"下载媒体失败: {exc}") from exc
            if len(content) > MAX_MEDIA_BYTES:
                raise RuntimeError(f"媒体超过 {MAX_MEDIA_BYTES // (1024 * 1024)}MB 上限")
            if not content:
                raise RuntimeError("下载到的媒体内容为空")
            return content, self._sniff_extension(content)

        raise RuntimeError("缺少媒体内容（既没有 base64 也没有可下载地址）")

    def _resolve_file_name(self, payload: dict[str, Any], extension: str) -> str:
        explicit = str(payload.get("file_name") or "").strip()
        if explicit:
            return explicit
        for key in ("file_url", "download_url", "url"):
            url = str(payload.get(key) or "").strip()
            if not url:
                continue
            name = urlparse.unquote(urlparse.urlparse(url).path.rsplit("/", 1)[-1]).strip()
            if name:
                return name
        return f"file{extension or '.bin'}"

    @staticmethod
    def _sniff_extension(content: bytes) -> str:
        if content[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if content[:4] == b"GIF8":
            return ".gif"
        if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return ".webp"
        if content[4:8] == b"ftyp":
            return ".mp4"
        if content[:3] == b"ID3" or content[:2] in (b"\xff\xfb", b"\xff\xf3"):
            return ".mp3"
        if content[:4] == b"%PDF":
            return ".pdf"
        if content[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        return ".bin"

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _fail(self, conversation: Conversation, message_type: str, code: str, message: str) -> dict[str, Any]:        return self.build_result(
            ok=False,
            conversation=conversation,
            message_type=message_type,
            transport_status="error",
            status="failed",
            error_code=code,
            error_message=message,
        )

    def _build_message_key(self, item: dict[str, Any]) -> str:
        """没有 UUID 时用本地/服务端消息 ID 兜底，保证事件去重键稳定。"""

        local_id = self._coerce_int(item.get("localMessageID"))
        server_id = self._coerce_int(item.get("serverMessageID"))
        if local_id or server_id:
            return f"ios:{local_id}:{server_id}"
        return ""
