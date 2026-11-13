from __future__ import annotations

from typing import Any

from app.models.entities import Channel, Conversation
from app.services.message_view import recall_info

from .base import BaseChannelAdapter


def _command_request_id(job: Any) -> str:
    """出站命令任务里保存的 request_id;读不到(例如测试替身)返回空串。"""

    from app.services.task_queue import parse_job_payload

    try:
        return str((parse_job_payload(job).get("command") or {}).get("request_id") or "")
    except Exception:
        return ""


class WechatWeBridgeAdapter(BaseChannelAdapter):
    """把 WeBridge 事件和统一出站消息映射到长轮询命令队列。"""

    adapter_type = "wechat_webridge"

    def health_check(self, channel: Channel) -> dict[str, Any]:
        """复用账号清单的在线判定，让过期快照和停用状态及时生效。"""

        from app.services.account_presence import build_account_presence

        presence = build_account_presence(channel)
        online = presence["state"] == "online"
        last_active_at = presence["last_active_at"]
        return {
            "ok": online,
            "adapter_type": self.adapter_type,
            "status": "online" if online else "offline",
            "summary": "设备在线，连接正常" if online else presence["hint"],
            "details": {"last_seen_at": last_active_at.isoformat() if last_active_at is not None else None},
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
            # 命令的 request_id 记在消息上,手机回报结果时据此找回这条消息并更新状态(见 service.settle_outbound_message)。
            raw_request={"job_id": int(job.id), "request_id": _command_request_id(job)},
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
            "group_member_nickname_change": "group_member_nickname_change",
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
            "create_time": body.get("create_time"),
            "mention_user_list": body.get("at_user_list") if isinstance(body.get("at_user_list"), list) else [],
            "quote_message_id": body.get("quote_msg_id"),
            "media": {key: body.get(key) for key in ("image_md5", "image_aes_key", "image_thumb_url", "image_mid_url", "voice_url", "voice_aes_key", "emoji_cdn_url", "app_msg_url", "app_thumb_url") if body.get(key)},
            "payment": {key: body.get(key) for key in ("red_packet_id", "transfer_id", "message_id", "from_user", "to_user", "payer_username", "receiver_username", "amount_fen", "amount_text", "control_transaction_id") if body.get(key) is not None},
            "contact": {key: body.get(key) for key in ("contact_id", "contact_ref", "encrypt_username", "ticket", "nickname") if body.get(key) is not None},
            "group": {key: body.get(key) for key in ("room_id", "member_id", "member_ids", "group_name", "room_name", "room_remark", "operation") if body.get(key) is not None},
            "moment": {key: body.get(key) for key in ("sns_id", "media_id", "author_id", "operation_id") if body.get(key) is not None},
        }
        own_account = _own_account_id(channel, payload)
        if message_type == "pat":
            # 拍一拍:按协议文档 4.3.3 的结构化字段(拍的人 / 被拍的人的 wxid)整理成统一结构,
            # 触发规则只读 details.pat;不解析文案,文案里的昵称会随群昵称变化、没有稳定性
            details["pat"] = _build_pat_info(body, own_account)
        sender_for_self = str(body.get("sender_id") or body.get("from") or "")
        if message_type == "pat":
            sender_for_self = str(body.get("pat_from_user") or sender_for_self)  # 拍的人是本账号(自己拍别人)也算自发
        if own_account and sender_for_self.strip() == own_account:
            # 本账号自己发出的消息(手机上亲自发的、机器人回复后回流的都算):标记出来,全局不进入内容检测
            details["from_self"] = True
            details["direction"] = "out"
        member_change = _build_member_change(event_name, body)
        if member_change:
            # 结构化成员事件:插件直接读 details.member_change,消息内容写成中文,后台和日志可读。
            details["member_change"] = member_change
            details["member_profile"] = {"nickname": body.get("member_nick_name")}
            content = content or member_change["summary"]
        timestamp = body.get("create_time", payload.get("create_time"))
        if timestamp is not None:
            details["timestamp"] = timestamp
        is_system_event = raw_type == "system" or str(body.get("raw_message_type") or "") == "10002"
        recalled = (recall_info(content) or recall_info(body.get("content_xml"))) if is_system_event else None
        if recalled is not None:
            # 撤回通知是消息状态更新，不交给插件和 AI 当成新发言处理。
            details["recall"] = recalled
            details["skip_plugin_dispatch"] = True
            message_type = "system"
            event_name = "message_recalled"
            content = recalled["text"]
            conversation_id = recalled["session"] or conversation_id
            is_group = is_group or conversation_id.endswith("@chatroom")
        event_key = str(payload.get("event_id") or body.get("event_id") or body.get("message_id") or body.get("id") or "").strip() or None
        if recalled is not None and event_key:
            # 有的客户端用原消息编号标识通知；分开原消息与撤回回调的去重键。
            event_key = "recall:" + event_key
        return {
            "adapter_type": self.adapter_type,
            "event_key": event_key,
            "event_type": event_name,
            "should_persist_message": bool(content.strip()) or message_type in {"red_packet", "transfer", "pat", "system", "friend_request", "contact", "group_member_join", "group_member_leave", "group_member_nickname_change", "moments", "favorite", "official_account"},
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


_MEMBER_EVENT_TYPES = {"group_member_join": "join", "group_member_leave": "left", "group_member_nickname_change": "rename"}


def _own_account_id(channel: Channel | None, payload: dict[str, Any]) -> str:
    """这个渠道登录的微信账号 ID:优先用手机注册时登记的,其次用事件信封里的 account_id。

    微桥会把本账号发出的消息也当入站事件推回来;不识别的话,机器人自己的回复会被当成
    用户消息再喂给插件和 AI,形成自问自答的循环。
    """

    if channel is not None:
        from app.services.wechat_webridge.service import _runtime

        bridge = _runtime(channel).get("webridge")
        registered = str(bridge.get("account_id") or "").strip() if isinstance(bridge, dict) else ""
        if registered:
            return registered
    return str(payload.get("account_id") or "").strip()


def _build_pat_info(body: dict[str, Any], own_account: str) -> dict[str, Any]:
    """整理拍一拍事件(协议文档 4.3.3):拍的人 pat_from_user、被拍的人 pat_patted_user,都是 wxid。

    文档没有"被拍的是不是机器人"的字段,要拿 pat_patted_user 和本账号 ID 比较:
    - 两个 ID 都有且本账号 ID 已知,才给出明确的 True/False;
    - 缺任何一个就不给(None),"只回应拍机器人"的规则不触发,宁可不回也不乱回。
    """

    from_id = str(body.get("pat_from_user") or "").strip()
    patted_id = str(body.get("pat_patted_user") or "").strip()
    info: dict[str, Any] = {"from_id": from_id, "patted_id": patted_id}
    if patted_id and own_account:
        info["patted_is_me"] = patted_id == own_account
    return info


def _build_member_change(event_name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """把微桥群成员事件转成通用 member_change 结构(join / left / rename)。"""

    change_type = _MEMBER_EVENT_TYPES.get(event_name)
    member_id = str(body.get("member_id") or "").strip()
    if not change_type or not member_id:
        return None
    name = str(body.get("member_display_name") or body.get("member_nick_name") or member_id).strip() or member_id
    result: dict[str, Any] = {
        "change_type": change_type,
        "members": [{"external_id": member_id, "display_name": name}],
        "after_count": body.get("snapshot_after_count"),
    }
    if change_type == "join":
        result["summary"] = f"{name} 加入了群聊"
    elif change_type == "left":
        result["summary"] = f"{name} 退出了群聊"
    else:
        if not isinstance(body.get("new_room_nick_name"), str):
            return None
        new_name = str(body.get("new_room_nick_name") or "").strip()
        result["new_room_nick_name"] = new_name
        if isinstance(body.get("old_room_nick_name"), str):
            result["old_room_nick_name"] = body["old_room_nick_name"]
        result["summary"] = f"{name} 将群昵称改为 {new_name}" if new_name else f"{name} 清除了群昵称"
    return result
