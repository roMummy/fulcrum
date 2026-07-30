from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


JOIN_PATTERNS = (
    '"$names$"\u52a0\u5165\u4e86\u7fa4\u804a',
    '"$username$"\u9080\u8bf7"$names$"\u52a0\u5165\u4e86\u7fa4\u804a',
    '\u4f60\u9080\u8bf7"$names$"\u52a0\u5165\u4e86\u7fa4\u804a',
    '"$adder$"\u901a\u8fc7\u626b\u63cf"$from$"\u5206\u4eab\u7684\u4e8c\u7ef4\u7801\u52a0\u5165\u7fa4\u804a',
    '"$adder$"\u901a\u8fc7"$from$"\u7684\u9080\u8bf7\u4e8c\u7ef4\u7801\u52a0\u5165\u7fa4\u804a',
    "joined the group",
    "joined the group chat",
)
LEAVE_PATTERNS = (
    '"$username$"\u9000\u51fa\u4e86\u7fa4\u804a',
    '"$username$"\u79bb\u5f00\u4e86\u7fa4\u804a',
    '"$names$"\u9000\u51fa\u4e86\u7fa4\u804a',
    '"$names$"\u79bb\u5f00\u4e86\u7fa4\u804a',
    "left the group",
    "left the group chat",
)
REMOVED_PATTERNS = (
    '"$username$"\u5c06"$names$"\u79fb\u51fa\u4e86\u7fa4\u804a',
    '\u4f60\u5c06"$names$"\u79fb\u51fa\u4e86\u7fa4\u804a',
    '"$username$"\u79fb\u51fa\u4e86"$names$"',
    '"$username$"\u8e22\u51fa\u4e86"$names$"',
    "removed from the group",
    "removed from the group chat",
)


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="group_member_change runtime is ready", output={})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")

    content = str(request.payload.get("content") or "").strip()
    if "<sysmsg" not in content:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="not a system member-change message",
            output={"provider_status": "ignored"},
        )

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"group member change XML parse failed: {exc}")

    if root.tag != "sysmsg":
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="not a sysmsg payload",
            output={"provider_status": "ignored"},
        )
    if str(root.attrib.get("type") or "").strip() != "sysmsgtemplate":
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="sysmsg type is not sysmsgtemplate",
            output={"provider_status": "ignored"},
        )

    template_text = _extract_template_text(root)
    parsed = _parse_member_change(root, template_text)
    if not parsed:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="no actionable group member change detected",
            output={"provider_status": "ignored", "template_text": template_text},
        )

    change_type = parsed["change_type"]
    members = parsed["members"]
    room_id = _resolve_room_id(request)
    updated_count = _apply_member_change(room_id, request.payload, change_type=change_type, members=members)
    reply_text = _build_reply_text(change_type, members)

    output: dict[str, Any] = {
        "provider_status": f"member_{change_type}_detected",
        "change_type": change_type,
        "template_text": template_text,
        "members": members,
        "room_id": room_id,
        "db_updated_member_count": updated_count,
    }
    if reply_text:
        output["reply_text"] = reply_text
        output["replies"] = [{"type": "text", "content": reply_text}]

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="group member change detected",
        output=output,
    )


def _extract_template_text(root: ET.Element) -> str:
    template_node = root.find(".//content_template/template")
    if template_node is None or not template_node.text:
        return ""
    return str(template_node.text).strip()


def _parse_member_change(root: ET.Element, template_text: str) -> dict[str, Any] | None:
    normalized = template_text.strip()
    if not normalized:
        return None

    if any(pattern in normalized for pattern in JOIN_PATTERNS):
        members = _parse_member_info(root, ("names", "adder"))
        if members:
            return {"change_type": "join", "members": members}
    if any(pattern in normalized for pattern in REMOVED_PATTERNS):
        members = _parse_member_info(root, ("names", "username"))
        if members:
            return {"change_type": "removed", "members": members}
    if any(pattern in normalized for pattern in LEAVE_PATTERNS):
        members = _parse_member_info(root, ("username", "names"))
        if members:
            return {"change_type": "left", "members": members}
    return None


def _parse_member_info(root: ET.Element, link_names: tuple[str, ...]) -> list[dict[str, str]]:
    for link_name in link_names:
        result = _parse_link_members(root, link_name)
        if result:
            return result
    return []


def _parse_link_members(root: ET.Element, link_name: str) -> list[dict[str, str]]:
    link = root.find(f".//link[@name='{link_name}']")
    if link is None:
        return []

    memberlist = link.find("memberlist")
    if memberlist is not None:
        members: list[dict[str, str]] = []
        for member in memberlist.findall("member"):
            user_id = (member.findtext("username") or "").strip()
            nickname = (member.findtext("nickname") or "").strip() or user_id
            if not user_id:
                continue
            members.append({"wxid": user_id, "nickname": nickname})
        if members:
            return members

    user_id = (link.findtext("member/username") or link.findtext("username") or "").strip()
    nickname = (link.findtext("member/nickname") or link.findtext("nickname") or "").strip() or user_id
    if user_id:
        return [{"wxid": user_id, "nickname": nickname}]
    return []


def _resolve_room_id(request: PluginExecutionRequest) -> str:
    normalized_event = request.payload.get("normalized_event")
    if isinstance(normalized_event, dict):
        conversation = normalized_event.get("conversation")
        if isinstance(conversation, dict):
            room_id = str(conversation.get("external_id") or "").strip()
            if room_id.endswith("@chatroom"):
                return room_id
    room_id = str(request.payload.get("conversation_external_id") or "").strip()
    return room_id if room_id.endswith("@chatroom") else ""


def _apply_member_change(
    room_id: str,
    payload: dict[str, Any],
    *,
    change_type: str,
    members: list[dict[str, str]],
) -> int:
    if not room_id or not members:
        return 0

    channel_id = _coerce_int(payload.get("channel_id"))
    now = now_shanghai()
    updated_count = 0

    with SessionLocal() as db:
        query = select(Chatroom).where(Chatroom.external_id == room_id)
        if channel_id is not None:
            query = query.where(Chatroom.channel_id == channel_id)
        chatroom = db.scalar(query.order_by(Chatroom.id.desc()))
        if chatroom is None:
            return 0

        for item in members:
            wxid = str(item.get("wxid") or "").strip()
            nickname = str(item.get("nickname") or "").strip() or wxid
            if not wxid:
                continue
            member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == chatroom.id,
                    ChatroomMember.member_external_id == wxid,
                )
            )
            if member is None:
                member = ChatroomMember(
                    chatroom_id=chatroom.id,
                    bot_id=chatroom.bot_id,
                    channel_id=chatroom.channel_id,
                    member_external_id=wxid,
                )
                db.add(member)
            member.nickname = nickname
            member.last_synced_at = now
            member.status = "active" if change_type == "join" else "left"
            updated_count += 1

        if updated_count:
            db.commit()
        else:
            db.rollback()

    return updated_count


def _build_reply_text(change_type: str, members: list[dict[str, str]]) -> str:
    names = "、".join((item.get("nickname") or item.get("wxid") or "").strip() for item in members).strip("、")
    if not names:
        return ""
    if change_type == "join":
        return f"欢迎 {names} 加入群聊。"
    return ""


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
