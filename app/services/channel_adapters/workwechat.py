from __future__ import annotations

import hashlib
from typing import Any

from app.models.entities import Channel, Conversation
from app.services.security import decrypt_secret
from app.services.workwechat_cloud_poll import is_cloud_poll_enabled, load_cloud_poll_config

from .wechat_winauto import WechatWinAutoAdapter


class WorkWechatAdapter(WechatWinAutoAdapter):
    adapter_type = "workwechat"

    HOOK_MESSAGE_TYPE_MAP = {
        11041: "text",
        11042: "image",
        11045: "file",
        11043: "video",
        11044: "voice",
        11046: "location",
        11047: "link",
        11048: "emoji",
        11049: "red_packet",
        11068: "rich_text",
        11123: "revoke",
        11124: "finder",
        11195: "finder_live",
        11211: "chat_history",
    }

    def health_check(self, channel: Channel) -> dict[str, Any]:
        if is_cloud_poll_enabled(channel):
            # 企微云端 WS/拉取模式由客服电脑主动连云端，服务端不应再反向探测 Windows provider；
            # 这里仅校验云端注册状态和回调用的 Bot Token，避免错误提示“缺少 API Base”。
            config = load_cloud_poll_config(channel)
            issues: list[str] = []
            if not str(config.get("client_token_hash") or "").strip():
                issues.append("企微云端客户端未注册")
            if not decrypt_secret(channel.bot_token_encrypted):
                issues.append("Bot Token 未配置")
            return {
                "ok": not issues,
                "adapter_type": self.adapter_type,
                "status": "online" if not issues else "degraded",
                "summary": "企微云端 WS/拉取模式在线，不需要 API Base" if not issues else "；".join(issues),
                "details": {
                    "delivery_mode": "cloud_poll",
                    "api_base_required": False,
                    "registered": bool(str(config.get("client_token_hash") or "").strip()),
                    "bot_token_configured": bool(decrypt_secret(channel.bot_token_encrypted)),
                },
            }
        return super().health_check(channel)

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_payload = dict(payload)
        normalized_payload.setdefault("endpoint", "/send-message")
        normalized_payload.setdefault("receiver_id", self._resolve_receiver_id(conversation, payload))
        normalized_payload.setdefault("conversation_title", conversation.title or conversation.external_id)
        normalized_payload.setdefault("title", conversation.title or conversation.external_id)
        normalized_payload.setdefault("chat_type", conversation.chat_type or "direct")
        return super().send_message(channel, conversation, normalized_payload)

    def _build_request_body(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        body = super()._build_request_body(channel, conversation, payload)
        # 企业微信群 ID 往往不便于人工排查，额外把群标题带给 provider 作为日志辅助字段。
        conversation_title = self._clean_string(payload.get("conversation_title") or conversation.title)
        if conversation_title:
            body["conversation_title"] = conversation_title
        return body

    def normalize_inbound_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if isinstance(payload.get("conversation"), dict) or isinstance(payload.get("message"), dict):
            return self._normalize_provider_payload(channel, payload, headers=headers)
        return self._normalize_hook_payload(payload, headers=headers)

    def _normalize_provider_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        normalized = super().normalize_inbound_payload(channel, payload, headers=headers)
        normalized["adapter_type"] = self.adapter_type
        details = normalized.get("details")
        if isinstance(details, dict):
            details.setdefault("source_protocol", "workwechat_provider")
            self._fill_provider_file_message_content(normalized, details)
        return normalized

    def _fill_provider_file_message_content(self, normalized: dict[str, Any], details: dict[str, Any]) -> None:
        """补齐 provider 标准消息里的文件展示内容。

        云端客户端有时只把文件名放在 details.file_name/download_path 中，message.content 为空。
        Fulcrum 入站留存依赖 content 非空；这里补成 `[file] 文件名`，保证入站文件消息能写入 Message 表。
        """

        message = normalized.get("message") if isinstance(normalized.get("message"), dict) else {}
        if str(message.get("content") or "").strip():
            return
        message_type = self._clean_string(message.get("message_type")) or "text"
        if message_type not in {"file", "image", "video", "voice"}:
            return
        file_name = self._clean_string(
            details.get("file_name")
            or details.get("filename")
            or details.get("name")
            or details.get("download_path")
            or details.get("storage_path")
        )
        message["content"] = f"[{message_type}] {file_name}" if file_name else f"[{message_type}]"
        normalized["should_persist_message"] = True

    def _normalize_hook_payload(self, payload: dict[str, Any], *, headers: dict[str, str] | None) -> dict[str, Any]:
        hook_type = self._coerce_int(payload.get("type"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_type = self.HOOK_MESSAGE_TYPE_MAP.get(hook_type, "event")

        # 企微 Hook 的会话 ID 按场景可能落在 conversation_id、room_conversation_id 或 room_id。
        conversation_external_id = self._clean_string(
            data.get("conversation_id")
            or data.get("room_conversation_id")
            or data.get("room_id")
            or payload.get("conversation_id")
            or payload.get("chat_id")
        ) or "manual"
        conversation_title = self._clean_string(
            data.get("room_name")
            or data.get("group_name")
            or data.get("chat_name")
            or data.get("room_nickname")
            or data.get("conversation_title")
            or data.get("title")
            or payload.get("conversation_title")
        )
        is_group = bool(
            data.get("is_room")
            or data.get("room_conversation_id")
            or str(conversation_external_id).startswith("R:")
            or str(data.get("room_id") or "").strip() not in {"", "0"}
        )

        content = self._resolve_hook_content(data, message_type)
        sender_id = self._clean_string(data.get("sender") or data.get("sender_id") or data.get("op_user_id"))
        sender_name = self._clean_string(data.get("sender_name") or data.get("op_user_name") or data.get("nickname"))
        external_message_id = self._clean_string(data.get("server_id") or data.get("local_id") or data.get("message_server_id"))
        timestamp = data.get("send_time") or data.get("timestamp") or payload.get("timestamp")
        is_self_sent = bool(data.get("is_self_sent"))
        event_key = (
            self._clean_string(payload.get("event_key"))
            or external_message_id
            or self._build_event_key(
                hook_type=hook_type,
                conversation_external_id=conversation_external_id,
                sender_id=sender_id,
                external_message_id=external_message_id,
                timestamp=timestamp,
                content=content,
            )
        )

        details = {
            "headers_used": sorted((headers or {}).keys()),
            "source_protocol": "workwechat_hook",
            "hook_type": hook_type,
            "content_type": data.get("content_type"),
            "timestamp": timestamp,
            "receiver": data.get("receiver"),
            "is_self_sent": is_self_sent,
            "self_user_id": data.get("self_user_id"),
            # 自发消息是客服手动在企微里发出的出站留痕，不应按入站消息触发插件。
            "skip_plugin_dispatch": is_self_sent,
            "direction": "out" if is_self_sent else "in",
            "local_id": data.get("local_id"),
            "server_id": data.get("server_id"),
            "raw_client_id": payload.get("client_id"),
            # 文件名优先看顶层，其次看 cdn / c2c_cdn 嵌套信息；企微文件消息经常把真实文件名藏在里面。
            "file_name": self._extract_hook_file_name(data),
            "file_size": data.get("file_size") or data.get("size"),
            "download_path": data.get("download_path") or data.get("save_path"),
            "download_error": data.get("download_error"),
            "cdn": data.get("cdn") or data.get("c2c_cdn"),
            "raw_data": data,
        }

        return {
            "adapter_type": self.adapter_type,
            "event_key": event_key,
            "event_type": "message" if message_type not in {"event", "revoke"} else message_type,
            "should_persist_message": bool(content) and message_type != "revoke",
            "conversation": {
                "external_id": conversation_external_id,
                "title": conversation_title,
                "chat_type": "group" if is_group else "direct",
            },
            "message": {
                "content": content,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "message_type": message_type,
                "external_message_id": external_message_id,
            },
            "details": details,
        }

    def _resolve_hook_content(self, data: dict[str, Any], message_type: str) -> str:
        text = self._clean_string(data.get("content") or data.get("text_content") or data.get("desc") or data.get("title"))
        if text:
            return text
        if message_type in {"image", "file", "video", "voice"}:
            file_name = self._extract_hook_file_name(data)
            return f"[{message_type}] {file_name}" if file_name else f"[{message_type}]"
        if message_type == "location":
            title = self._clean_string(data.get("title"))
            address = self._clean_string(data.get("address"))
            return " / ".join(part for part in (title, address) if part) or "[location]"
        if message_type == "revoke":
            return "[message revoked]"
        return ""

    def _build_event_key(
        self,
        *,
        hook_type: int | None,
        conversation_external_id: str,
        sender_id: str | None,
        external_message_id: str | None,
        timestamp: Any,
        content: str,
    ) -> str:
        # Hook 通知不保证每类消息都有稳定 ID，所以把协议类型、会话、发送者、时间和内容合成去重键。
        raw = "|".join(
            [
                str(hook_type or ""),
                str(conversation_external_id or ""),
                str(sender_id or ""),
                str(external_message_id or ""),
                str(timestamp or ""),
                str(content or ""),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _extract_hook_file_name(self, data: dict[str, Any]) -> str:
        cdn = data.get("cdn") if isinstance(data.get("cdn"), dict) else {}
        c2c_cdn = data.get("c2c_cdn") if isinstance(data.get("c2c_cdn"), dict) else {}
        # 这里保持非常保守的兜底顺序，优先用最明确的字段，避免把描述文本误当成文件名。
        return (
            self._clean_string(data.get("file_name") or data.get("filename"))
            or self._clean_string(cdn.get("file_name") or cdn.get("filename"))
            or self._clean_string(c2c_cdn.get("file_name") or c2c_cdn.get("filename"))
            or ""
        )


    def _coerce_int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
