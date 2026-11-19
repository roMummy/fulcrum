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
from html import escape, unescape
from typing import Any
from urllib import parse as urlparse
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree

from app.core.logging import get_logger
from app.models.entities import Channel, Conversation

from app.services.message_view import parse_quote_payload, recall_info, strip_sender
from app.services.safe_fetch import UnsafeUrlError, open_public_url

from .base import BaseChannelAdapter


DEFAULT_TIMEOUT_SECONDS = 15
SEND_TIMEOUT_SECONDS = 30
SYNC_TIMEOUT_SECONDS = 10
MEDIA_TIMEOUT_SECONDS = 30
PROFILE_TIMEOUT_SECONDS = 4
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_SEND_BYTES = 32 * 1024 * 1024
DEFAULT_VOICE_MS = 3000
GUGATOOL_MUSIC_APP_ID = "wx8dd6ecd81906fd84"
GUGATOOL_MUSIC_SEND_TYPE = 3

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

# appmsg(49) 子类型:同一个 messageType 下混着链接、文件、转账、视频号等十几种内容,
# 归一化时按子类型生成可读正文,并把结构化摘要放进 details.appmsg 供插件和 AI 使用。
APPMSG_SUBTYPE_KIND = {
    3: "link",
    4: "link",
    5: "link",
    6: "file",
    8: "emoji",
    19: "chat_record",
    33: "mini_program",
    36: "mini_program",
    51: "video_channel",
    57: "quote",
    62: "pat",
    74: "file",
    87: "group_announcement",
    2000: "transfer",
    2001: "red_packet",
}
APPMSG_KIND_LABELS = {
    "link": "链接",
    "file": "文件",
    "emoji": "表情",
    "chat_record": "聊天记录",
    "mini_program": "小程序",
    "video_channel": "视频号",
    "quote": "引用",
    "pat": "拍一拍",
    "transfer": "转账",
    "red_packet": "红包",
    "group_announcement": "群公告",
}
APPMSG_MAX_XML_CHARS = 60_000
APPMSG_MAX_TEXT_CHARS = 500
APPMSG_MAX_RECORD_CHARS = 60_000
# 这些子类型能直接复用消息中心现有的卡片渲染(链接/聊天记录/小程序/通知),
# 正文写成紧凑 XML,不再只存一行标题。
APPMSG_CARD_KINDS = {
    "link",
    "chat_record",
    "mini_program",
    "video_channel",
    "transfer",
    "red_packet",
    "group_announcement",
}
# appmsg 子类型 -> 统一 message_type。文件单独映射为 file,引用保持 text,
# 没有映射的仍然回退 app。
APPMSG_MESSAGE_TYPES = {
    "link": "link",
    "emoji": "emoji",
    "chat_record": "merged_records",
    "mini_program": "mini_program",
    "video_channel": "video_channel",
    "transfer": "transfer",
    "red_packet": "red_packet",
    "pat": "pat",
    "group_announcement": "group_announcement",
}

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


