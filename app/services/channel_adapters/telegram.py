from __future__ import annotations

import base64
import json
import uuid
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.models.entities import Channel, Conversation
from app.services.security import decrypt_secret

from .base import BaseChannelAdapter


DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAdapter(BaseChannelAdapter):
    adapter_type = "telegram"

    def send_chat_action(
        self,
        channel: Channel,
        conversation: Conversation,
        *,
        action: str = "typing",
    ) -> dict[str, Any]:
        token = decrypt_secret(channel.bot_token_encrypted)
        chat_id = str(conversation.external_id or "").strip()
        if not token:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "action": "send_chat_action",
                "status": "failed",
                "error_code": "missing_bot_token",
                "error_message": "Telegram Bot Token 缺失",
            }
        if not chat_id:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "action": "send_chat_action",
                "status": "failed",
                "error_code": "missing_chat_id",
                "error_message": "Telegram chat_id 缺失",
            }
        target_url = self._build_api_url(channel, token, "sendChatAction")
        body = {"chat_id": chat_id, "action": str(action or "typing").strip() or "typing"}
        try:
            raw_response = self._post_json(target_url, body)
        except Exception as exc:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "action": "send_chat_action",
                "status": "failed",
                "error_code": "request_failed",
                "error_message": str(exc),
            }
        if not self._response_indicates_success(raw_response):
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "action": "send_chat_action",
                "status": "failed",
                "error_code": self._extract_error_code(raw_response),
                "error_message": self._extract_error_message(raw_response),
            }
        return {
            "ok": True,
            "adapter_type": self.adapter_type,
            "action": "send_chat_action",
            "status": "sent",
            "external_status": "ok",
        }

    def fetch_updates(
        self,
        channel: Channel,
        *,
        offset: int | None = None,
        limit: int = 20,
        timeout: int = 0,
    ) -> dict[str, Any]:
        token = decrypt_secret(channel.bot_token_encrypted)
        if not token:
            return {
                "ok": False,
                "http_status": 400,
                "error_code": "missing_bot_token",
                "description": "Telegram Bot Token 缺失",
            }
        body: dict[str, Any] = {
            "limit": max(1, min(limit, 100)),
            "timeout": max(0, min(timeout, 50)),
        }
        if offset is not None:
            body["offset"] = int(offset)
        target_url = self._build_api_url(channel, token, "getUpdates")
        return self._post_json(target_url, body)

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        message_type = str(payload.get("message_type") or "text").strip().lower() or "text"
        token = decrypt_secret(channel.bot_token_encrypted)
        chat_id = str(payload.get("receiver_id") or payload.get("external_id") or conversation.external_id or "").strip()

        if not token:
            return self._error_result(conversation, message_type, "missing_bot_token", "Telegram Bot Token 缺失")
        if not chat_id:
            return self._error_result(conversation, message_type, "missing_chat_id", "Telegram chat_id 缺失")

        request_spec = self._build_request(message_type, chat_id, payload)
        if not request_spec:
            return self._error_result(
                conversation,
                message_type,
                "invalid_payload",
                self._build_payload_error_message(message_type),
            )

        method_name = str(request_spec["method"])
        body = request_spec["body"]
        request_kind = str(request_spec.get("request_kind") or "json").strip() or "json"
        target_url = self._build_api_url(channel, token, method_name)
        raw_request = {
            "url": self._redact_token(target_url, token),
            "method": "POST",
            "json": body if request_kind == "json" else None,
            "multipart": request_kind == "multipart",
        }

        try:
            if request_kind == "multipart":
                raw_response = self._post_multipart(target_url, body, request_spec.get("files") or {})
            else:
                raw_response = self._post_json(target_url, body)
        except Exception as exc:
            return self.build_result(
                ok=False,
                conversation=conversation,
                message_type=message_type,
                transport_status="error",
                status="failed",
                raw_request=raw_request,
                error_code="request_failed",
                error_message=str(exc),
            )

        if not self._response_indicates_success(raw_response):
            return self.build_result(
                ok=False,
                conversation=conversation,
                message_type=message_type,
                transport_status="error",
                status="failed",
                raw_request=raw_request,
                raw_response=raw_response,
                error_code=self._extract_error_code(raw_response),
                error_message=self._extract_error_message(raw_response),
                extra={"telegram_method": method_name},
            )

        result = raw_response.get("result")
        result_payload = result if isinstance(result, dict) else {}
        return self.build_result(
            ok=True,
            conversation=conversation,
            message_type=message_type,
            transport_status="sent",
            status="sent",
            external_status="ok",
            external_message_id=self._clean_string(result_payload.get("message_id")),
            raw_request=raw_request,
            raw_response=raw_response,
            extra={
                "telegram_method": method_name,
                "chat_id": chat_id,
            },
        )

    def normalize_inbound_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        update_id = self._clean_string(payload.get("update_id"))
        event_type, event_payload = self._resolve_update_payload(payload)
        chat_payload = self._extract_chat_payload(event_payload)
        sender_payload = self._extract_sender_payload(event_payload)

        content = self._extract_message_content(event_type, event_payload)
        conversation_external_id = self._clean_string(chat_payload.get("id")) or "manual"
        external_message_id = self._extract_external_message_id(event_type, event_payload)
        event_key = update_id or external_message_id

        details: dict[str, Any] = {
            "headers_used": sorted((headers or {}).keys()),
            "update_id": update_id,
            "telegram_chat_type": self._clean_string(chat_payload.get("type")),
            "telegram_chat_id": self._clean_string(chat_payload.get("id")),
        }
        member_change = self._extract_member_change(event_type, event_payload)
        if member_change is not None:
            # Telegram 的 chat_member 是渠道私有结构。进入平台后只保留通用成员
            # 变更语义，插件不需要了解 old_chat_member/new_chat_member 字段。
            details["member_change"] = member_change

        return {
            "adapter_type": self.adapter_type,
            "event_key": event_key,
            "event_type": event_type,
            "should_persist_message": bool(content),
            "conversation": {
                "external_id": conversation_external_id,
                "title": self._resolve_conversation_title(chat_payload, sender_payload),
                "chat_type": self._resolve_chat_type(chat_payload.get("type")),
            },
            "message": {
                "content": content,
                "sender_id": self._clean_string(sender_payload.get("id")),
                "sender_name": self._resolve_sender_name(sender_payload),
                "message_type": self._resolve_message_type(event_type, event_payload),
                "external_message_id": external_message_id,
            },
            "details": details,
        }

    def _extract_member_change(self, event_type: str, event_payload: dict[str, Any]) -> dict[str, Any] | None:
        """把 Telegram 成员状态机转换为平台统一的 join/left/removed 事件。"""

        if event_type != "chat_member":
            return None
        old_state = event_payload.get("old_chat_member")
        new_state = event_payload.get("new_chat_member")
        if not isinstance(old_state, dict) or not isinstance(new_state, dict):
            return None
        old_active = self._telegram_member_is_active(old_state)
        new_active = self._telegram_member_is_active(new_state)
        new_status = self._clean_string(new_state.get("status")) or ""
        if not old_active and new_active:
            change_type = "join"
        elif old_active and new_status == "kicked":
            change_type = "removed"
        elif old_active and not new_active:
            change_type = "left"
        else:
            # 管理员升降级、禁言和其他仍在群内的状态变化不是成员增减。
            return None

        user_payload = new_state.get("user") if isinstance(new_state.get("user"), dict) else old_state.get("user")
        if not isinstance(user_payload, dict):
            return None
        external_id = self._clean_string(user_payload.get("id"))
        if not external_id:
            return None
        return {
            "change_type": change_type,
            "members": [
                {
                    "external_id": external_id,
                    "display_name": self._resolve_sender_name(user_payload) or external_id,
                }
            ],
        }

    def _telegram_member_is_active(self, member_state: dict[str, Any]) -> bool:
        """判断成员状态是否仍属于群，restricted 需要同时检查 is_member。"""

        status = self._clean_string(member_state.get("status")) or ""
        if status in {"creator", "administrator", "member"}:
            return True
        if status == "restricted":
            return bool(member_state.get("is_member"))
        return False

    def health_check(self, channel: Channel) -> dict[str, Any]:
        token = decrypt_secret(channel.bot_token_encrypted)
        if not token:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": "Telegram Bot Token 缺失",
                "details": {"reason": "missing_bot_token"},
            }

        get_me_url = self._build_api_url(channel, token, "getMe")
        get_webhook_url = self._build_api_url(channel, token, "getWebhookInfo")
        try:
            me_response = self._post_json(get_me_url, {})
            webhook_response = self._post_json(get_webhook_url, {})
        except Exception as exc:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": f"Telegram API 请求失败: {exc}",
                "details": {"reason": "request_failed"},
            }

        if not self._response_indicates_success(me_response):
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": self._extract_error_message(me_response),
                "details": {
                    "reason": "get_me_rejected",
                    "error_code": self._extract_error_code(me_response),
                },
            }

        webhook_result = webhook_response.get("result")
        webhook_info = webhook_result if isinstance(webhook_result, dict) else {}
        me_result = me_response.get("result")
        bot_info = me_result if isinstance(me_result, dict) else {}
        pending_updates = int(webhook_info.get("pending_update_count") or 0)
        last_error_message = self._clean_string(webhook_info.get("last_error_message"))
        status = "online"
        summary = f"Telegram bot @{bot_info.get('username') or 'unknown'} 可用"
        if last_error_message:
            status = "degraded"
            summary = f"{summary}，Webhook 最近报错: {last_error_message}"
        elif pending_updates > 0:
            status = "degraded"
            summary = f"{summary}，Webhook 待处理更新 {pending_updates}"

        return {
            "ok": status == "online",
            "adapter_type": self.adapter_type,
            "status": status,
            "summary": summary,
            "details": {
                "bot_id": bot_info.get("id"),
                "username": bot_info.get("username"),
                "can_join_groups": bot_info.get("can_join_groups"),
                "supports_inline_queries": bot_info.get("supports_inline_queries"),
                "webhook_url": webhook_info.get("url"),
                "pending_update_count": pending_updates,
                "last_error_message": last_error_message,
            },
        }

    def _build_request(self, message_type: str, chat_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if message_type == "text":
            text = str(payload.get("content") or "").strip()
            if not text:
                return None
            body = {
                "chat_id": chat_id,
                "text": text,
            }
            parse_mode = self._clean_string(payload.get("parse_mode"))
            if parse_mode:
                body["parse_mode"] = parse_mode
            disable_preview = payload.get("disable_web_page_preview")
            if disable_preview is not None:
                body["disable_web_page_preview"] = bool(disable_preview)
            return {"method": "sendMessage", "body": body}

        if message_type == "image":
            base64_payload = self._clean_string(payload.get("base64") or payload.get("image_base64"))
            if base64_payload:
                photo_file = self._decode_base64_file(base64_payload, "image.jpg", "image/jpeg")
                body = {"chat_id": chat_id}
                caption = self._clean_string(payload.get("content") or payload.get("caption"))
                if caption:
                    body["caption"] = caption
                return {
                    "method": "sendPhoto",
                    "request_kind": "multipart",
                    "body": body,
                    "files": {"photo": photo_file},
                }
            photo = self._clean_string(payload.get("file_id") or payload.get("photo") or payload.get("image_url") or payload.get("url"))
            if not photo:
                return None
            body = {
                "chat_id": chat_id,
                "photo": photo,
            }
            caption = self._clean_string(payload.get("content") or payload.get("caption"))
            if caption:
                body["caption"] = caption
            return {"method": "sendPhoto", "body": body}

        return None

    def _build_payload_error_message(self, message_type: str) -> str:
        if message_type == "text":
            return "Telegram text 消息缺少 content"
        if message_type == "image":
            return "Telegram image 消息需要 base64/image_base64 或 file_id/photo/image_url/url"
        return f"Telegram 暂不支持消息类型: {message_type}"

    def _build_api_url(self, channel: Channel, token: str, method_name: str) -> str:
        base = str(channel.api_base or DEFAULT_TELEGRAM_API_BASE).strip().rstrip("/")
        if "{token}" in base:
            api_base = base.format(token=token)
        elif base.endswith(f"/bot{token}"):
            api_base = base
        elif base.endswith("/bot"):
            api_base = f"{base}{token}"
        else:
            api_base = f"{base}/bot{token}"
        return f"{api_base}/{method_name}"

    def _redact_token(self, value: str, token: str) -> str:
        return value.replace(token, "<redacted>")

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return self._parse_json_like(raw, getattr(response, "status", 200))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            return self._parse_json_like(body_text, exc.code)
        except URLError as exc:
            raise RuntimeError(str(exc.reason or exc)) from exc

    def _post_multipart(self, url: str, body: dict[str, Any], files: dict[str, dict[str, Any]]) -> dict[str, Any]:
        boundary = f"----CodexBoundary{uuid.uuid4().hex}"
        data = self._build_multipart_body(body, files, boundary)
        req = urlrequest.Request(url, data=data, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return self._parse_json_like(raw, getattr(response, "status", 200))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            return self._parse_json_like(body_text, exc.code)
        except URLError as exc:
            raise RuntimeError(str(exc.reason or exc)) from exc

    def _build_multipart_body(self, body: dict[str, Any], files: dict[str, dict[str, Any]], boundary: str) -> bytes:
        chunks: list[bytes] = []
        for key, value in body.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )
        for key, spec in files.items():
            filename = str(spec.get("filename") or "upload.bin")
            content_type = str(spec.get("content_type") or "application/octet-stream")
            content = spec.get("content") or b""
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                    bytes(content),
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks)

    def _decode_base64_file(self, payload: str, filename: str, content_type: str) -> dict[str, Any]:
        normalized = payload.strip()
        detected_type = content_type
        if normalized.startswith("data:") and "," in normalized:
            header, normalized = normalized.split(",", 1)
            media_type = header.removeprefix("data:").split(";", 1)[0].strip()
            if media_type:
                detected_type = media_type
        extension = self._guess_extension(detected_type)
        return {
            "filename": self._replace_extension(filename, extension),
            "content_type": detected_type,
            "content": base64.b64decode(normalized),
        }

    def _guess_extension(self, content_type: str) -> str:
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        return mapping.get(str(content_type or "").strip().lower(), ".jpg")

    def _replace_extension(self, filename: str, extension: str) -> str:
        base_name = str(filename or "upload").strip() or "upload"
        dot_index = base_name.rfind(".")
        if dot_index > 0:
            base_name = base_name[:dot_index]
        return f"{base_name}{extension}"

    def _parse_json_like(self, raw: str, status: int) -> dict[str, Any]:
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        if not isinstance(parsed, dict):
            parsed = {"data": parsed}
        parsed.setdefault("http_status", status)
        return parsed

    def _response_indicates_success(self, response: dict[str, Any]) -> bool:
        if int(response.get("http_status") or 200) >= 400:
            return False
        return bool(response.get("ok"))

    def _extract_error_code(self, response: dict[str, Any]) -> str:
        value = response.get("error_code")
        if value not in (None, ""):
            return str(value)
        return "provider_error"

    def _extract_error_message(self, response: dict[str, Any]) -> str:
        description = response.get("description")
        if description not in (None, ""):
            return str(description)
        return "telegram provider returned an error"

    def _resolve_update_payload(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        for event_type in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "callback_query",
            "my_chat_member",
            "chat_member",
        ):
            value = payload.get(event_type)
            if isinstance(value, dict):
                return event_type, value
        return "unknown", payload

    def _extract_chat_payload(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(event_payload.get("chat"), dict):
            return event_payload["chat"]
        if isinstance(event_payload.get("message"), dict) and isinstance(event_payload["message"].get("chat"), dict):
            return event_payload["message"]["chat"]
        return {}

    def _extract_sender_payload(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(event_payload.get("from"), dict):
            return event_payload["from"]
        if isinstance(event_payload.get("message"), dict) and isinstance(event_payload["message"].get("from"), dict):
            return event_payload["message"]["from"]
        return {}

    def _extract_message_content(self, event_type: str, event_payload: dict[str, Any]) -> str:
        if event_type == "callback_query":
            return self._clean_string(event_payload.get("data")) or ""
        for key in ("text", "caption"):
            value = self._clean_string(event_payload.get(key))
            if value:
                return value
        message_payload = event_payload.get("message")
        if isinstance(message_payload, dict):
            for key in ("text", "caption"):
                value = self._clean_string(message_payload.get(key))
                if value:
                    return value
        return ""

    def _resolve_message_type(self, event_type: str, event_payload: dict[str, Any]) -> str:
        if event_type == "callback_query":
            return "callback"
        if event_payload.get("photo"):
            return "image"
        if event_payload.get("document"):
            return "file"
        if event_payload.get("voice"):
            return "voice"
        if event_payload.get("video"):
            return "video"
        return "text" if self._extract_message_content(event_type, event_payload) else "event"

    def _extract_external_message_id(self, event_type: str, event_payload: dict[str, Any]) -> str | None:
        if event_type == "callback_query":
            message_payload = event_payload.get("message")
            if isinstance(message_payload, dict):
                return self._clean_string(message_payload.get("message_id")) or self._clean_string(event_payload.get("id"))
            return self._clean_string(event_payload.get("id"))
        return self._clean_string(event_payload.get("message_id"))

    def _resolve_chat_type(self, value: Any) -> str:
        normalized = self._clean_string(value) or ""
        if normalized in {"group", "supergroup", "channel"}:
            return "group"
        return "direct"

    def _resolve_conversation_title(self, chat_payload: dict[str, Any], sender_payload: dict[str, Any]) -> str | None:
        for key in ("title", "username", "first_name"):
            value = self._clean_string(chat_payload.get(key))
            if value:
                return value
        return self._resolve_sender_name(sender_payload)

    def _resolve_sender_name(self, sender_payload: dict[str, Any]) -> str | None:
        first_name = self._clean_string(sender_payload.get("first_name"))
        last_name = self._clean_string(sender_payload.get("last_name"))
        full_name = " ".join(part for part in (first_name, last_name) if part)
        if full_name:
            return full_name
        return self._clean_string(sender_payload.get("username"))

    def _clean_string(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = str(value).strip()
        return normalized or None

    def _error_result(
        self,
        conversation: Conversation,
        message_type: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        return self.build_result(
            ok=False,
            conversation=conversation,
            message_type=message_type,
            transport_status="error",
            status="failed",
            error_code=error_code,
            error_message=error_message,
        )
