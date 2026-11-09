from __future__ import annotations

from typing import Any

from app.models.entities import Channel, Conversation

from .base import BaseChannelAdapter


class WechatWeBridgeAdapter(BaseChannelAdapter):
    """把 WeBridge 事件和统一出站消息映射到长轮询命令队列。"""

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
        """解析 WeBridge 的 event_name/body 信封，覆盖文本、媒体、红包和转账事件。"""

        body = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        event_name = str(payload.get("event_name") or payload.get("event_type") or body.get("event_name") or "message").strip() or "message"
        raw_type = str(body.get("msg_type") or body.get("message_type") or "text").strip().lower() or "text"
        # WeBridge 将文件、链接、小程序等放在 app 类型下，用 msg_subtype 区分子类型。
        message_type = {"contact_card": "card", "pat": "pat", "system": "system"}.get(raw_type, raw_type)
        if raw_type == "app":
            message_type = str(body.get("msg_subtype") or "app").strip().lower() or "app"
        event_message_types = {
            "friend_request": "friend_request",
            "friend_added": "contact",
            "group_member_join": "group_member_join",
            "group_member_leave": "group_member_leave",
            "moments_received": "moments",
            "favorite_received": "favorite",
            "official_account_message": "official_account",
        }
        if event_name in event_message_types:
            message_type = event_message_types[event_name]
        elif event_name == "red_packet_received":
            message_type = "red_packet"
        elif event_name == "transfer_received":
            message_type = "transfer"
        conversation_id = str(body.get("chat_id") or body.get("room_id") or body.get("from") or "manual").strip() or "manual"
        content = str(body.get("content") or body.get("text") or body.get("message") or "")
        is_group = bool(body.get("is_group") or body.get("room_id") or str(conversation_id).endswith("@chatroom"))
        details = {
            "headers_used": sorted((headers or {}).keys()),
            "event_name": event_name,
            "raw_message_type": body.get("raw_message_type"),
            "msg_subtype": body.get("msg_subtype"),
            # 微桥 is_at 表示本条消息是否 @ 了当前登录账号，插件匹配与 AI 调度据此判断是否被呼叫。
            "mentioned_self": bool(body.get("is_at")),
            "content_xml": body.get("content_xml"),
            "media": {key: body.get(key) for key in ("image_md5", "image_aes_key", "image_thumb_url", "image_mid_url", "voice_url", "voice_aes_key", "emoji_cdn_url", "app_msg_url", "app_thumb_url") if body.get(key)},
            "payment": {key: body.get(key) for key in ("red_packet_id", "transfer_id", "amount_fen", "amount_text", "control_transaction_id") if body.get(key) is not None},
            "contact": {key: body.get(key) for key in ("contact_id", "contact_ref", "encrypt_username", "ticket", "nickname") if body.get(key) is not None},
            "group": {key: body.get(key) for key in ("room_id", "member_id", "member_ids", "group_name", "operation") if body.get(key) is not None},
            "moment": {key: body.get(key) for key in ("sns_id", "media_id", "author_id", "operation_id") if body.get(key) is not None},
        }
        return {
            "adapter_type": self.adapter_type,
            "event_key": str(payload.get("event_id") or body.get("event_id") or body.get("message_id") or body.get("id") or "").strip() or None,
            "event_type": event_name,
            "should_persist_message": bool(content.strip()) or message_type in {"red_packet", "transfer", "pat", "system", "friend_request", "contact", "group_member_join", "group_member_leave", "moments", "favorite", "official_account"},
            "conversation": {
                "external_id": conversation_id,
                "title": body.get("conversation_title") or body.get("chat_name") or conversation_id,
                "chat_type": "group" if is_group else "direct",
            },
            "message": {
                "content": content or (body.get("pat_template") or body.get("app_msg_title") or event_name),
                "sender_id": str(body.get("sender_id") or body.get("from") or "").strip() or None,
                "sender_name": body.get("sender_name") or body.get("nickname") or body.get("nick_name"),
                "message_type": message_type,
                "external_message_id": str(body.get("message_id") or payload.get("event_id") or "").strip() or None,
            },
            "details": details,
        }

