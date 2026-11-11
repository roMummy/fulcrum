from __future__ import annotations

import json
from typing import Any
from urllib import parse as urlparse
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.core.logging import get_logger
from app.models.entities import Channel, Conversation
from app.services.security import decrypt_secret

from .base import BaseChannelAdapter


DEFAULT_TIMEOUT_SECONDS = 12
logger = get_logger(__name__)


class GenericHttpAdapter(BaseChannelAdapter):
    adapter_type = "generic_http"

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        message_type = str(payload.get("message_type") or "text").strip().lower() or "text"
        if not channel.api_base:
            logger.error("generic adapter missing api_base", extra={"channel_id": channel.id, "adapter_type": self.adapter_type})
            return self.build_result(
                ok=False,
                conversation=conversation,
                message_type=message_type,
                transport_status="error",
                status="failed",
                error_code="missing_api_base",
                error_message="渠道未配置 API Base",
            )

        adapter_config = self._load_adapter_config(channel)
        target_url = self._build_target_url(channel, payload, adapter_config)
        request_body = self._build_request_body(channel, conversation, payload)
        headers = self._build_headers(channel, payload, adapter_config)
        timeout = self._resolve_timeout(payload, adapter_config)
        raw_request = {
            "url": target_url,
            "method": "POST",
            "json": request_body,
            "headers": headers,
        }
        logger.info(
            "generic adapter sending request",
            extra={"channel_id": channel.id, "adapter_type": self.adapter_type, "target_url": target_url, "message_type": message_type},
        )

        try:
            raw_response = self._post_json(target_url, request_body, headers=headers, timeout=timeout)
        except Exception as exc:
            logger.exception(
                "generic adapter request failed",
                extra={"channel_id": channel.id, "adapter_type": self.adapter_type, "target_url": target_url},
            )
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
            logger.error(
                "generic adapter response indicates failure",
                extra={"channel_id": channel.id, "adapter_type": self.adapter_type, "target_url": target_url},
            )
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
                extra={"endpoint": urlparse.urlparse(target_url).path or "/"},
            )

        logger.info(
            "generic adapter request succeeded",
            extra={"channel_id": channel.id, "adapter_type": self.adapter_type, "target_url": target_url},
        )
        return self.build_result(
            ok=True,
            conversation=conversation,
            message_type=message_type,
            transport_status="sent",
            status="sent",
            external_status=self._pick_response_status(raw_response),
            external_message_id=self._extract_external_message_id(raw_response),
            raw_request=raw_request,
            raw_response=raw_response,
            extra={
                "endpoint": urlparse.urlparse(target_url).path or "/",
                "request_timeout": timeout,
            },
        )

    def _load_adapter_config(self, channel: Channel) -> dict[str, Any]:
        raw = (channel.adapter_config_json or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _build_target_url(self, channel: Channel, payload: dict[str, Any], adapter_config: dict[str, Any]) -> str:
        base_url = str(channel.api_base or "").rstrip("/")
        path = (
            str(payload.get("endpoint") or "").strip()
            or str(adapter_config.get("outbound_path") or "").strip()
            or str(adapter_config.get("send_path") or "").strip()
            or str(channel.webhook_path or "").strip()
            or "/"
        )
        if path.startswith("http://") or path.startswith("https://"):
            return path
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{base_url}{normalized_path}"

    def _build_request_body(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        body = {
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "conversation_id": conversation.id,
            "conversation_external_id": conversation.external_id,
            "receiver_id": str(payload.get("receiver_id") or payload.get("external_id") or conversation.external_id or ""),
            "message_type": str(payload.get("message_type") or "text"),
            "content": str(payload.get("content") or ""),
            "payload": dict(payload),
        }
        for key in (
            "title",
            "chat_type",
            "xml",
            "base64",
            "image_base64",
            "file_base64",
            "file_name",
            "file_url",
            "download_url",
            "image_url",
            "file_path",
            "image_path",
            "local_path",
            "path",
            "url",
            "desc",
            "username",
            "appid",
            "appname",
            "appicon",
            "page_path",
            "pagepath",
            "file_id",
            "aes_key",
            "md5",
            "size",
        ):
            value = payload.get(key)
            if value not in (None, ""):
                body[key] = value
        return body

    def _build_headers(self, channel: Channel, payload: dict[str, Any], adapter_config: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "fulcrum/1.0",
        }
        token = decrypt_secret(channel.bot_token_encrypted)
        authorization = str(adapter_config.get("authorization") or payload.get("authorization") or "").strip()
        if authorization:
            headers["Authorization"] = authorization
        elif token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Bot-Token"] = token
        extra_headers = adapter_config.get("headers")
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                normalized_key = str(key).strip()
                normalized_value = str(value).strip()
                if normalized_key and normalized_value:
                    headers[normalized_key] = normalized_value
        return headers

    def _resolve_timeout(self, payload: dict[str, Any], adapter_config: dict[str, Any]) -> int:
        value = payload.get("timeout") or adapter_config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        return timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS

    def _post_json(self, url: str, body: dict[str, Any], *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(url, data=data, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urlrequest.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return self._parse_response_body(raw, getattr(response, "status", None), getattr(response, "url", url))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return self._parse_response_body(raw, exc.code, url)
        except URLError as exc:
            raise RuntimeError(str(exc.reason or exc)) from exc

    def _parse_response_body(self, raw: str, http_status: int | None, url: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"text": raw}
        if not isinstance(parsed, dict):
            parsed = {"data": parsed}
        parsed.setdefault("http_status", http_status or 200)
        parsed.setdefault("url", url)
        return parsed

    def _response_indicates_success(self, response: dict[str, Any]) -> bool:
        http_status = int(response.get("http_status") or 200)
        if http_status >= 400:
            return False
        for key in ("ok", "success"):
            value = response.get(key)
            if value is not None:
                return bool(value)
        status = str(response.get("status") or response.get("code") or "").strip().lower()
        if status in {"error", "failed", "fail"}:
            return False
        return True

    def _extract_error_code(self, response: dict[str, Any]) -> str:
        for key in ("error_code", "code", "status"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return "request_failed"

    def _extract_error_message(self, response: dict[str, Any]) -> str:
        for key in ("error", "message", "detail", "text"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return "generic outbound request failed"

    def _pick_response_status(self, response: dict[str, Any]) -> str:
        for key in ("status", "message", "result"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return "sent"

    def _extract_external_message_id(self, response: dict[str, Any]) -> str | None:
        for key in ("external_message_id", "message_id", "msg_id", "id"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return None