def build_music_card_xml(payload: dict[str, Any], from_user: str) -> str:
    """微信原生音乐卡片 XML；与服务端插件验证过的 Type=3 / 内层 76 模板一致。"""

    def esc(value: Any) -> str:
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))
        return escape(cleaned, quote=True)

    app_id = esc(str(payload.get("app_id") or "").strip() or GUGATOOL_MUSIC_APP_ID)
    title = esc(payload.get("title") or "音乐")
    singer = esc(payload.get("desc") or "")
    url = esc(payload.get("url") or payload.get("data_url") or "")
    data_url = esc(payload.get("data_url") or "")
    cover = esc(payload.get("song_album_url") or "")
    lyric = esc(payload.get("song_lyric") or "")
    return (
        f'<appmsg appid="{app_id}" sdkver="0"><title>{title}</title><des>{singer}</des>'
        f"<action>view</action><type>76</type><showtype>0</showtype><content/><url>{url}</url>"
        f"<dataurl>{data_url}</dataurl><lowurl>{url}</lowurl><lowdataurl>{data_url}</lowdataurl>"
        f"<recorditem/><thumburl>{cover}</thumburl><messageaction/><laninfo/><extinfo/>"
        f"<sourceusername/><sourcedisplayname/><songlyric>{lyric}</songlyric><commenturl/>"
        f"<appattach><totallen>0</totallen><attachid/><emoticonmd5/><fileext/><aeskey/></appattach>"
        f"<webviewshared><publisherId/><publisherReqId>0</publisherReqId></webviewshared>"
        f"<weappinfo><pagepath/><username/><appid/><appservicetype>0</appservicetype></weappinfo>"
        f"<websearch/><songalbumurl>{cover}</songalbumurl></appmsg>"
        f"<fromusername>{esc(from_user)}</fromusername><scene>0</scene>"
        f"<appinfo><version>1</version><appname/></appinfo><commenturl/>"
    )


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
        upload_url: str | None = None,
        timeout: float = MEDIA_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """通过 Agent 命令让手机回传媒体;新版本可直接上传到 upload_url。"""

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
        if upload_url:
            # 手机优先把文件 multipart 直传平台,避免几十 MB 的 Base64 进命令队列。
            params["upload_url"] = str(upload_url).strip()
        from app.services.guga_agent.bridge import call_command

        try:
            response = call_command(channel, "download_media", params, timeout=timeout + 30, priority=4)
        except RuntimeError as exc:
            # 手机端目前只给图片/语音/视频提供了 download_media 选择器;
            # appmsg(49) 文件会返回这个协议错误,翻译成用户能理解的结论。
            if "download_selector_unavailable" in str(exc):
                raise RuntimeError("手机端暂不支持下载这类文件，请升级咕嘎助手后重试") from exc
            raise
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
            if message_type == "emoji":
                return self._send_emoji(channel, conversation, payload, to_wxid)
            if message_type == "voice":
                return self._send_voice(channel, conversation, payload, to_wxid)
            if message_type == "video":
                return self._send_video(channel, conversation, payload, to_wxid)
            if message_type == "file":
                return self._send_file(channel, conversation, payload, to_wxid)
            if message_type in {"link", "card"}:
                return self._send_link(channel, conversation, payload, to_wxid)
            if message_type == "music":
                return self._send_music(channel, conversation, payload, to_wxid)
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
        quote_local_id = self._coerce_int(payload.get("quote_local_id"))
        quote_msg_svr_id = self._coerce_int(payload.get("quote_msg_svr_id"))
        if quote_local_id or quote_msg_svr_id:
            # 手机的 SendQuote 只接受本地/服务端消息 ID，没有 @ 字段，引用时由入口层拒绝与 @ 同时使用。
            params: dict[str, Any] = {"to_wxid": to_wxid, "content": content}
            if quote_local_id:
                params["local_id"] = quote_local_id
            if quote_msg_svr_id:
                params["msg_svr_id"] = quote_msg_svr_id
            return self._queued_result(channel, conversation, "text", "send_quote", params)
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

    def _send_emoji(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        """表情走手机单独的 SendEmoji 接口，同样按原始字节上传。"""

        content, extension = self._load_media_bytes(
            payload, url_keys=("emoji", "emoji_url", "image_url", "url"), b64_keys=("emoji_base64", "image_base64", "base64")
        )
        return self._queued_media(
            channel,
            conversation,
            message_type="emoji",
            command_name="send_emoji",
            to_wxid=to_wxid,
            content=content,
            content_type=IMAGE_MIME_BY_EXT.get(extension, "image/gif"),
            file_name=self._resolve_file_name(payload, extension or ".gif"),
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
            "description": str(payload.get("description") or payload.get("desc")
                               or payload.get("link_description") or payload.get("content") or "").strip(),
            "url": url,
        }
        thumb_url = str(payload.get("thumb_url") or payload.get("image_url") or "").strip()
        if thumb_url:
            params["thumb_url"] = thumb_url
        thumb_base64 = str(payload.get("thumb_base64") or "").strip()
        if thumb_base64:
            params["thumb_base64"] = thumb_base64
        return self._queued_result(channel, conversation, "link", "send_link", params)

    def _send_music(
        self,
        channel: Channel,
        conversation: Conversation,
        payload: dict[str, Any],
        to_wxid: str,
    ) -> dict[str, Any]:
        """原生音乐卡片走手机的 SendApp：外层 Type=3，XML 内层 76。"""

        title = str(payload.get("title") or "").strip()
        data_url = str(payload.get("data_url") or "").strip()
        if not title or not data_url:
            return self._fail(conversation, "music", "music_payload_missing", "音乐卡片缺少标题或播放地址")
        from app.services.guga_agent.service import registration

        xml = build_music_card_xml(payload, str(registration(channel).get("account_id") or ""))
        return self._queued_result(channel, conversation, "music", "send_app", {
            "to_wxid": to_wxid,
            "xml": xml,
            "type": GUGATOOL_MUSIC_SEND_TYPE,
        })

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
        appmsg = _parse_appmsg_payload(raw_content) if raw_type == 49 else None
        if appmsg is not None:
            appmsg_kind = str(appmsg.get("kind") or "")
            if appmsg_kind == "file":
                # 文件复用现有文件消息契约:消息中心显示文件卡片,点击可按需从手机下载。
                message_type = "file"
            elif appmsg_kind != "quote":
                # 引用仍然按 text 入库,由下面的 quote_payload 逻辑还原引用卡片。
                message_type = APPMSG_MESSAGE_TYPES.get(appmsg_kind, message_type)
        content, content_xml = self._resolve_content(message_type, raw_content, appmsg=appmsg)
        revoke_summary = str(item.get("revokeSummary") or "").strip()

        sender_id = (real_chat_user or from_user) if is_group else (from_user or real_chat_user)
        if is_group and not real_chat_user and "<sysmsg" in raw_content:
            # 群系统消息的 fromUser 是群 id,没有真实发送者;不能把群 id 写成群成员。
            sender_id = ""
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
        # 撤回在微信里是 10002 系统消息(kind 仍是 incoming),不能只认 Agent 的 revoke 事件。
        recalled = recall_info(raw_content) if message_type == "system" else None
        if recalled is not None:
            event_type = "revoke"
            message_type = "system"
            content = str(recalled.get("text") or "一条消息已被撤回")

        # 手机端把入群/退群/改群昵称作为结构化 eventName 上报;
        # 入群/被移出等没有 eventName 的,再从 10002 系统消息 XML 组装。
        structured_change = _build_guga_member_change(item)
        member_change = structured_change
        if member_change is None and "<sysmsg" in raw_content:
            member_change = _build_wechat_sysmsg_member_change(raw_content)
            if member_change is not None:
                # 去掉 "群id:\n" 前缀,共享的邀请关系解析器只认纯 <sysmsg>。
                content_xml = _extract_sysmsg_xml(raw_content)
        if member_change is not None:
            if structured_change is not None:
                message_type = _MEMBER_CHANGE_MESSAGE_TYPES.get(
                    str(member_change.get("change_type") or ""), message_type
                )
            content = (
                str(member_change.get("summary") or "").strip()
                or str(item.get("content") or "").strip()
                or content
            )

        external_message_id = str(item.get("id") or "").strip() or self._build_message_key(item)
        local_message_id = self._coerce_int(item.get("localMessageID"))
        server_message_id = self._coerce_int(item.get("serverMessageID"))
        message_source = str(item.get("messageSource") or "")
        from app.services.maibot_wechat import source_mentions

        quote_payload = parse_quote_payload(raw_content) if kind != "revoke" else None
        if quote_payload:
            # 引用消息(微信 appmsg type=57)按文本入库:正文只留回复内容,
            # 被引用的人/原文放进 details.quote_payload,消息中心和 AI 插件都读这一份。
            message_type = "text"
            content = str(quote_payload.get("title") or "").strip() or "[引用消息]"

        details: dict[str, Any] = {
            "headers_used": sorted((headers or {}).keys()),
            "raw_message_type": raw_type,
            "kind": kind,
            "is_history": bool(item.get("isHistory")),
            "create_time": self._coerce_int(item.get("createTime")),
            # 历史补传和实时消息都保留渠道原时间，统一参与十分钟指令时效判断。
            "timestamp": self._coerce_int(item.get("createTime")),
            "sequence": self._coerce_int(item.get("sequence")),
            "local_message_id": local_message_id,
            "server_message_id": server_message_id,
            "self_wxid": to_user or None,
            "room_id": str(item.get("roomID") or "") or None,
            "member_id": str(item.get("memberID") or "") or None,
            "content_xml": content_xml,
            "message_source": message_source[:2000] or None,
            # 引用原文统一走这个字段;非引用消息显式给 None,与 iPad 渠道保持一致。
            "quote_payload": quote_payload,
            # 撤回只携带明确编号和通知；原文不能作为匹配证据或重新展示。
            "recall": recalled,
            # 事件自带的昵称/头像字段；群名册由异步命令补齐后写入群成员表。
            "sender_avatar_url": None,
            "profile_source": "event" if sender_name else None,
            "member_profile": None,
            # 群里 @ 自己（或 @所有人）时置位，插件与 AI 调度据此判断是否被点名。
            "mentioned_self": self._is_self_mentioned(message_source, to_user),
            "mention_user_list": source_mentions(message_source),
        }
        if member_change is not None:
            details["member_change"] = member_change
            details["member_profile"] = {"nickname": str(item.get("memberNickName") or "").strip()}
        if appmsg is not None:
            # 链接/文件/转账等 appmsg 的结构化摘要;正文只保留上面的可读占位。
            details["appmsg"] = appmsg
            if str(appmsg.get("kind") or "") == "pat":
                pat = appmsg.get("pat") if isinstance(appmsg.get("pat"), dict) else {}
                patted_id = str(pat.get("patted_user") or "").strip()
                details["pat"] = {
                    "from_id": str(pat.get("from_user") or "").strip(),
                    "patted_id": patted_id,
                    # 群/私聊事件的 toUser 都是机器人自己;判断不了就不猜。
                    "patted_is_me": (patted_id == to_user) if (to_user and patted_id) else None,
                }
        if recalled is not None:
            # 撤回是消息状态更新,不交给插件和 AI 当成新发言处理。
            details["skip_plugin_dispatch"] = True
        if self._is_from_self(channel, sender_id):
            # 本账号自己发出的消息(手机上亲自发的、机器人回复后回流的都算):AI 不把它当用户输入,
            # 否则私聊里 AI 会对着自己的回复继续回复(与微桥渠道同样的自问自答循环)。
            details["from_self"] = True
        if message_type in {"image", "voice", "video"}:
            # 入站媒体下载任务只依赖这三个 ID 定位手机端的消息缓存。
            details["media"] = {
                "to_wxid": conversation_id,
                "local_message_id": local_message_id or None,
                "server_message_id": server_message_id or None,
                "message_type": raw_type,
            }
        elif (
            appmsg is not None
            and str(appmsg.get("kind") or "") == "file"
            and (local_message_id or server_message_id)
        ):
            # 文件是 appmsg(49);这里显式声明附件元数据,消息中心按现有文件契约按需下载。
            attachment = appmsg.get("attachment") if isinstance(appmsg.get("attachment"), dict) else {}
            details["media"] = {
                "to_wxid": conversation_id,
                "local_message_id": local_message_id or None,
                "server_message_id": server_message_id or None,
                "message_type": raw_type,
                "attachment_type": "file",
                "file_name": appmsg.get("title") or None,
                "file_ext": attachment.get("file_ext") or None,
                "file_size": attachment.get("file_size") or None,
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

    def _resolve_content(
        self,
        message_type: str,
        raw_content: str,
        *,
        appmsg: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        """媒体消息正文是整段 XML，列表里只展示中文占位，原文放进 details。"""

        label = MEDIA_CONTENT_LABELS.get(message_type)
        if label:
            return label, raw_content or None
        if message_type == "file" and appmsg is not None:
            # 文件/音频保留原始 appmsg XML,消息中心按现有文件卡片解析标题和大小。
            return raw_content, raw_content or None
        if message_type == "system":
            # 撤回在微信里是 10002 系统消息，正文同样是 XML，这里只保留可读结论。
            if "revokemsg" in (raw_content or ""):
                return "对方撤回了一条消息", raw_content or None
            return "[系统消息]", raw_content or None
        if appmsg is not None:
            kind = str(appmsg.get("kind") or "")
            if kind in APPMSG_CARD_KINDS:
                # 链接/聊天记录/小程序等写成紧凑 XML,消息中心直接渲染卡片。
                return _appmsg_display_xml(appmsg), raw_content or None
            if kind in {"emoji", "pat"}:
                return _appmsg_content(appmsg), raw_content or None
        if message_type == APP_MESSAGE_TYPE:
            if appmsg is not None:
                return _appmsg_content(appmsg), raw_content or None
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

    @staticmethod
    def _is_from_self(channel: Channel | None, sender_id: str) -> bool:
        """事件发送者是否就是手机注册时登记的账号本身;账号尚未登记时不猜。"""

        if channel is None or not sender_id.strip():
            return False
        from app.services.guga_agent.service import registration

        own = str(registration(channel).get("account_id") or "").strip()
        return bool(own) and sender_id.strip() == own

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

        raw = payload.get("at") or payload.get("at_list") or payload.get("at_wxids") or payload.get("mention_user_list")
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
            try:
                # 地址来自插件回复或手动发送,是不可信输入:只允许公网地址(含每一跳重定向),
                # 否则租户能让服务器去抓内网内容再当图片发出去。
                with open_public_url(url, timeout=20) as response:
                    content = response.read(MAX_MEDIA_BYTES + 1)
            except UnsafeUrlError as exc:
                raise RuntimeError(f"{key} 地址不允许: {exc}") from exc
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


_MEMBER_EVENT_TYPES = {
    "group_member_join": "join",
    "group_member_leave": "left",
    "group_member_nickname_change": "rename",
}

_MEMBER_CHANGE_MESSAGE_TYPES = {
    "join": "group_member_join",
    "left": "group_member_leave",
    "removed": "group_member_leave",
    "rename": "group_member_nickname_change",
}


def _parse_appmsg_payload(raw_content: str) -> dict[str, Any] | None:
    """解析 appmsg(49) 的公开 XML 字段，未知子类型只保留通用标题。

    手机上报的微信 XML 可能带 ``wxid:\\n`` 前缀，也可能整段再转义一层；
    这里逐层反转义后解析，任何一步失败都返回 None，不影响消息正常入库。
    """

    root = _appmsg_root(raw_content)
    if root is None:
        return None
    appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
    if appmsg is None:
        return None
    subtype = _int_value(_node_text(appmsg, "type"))
    kind = APPMSG_SUBTYPE_KIND.get(subtype, "app")
    payload: dict[str, Any] = {"type": subtype, "kind": kind}
    for key, path in (
        ("title", "title"),
        ("description", "des"),
        ("url", "url"),
        ("thumb_url", "thumburl"),
        ("source", "sourcedisplayname"),
        ("app_name", ".//appinfo/appname"),
    ):
        value = _node_text(root if path.startswith(".//") else appmsg, path)
        if value:
            payload[key] = value
    app_id = str(appmsg.get("appid") or "").strip()
    if app_id:
        payload["app_id"] = app_id

    attachment = _appmsg_attachment(appmsg)
    if attachment:
        payload["attachment"] = attachment
    if kind == "chat_record":
        record = _appmsg_chat_record(appmsg)
        if record:
            payload["chat_record"] = record
    elif kind == "video_channel":
        feed = _appmsg_video_channel(appmsg)
        if feed:
            payload["video_channel"] = feed
    elif kind == "quote":
        quote = _appmsg_quote(appmsg)
        if quote:
            payload["quote"] = quote
    elif kind == "pat":
        pat = _appmsg_pat(appmsg)
        if pat:
            payload["pat"] = pat
    elif kind in {"transfer", "red_packet"}:
        payment = _appmsg_payment(appmsg)
        if payment:
            payload["payment"] = payment
    elif kind == "mini_program":
        mini_program = _appmsg_mini_program(appmsg)
        if mini_program:
            payload["mini_program"] = mini_program
    elif kind == "group_announcement":
        announcement = _node_text(appmsg, "textannouncement", 2000) or str(payload.get("description") or "")
        if announcement:
            payload["announcement"] = announcement
    return payload


def parse_appmsg_details(raw_content: str) -> dict[str, Any] | None:
    """解析 appmsg 原始 XML;媒体落盘等跨模块场景复用渠道解析结果。"""

    return _parse_appmsg_payload(raw_content)


def _appmsg_root(raw_content: str) -> ElementTree.Element | None:
    """去掉发送人前缀并逐层反转义，返回包含 appmsg 的 XML 根节点。"""

    current = strip_sender(str(raw_content or "").strip())
    for _ in range(3):
        root = _parse_xml(current)
        if root is not None:
            return root
        unescaped = unescape(current)
        if unescaped == current:
            return None
        current = strip_sender(unescaped)
    return None


def _parse_xml(body: str) -> ElementTree.Element | None:
    text = str(body or "").strip()
    if not text.startswith("<") or len(text) > APPMSG_MAX_XML_CHARS:
        return None
    try:
        return ElementTree.fromstring(text)
    except (ElementTree.ParseError, ValueError):
        return None


def _node_text(node: ElementTree.Element | None, path: str, limit: int = APPMSG_MAX_TEXT_CHARS) -> str:
    if node is None:
        return ""
    target = node.find(path)
    if target is None:
        return ""
    text = str(target.text or "").strip()
    return text[:limit] if text else ""


def _int_value(value: Any) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0


def _appmsg_attachment(appmsg: ElementTree.Element) -> dict[str, Any]:
    node = appmsg.find("appattach")
    if node is None:
        return {}
    info: dict[str, Any] = {}
    for key, path in (
        ("file_ext", "fileext"),
        ("attach_id", "attachid"),
        ("emoticon_md5", "emoticonmd5"),
        ("cdn_thumb_url", "cdnthumburl"),
    ):
        value = _node_text(node, path, 1000)
        if value:
            info[key] = value
    size = _int_value(_node_text(node, "totallen"))
    if size:
        info["file_size"] = size
    return info


def _appmsg_chat_record(appmsg: ElementTree.Element) -> dict[str, Any]:
    """聊天记录只保留标题、摘要和条数；完整的 recorditem 可能几十 KB，不重复入库。"""

    raw = _node_text(appmsg, "recorditem", APPMSG_MAX_RECORD_CHARS)
    if not raw:
        return {}
    root = _parse_xml(raw)
    if root is None:
        unescaped = unescape(raw)
        root = _parse_xml(unescaped) if unescaped != raw else None
    if root is None:
        return {}
    data_list = root.find(".//datalist")
    info: dict[str, Any] = {
        "title": _node_text(root, "title", 200),
        "description": _node_text(root, "desc", 1000),
    }
    count = _int_value(data_list.get("count") if data_list is not None else "")
    if count:
        info["item_count"] = count
    return {key: value for key, value in info.items() if value}


def _appmsg_video_channel(appmsg: ElementTree.Element) -> dict[str, Any]:
    feed = appmsg.find("finderFeed")
    if feed is None:
        return {}
    info: dict[str, Any] = {
        "object_id": _node_text(feed, "objectId", 100),
        "nickname": _node_text(feed, "nickname", 200),
        "description": _node_text(feed, "desc", 1000),
        "avatar_url": _node_text(feed, "avatar", 1000),
        "feed_type": _node_text(feed, "feedType", 20),
    }
    count = _int_value(_node_text(feed, "mediaCount"))
    if count:
        info["media_count"] = count
    return {key: value for key, value in info.items() if value}


def _appmsg_quote(appmsg: ElementTree.Element) -> dict[str, Any]:
    refer = appmsg.find("refermsg")
    if refer is None:
        return {}
    return {
        "ref_type": _int_value(_node_text(refer, "type")),
        "server_id": _node_text(refer, "svrid", 100),
        "display_name": _node_text(refer, "displayname", 200),
        "content": _node_text(refer, "content", 1000),
    }


def _appmsg_pat(appmsg: ElementTree.Element) -> dict[str, Any]:
    pat = appmsg.find("patMsg")
    record = pat.find(".//record") if pat is not None else None
    if record is None:
        return {}
    return {
        "template": _node_text(record, "templete", 500),
        "from_user": _node_text(record, "fromUser", 100),
        "patted_user": _node_text(record, "pattedUser", 100),
        "chat_user": _node_text(pat, "chatUser", 100),
    }


def _appmsg_payment(appmsg: ElementTree.Element) -> dict[str, Any]:
    pay = appmsg.find("wcpayinfo")
    if pay is None:
        return {}
    info = {
        "subtype": _node_text(pay, "paysubtype", 20),
        "amount": _node_text(pay, "feedesc", 100),
        "memo": _node_text(pay, "pay_memo", 200),
        "transaction_id": _node_text(pay, "transcationid", 100),
        "transfer_id": _node_text(pay, "transferid", 100),
    }
    return {key: value for key, value in info.items() if value}


def _appmsg_mini_program(appmsg: ElementTree.Element) -> dict[str, Any]:
    weapp = appmsg.find("weappinfo")
    if weapp is None:
        return {}
    info = {
        "app_id": _node_text(weapp, "appid", 100),
        "page_path": _node_text(weapp, "pagepath", 500),
        "username": _node_text(weapp, "username", 200),
        "service_type": _node_text(weapp, "appservicetype", 20),
    }
    return {key: value for key, value in info.items() if value}


def _appmsg_display_xml(payload: dict[str, Any]) -> str:
    """生成只含展示字段的紧凑 appmsg XML。

    消息中心已有按 XML 渲染链接/聊天记录/小程序/通知卡片的能力;这里把
    咕嘎上报的原始字段(尤其聊天记录里几十 KB 的 recorditem)裁剪后再入库,
    既不改公共解析器,也避免消息正文过大。
    """

    kind = str(payload.get("kind") or "")
    subtype = _int_value(payload.get("type")) or 5
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    url = str(payload.get("url") or "").strip()
    source = str(payload.get("source") or payload.get("app_name") or "").strip()
    if kind == "video_channel":
        feed = payload.get("video_channel") if isinstance(payload.get("video_channel"), dict) else {}
        title = str(feed.get("nickname") or title or "视频号").strip()
        description = str(feed.get("description") or description).strip()
        source = "视频号"
    elif kind == "group_announcement":
        title = "群公告"
        description = str(payload.get("announcement") or description).strip()
        source = "群公告"
    elif kind == "chat_record":
        record = payload.get("chat_record") if isinstance(payload.get("chat_record"), dict) else {}
        description = str(record.get("description") or description).strip()
    elif kind in {"transfer", "red_packet"}:
        payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
        description = str(payment.get("amount") or description).strip()
    if not (title or description or url):
        return ""
    # 只有普通链接的 url 是可点击的目标;视频号/转账等卡片里的 url 常是微信升级提示,
    # 原样保留会把卡片链到 support.weixin.qq.com。
    if kind != "link":
        url = ""
    parts = ["<msg><appmsg>"]
    if title:
        parts.append(f"<title>{escape(title[:200])}</title>")
    if description:
        parts.append(f"<des>{escape(description[:500])}</des>")
    parts.append(f"<type>{subtype}</type>")
    if url:
        parts.append(f"<url>{escape(url[:1000])}</url>")
    if source:
        parts.append(f"<sourcedisplayname>{escape(source[:100])}</sourcedisplayname>")
    parts.append("</appmsg></msg>")
    return "".join(parts)


def _appmsg_content(payload: dict[str, Any]) -> str:
    """按子类型生成会话列表和插件都能直接读取的中文占位正文。"""

    kind = str(payload.get("kind") or "")
    title = str(payload.get("title") or "").strip()[:200]
    if kind in {"link", "file", "chat_record", "mini_program", "group_announcement"}:
        label = APPMSG_KIND_LABELS[kind]
        return f"[{label}] {title}" if title else f"[{label}]"
    if kind == "emoji":
        return f"[{APPMSG_KIND_LABELS[kind]}]"
    if kind == "video_channel":
        feed = payload.get("video_channel") if isinstance(payload.get("video_channel"), dict) else {}
        label = str(feed.get("description") or feed.get("nickname") or "").strip()[:200]
        return f"[视频号] {label}" if label else "[视频号]"
    if kind == "quote":
        return title or "[引用消息]"
    if kind == "pat":
        return "[拍一拍]"
    if kind in {"transfer", "red_packet"}:
        payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
        amount = str(payment.get("amount") or "").strip()
        label = APPMSG_KIND_LABELS[kind]
        return f"[{label}] {amount}" if amount else f"[{label}]"
    return title or "[链接/文件]"


def _extract_sysmsg_xml(content: str) -> str:
    """截出正文里的 <sysmsg> 片段;手机端可能在前面带 "群id:\\n"。"""

    raw = str(content or "")
    start = raw.find("<sysmsg")
    end = raw.rfind("</sysmsg>")
    if start < 0 or end <= start:
        return ""
    return raw[start:end + len("</sysmsg>")]


def _build_wechat_sysmsg_member_change(content: str) -> dict[str, Any] | None:
    """从 10002 系统消息 XML 组装成员事件;复用群功能的微信 XML 解析。"""

    xml = _extract_sysmsg_xml(content)
    if not xml:
        return None
    try:
        from app.services.group_features.member_change import parse_member_change
    except Exception:  # noqa: BLE001 - 解析依赖不可用时按普通系统消息处理
        return None
    parsed = parse_member_change({"message": {"content": xml, "message_type": "system"}, "details": {}})
    if not parsed:
        return None
    members = parsed.get("members") or []
    names = "、".join(
        str(item.get("display_name") or item.get("external_id") or "").strip()
        for item in members
        if isinstance(item, dict)
    )
    change_type = str(parsed.get("change_type") or "")
    summary = {
        "join": f"{names} 加入了群聊",
        "left": f"{names} 退出了群聊",
        "removed": f"{names} 被移出了群聊",
    }.get(change_type, "")
    return {**parsed, "summary": summary} if summary else parsed


def _build_guga_member_change(item: dict[str, Any]) -> dict[str, Any] | None:
    """把手机端群成员 eventName 转成群功能通用的 member_change 结构。"""

    event_name = str(item.get("eventName") or item.get("messageSubType") or "").strip().lower()
    change_type = _MEMBER_EVENT_TYPES.get(event_name)
    member_id = str(
        item.get("memberID") or item.get("memberUserName") or item.get("realChatUser") or ""
    ).strip()
    if not change_type or not member_id:
        return None

    old_name = str(item.get("oldRoomNickName") or "").strip()
    new_name = str(item.get("newRoomNickName") or "").strip()
    name = (
        new_name
        if change_type == "rename"
        else str(item.get("memberDisplayName") or "").strip()
        or str(item.get("roomNickName") or "").strip()
        or str(item.get("memberNickName") or "").strip()
    )
    name = name or str(item.get("memberDisplayName") or item.get("roomNickName") or member_id).strip()

    result: dict[str, Any] = {
        "change_type": change_type,
        "members": [{"external_id": member_id, "display_name": name}],
    }
    after_count = item.get("snapshotAfterCount")
    if isinstance(after_count, int) and not isinstance(after_count, bool) and after_count >= 0:
        result["after_count"] = after_count
    if change_type == "rename":
        if not new_name:
            return None
        result["new_room_nick_name"] = new_name
        result["old_room_nick_name"] = old_name
        result["summary"] = f"{old_name or name} 将群昵称改为 {new_name}"
    elif change_type == "join":
        result["summary"] = f"{name} 加入了群聊"
    else:
        result["summary"] = f"{name} 退出了群聊"
    return result


def backfill_wechat_gugatool_invitations(db, channel, room) -> dict[str, Any]:
    """从原始事件回查本群入群系统消息,补记邀请关系。

    咕嘎消息表的系统消息正文只有占位/摘要,原始 XML 仍在 raw_webhook_events,
    因此这里按群 ID 和"加入了群聊"过滤原始事件,再交给统一入口解析。
    """

    from sqlalchemy import desc, select

    from app.core.time import ensure_shanghai_datetime
    from app.models.entities import Conversation, RawWebhookEvent
    from app.services.chatroom_invitations import HISTORY_LIMIT, record_invitation_event

    room_id = str(room.external_id or "").strip()
    if not room_id:
        return {"checked": 0, "updated": 0, "limited": False}
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.tenant_id == int(channel.tenant_id),
            Conversation.channel_id == int(channel.id),
            Conversation.bot_id == int(channel.bot_id),
            Conversation.external_id == room_id,
            Conversation.chat_type == "group",
        )
    )
    if conversation is None:
        return {"checked": 0, "updated": 0, "limited": False}
    rows = db.scalars(
        select(RawWebhookEvent).where(
            RawWebhookEvent.tenant_id == int(channel.tenant_id),
            RawWebhookEvent.channel_id == int(channel.id),
            RawWebhookEvent.payload_json.like(f"%{room_id}%"),
            RawWebhookEvent.payload_json.like("%加入了群聊%"),
        ).order_by(desc(RawWebhookEvent.id)).limit(HISTORY_LIMIT + 1)
    ).all()
    adapter = WechatGugatoolAdapter()
    updated = 0
    for raw in reversed(rows[:HISTORY_LIMIT]):
        try:
            payload = json.loads(raw.payload_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        normalized = adapter.normalize_inbound_payload(channel, payload)
        updated += record_invitation_event(
            db,
            channel,
            conversation,
            normalized,
            recorded_at=ensure_shanghai_datetime(raw.created_at),
        )
    return {
        "checked": min(len(rows), HISTORY_LIMIT),
        "updated": updated,
        "limited": len(rows) > HISTORY_LIMIT,
    }
