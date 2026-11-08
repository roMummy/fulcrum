from __future__ import annotations

from typing import Any

from app.models.entities import Channel, Conversation

from .base import BaseChannelAdapter


class WechatWeBridgeAdapter(BaseChannelAdapter):
    """把 WeBridge 事件映射成统一消息结构，并限制首期只发送文本。"""

    adapter_type = "wechat_webridge"

    def health_check(self, channel: Channel) -> dict[str, Any]:
        """根据最近注册快照报告手机是否在线，避免云端反向探测手机地址。"""

        from app.services.wechat_webridge.service import _runtime

        snapshot = _runtime(channel).get("webridge")
        if not isinstance(snapshot, dict):
            return {"ok": False, "adapter_type": self.adapter_type, "status": "offline", "summary": "WeBridge 尚未注册"}
        return {
            "ok": bool(snapshot.get("online")),
            "adapter_type": self.adapter_type,
            "status": "online" if snapshot.get("online") else "offline",
            "summary": "WeBridge 最近一次状态已登记" if snapshot.get("online") else "WeBridge 没有在线状态",
            "details": {"device_id": snapshot.get("device_id"), "account_id": snapshot.get("account_id"), "last_seen_at": snapshot.get("last_seen_at")},
        }

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        """返回协议服务待入队的文本命令，真正发送由手机 poll 执行。"""

        from app.services.wechat_webridge.service import enqueue_message_command

        message_type = str(payload.get("message_type") or "text").strip().lower() or "text"
        try:
            job = enqueue_message_command(channel._sa_instance_state.session, channel, conversation, payload)
        except Exception as exc:
            return self.build_result(
                ok=False,
                conversation=conversation,
                message_type=message_type,
                transport_status="error",
                status="failed",
                error_code="webridge_enqueue_failed",
                error_message=str(exc),
            )
        return self.build_result(
            ok=True,
            conversation=conversation,
            message_type=message_type,
            transport_status="queued",
            status="pending",
            external_status="pending",
            extra={"job_id": int(job.id), "command_type": message_type},
        )

    def normalize_inbound_payload(self, channel: Channel, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """兼容 WeBridge 事件的常见字段名，输出主服务统一入站结构。"""

        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        conversation_id = str(message.get("conversation_id") or message.get("chat_id") or message.get("from") or "manual").strip()
        content = str(message.get("content") or message.get("text") or message.get("message") or "")
        is_group = bool(message.get("is_group") or str(conversation_id).endswith("@chatroom"))
        return {
            "adapter_type": self.adapter_type,
            "event_key": str(payload.get("event_id") or payload.get("message_id") or payload.get("id") or "").strip() or None,
            "event_type": str(payload.get("event_type") or payload.get("type") or "message").strip() or "message",
            "should_persist_message": bool(content.strip()),
            "conversation": {
                "external_id": conversation_id,
                "title": message.get("conversation_title") or message.get("chat_name") or conversation_id,
                "chat_type": "group" if is_group else "direct",
            },
            "message": {
                "content": content,
                "sender_id": str(message.get("sender_id") or message.get("from") or "").strip() or None,
                "sender_name": message.get("sender_name") or message.get("nickname"),
                "message_type": str(message.get("message_type") or "text").strip() or "text",
                "external_message_id": str(message.get("message_id") or payload.get("message_id") or "").strip() or None,
            },
            "details": {"headers_used": sorted((headers or {}).keys())},
        }
