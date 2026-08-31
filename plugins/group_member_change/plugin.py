from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember, Conversation
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


JOIN_PATTERNS = (
    '"$names$"加入了群聊',
    '"$username$"邀请"$names$"加入了群聊',
    '你邀请"$names$"加入了群聊',
    '"$adder$"通过扫描"$from$"分享的二维码加入群聊',
    '"$adder$"通过"$from$"的邀请二维码加入群聊',
    "joined the group",
    "joined the group chat",
)
LEAVE_PATTERNS = (
    '"$username$"退出了群聊',
    '"$username$"离开了群聊',
    '"$names$"退出了群聊',
    '"$names$"离开了群聊',
    "left the group",
    "left the group chat",
)
REMOVED_PATTERNS = (
    '"$username$"将"$names$"移出了群聊',
    '你将"$names$"移出了群聊',
    '"$username$"移出了"$names$"',
    '"$username$"踢出了"$names$"',
    "removed from the group",
    "removed from the group chat",
)
VALID_CHANGE_TYPES = {"join", "left", "removed"}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只汇报运行状态，成员数据更新必须经过可信数据库入口。"""

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群成员变更插件已就绪。",
            output={},
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("群成员变更只能由真实群聊事件触发。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """使用可信群会话更新成员状态，并对加入事件生成标准欢迎回复。"""

    if request.action != "execute":
        return execute_action(request)
    context, chatroom, error = _require_group_context(db, request.context)
    if error is not None or context is None or chatroom is None:
        return error or _error_result("群成员变更缺少可信群聊上下文。", "trusted_context_required")

    parsed = _parse_structured_member_change(request.payload)
    if parsed is None:
        parsed = _parse_wechat_member_change(str(request.payload.get("content") or "").strip())
    if parsed is None:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="当前事件不包含成员增减。",
            output={"provider_status": "ignored"},
        )

    change_type = str(parsed["change_type"])
    members = list(parsed["members"])
    updated_count = _apply_member_change(
        db,
        context=context,
        chatroom=chatroom,
        change_type=change_type,
        members=members,
    )
    reply_text = _build_reply_text(change_type, members)
    output: dict[str, Any] = {
        "provider_status": f"member_{change_type}",
        "change_type": change_type,
        "member_count": len(members),
        "db_updated_member_count": updated_count,
    }
    if reply_text:
        output["reply_text"] = reply_text
        output["replies"] = standard_replies([text_reply(reply_text)])
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="群成员状态已更新。",
        output=output,
    )


def _require_group_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[PluginExecutionContext | None, Chatroom | None, PluginExecutionResult | None]:
    """从平台注入的会话主键解析群，不读取载荷中的渠道号或群号。"""

    if (
        context is None
        or context.conversation_id is None
        or context.channel_id is None
        or context.chat_type != "group"
    ):
        return None, None, _error_result("请在群聊事件中使用群成员变更插件。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or conversation.chat_type != "group"
    ):
        return None, None, _error_result("当前群聊上下文无效。", "trusted_context_required")
    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(context.channel_id),
            Chatroom.external_id == str(conversation.external_id),
        )
    )
    if chatroom is None:
        return None, None, _error_result("当前群还没有建立成员资料。", "chatroom_not_found")
    return context, chatroom, None


def _parse_structured_member_change(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """读取渠道适配器生成的通用成员事件，不直接解析 Telegram 私有字段。"""

    normalized_event = payload.get("normalized_event")
    if not isinstance(normalized_event, Mapping):
        return None
    details = normalized_event.get("details")
    if not isinstance(details, Mapping):
        return None
    member_change = details.get("member_change")
    if not isinstance(member_change, Mapping):
        return None
    change_type = str(member_change.get("change_type") or "").strip().lower()
    if change_type not in VALID_CHANGE_TYPES:
        return None
    members = _normalize_members(member_change.get("members"))
    return {"change_type": change_type, "members": members} if members else None


def _parse_wechat_member_change(content: str) -> dict[str, Any] | None:
    """把微信协议系统消息转换为与通用渠道一致的成员事件。"""

    if "<sysmsg" not in content:
        return None
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    if root.tag != "sysmsg" or str(root.attrib.get("type") or "").strip() != "sysmsgtemplate":
        return None
    template_node = root.find(".//content_template/template")
    template_text = str(template_node.text or "").strip() if template_node is not None else ""
    if any(pattern in template_text for pattern in JOIN_PATTERNS):
        link_names = ("names", "adder")
        change_type = "join"
    elif any(pattern in template_text for pattern in REMOVED_PATTERNS):
        link_names = ("names", "username")
        change_type = "removed"
    elif any(pattern in template_text for pattern in LEAVE_PATTERNS):
        link_names = ("username", "names")
        change_type = "left"
    else:
        return None
    for link_name in link_names:
        members = _parse_wechat_link_members(root, link_name)
        if members:
            return {"change_type": change_type, "members": members}
    return None


def _parse_wechat_link_members(root: ET.Element, link_name: str) -> list[dict[str, str]]:
    """读取微信系统消息中的 memberlist 或单 member 结构。"""

    link = root.find(f".//link[@name='{link_name}']")
    if link is None:
        return []
    nodes = link.findall("memberlist/member") or link.findall("member")
    members: list[dict[str, str]] = []
    for node in nodes:
        external_id = str(node.findtext("username") or "").strip()
        if not external_id:
            continue
        display_name = str(node.findtext("nickname") or "").strip() or external_id
        members.append({"external_id": external_id, "display_name": display_name})
    return members


def _normalize_members(value: Any) -> list[dict[str, str]]:
    """校验通用成员列表，只保留平台定义的公开字段。"""

    if not isinstance(value, (list, tuple)):
        return []
    members: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        external_id = str(item.get("external_id") or "").strip()
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        display_name = str(item.get("display_name") or "").strip() or external_id
        members.append({"external_id": external_id, "display_name": display_name})
    return members


def _apply_member_change(
    db: Session,
    *,
    context: PluginExecutionContext,
    chatroom: Chatroom,
    change_type: str,
    members: list[dict[str, str]],
) -> int:
    """在当前租户和当前群内更新成员，退出与移除统一进入 left 状态。"""

    current = now_shanghai()
    updated_count = 0
    for item in members:
        external_id = item["external_id"]
        display_name = item["display_name"]
        member = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.tenant_id == int(context.tenant_id),
                ChatroomMember.bot_id == int(context.bot_id),
                ChatroomMember.channel_id == int(context.channel_id or 0),
                ChatroomMember.chatroom_id == int(chatroom.id),
                ChatroomMember.member_external_id == external_id,
            )
        )
        if member is None:
            member = ChatroomMember(
                tenant_id=int(context.tenant_id),
                chatroom_id=int(chatroom.id),
                bot_id=int(context.bot_id),
                channel_id=int(context.channel_id or 0),
                member_external_id=external_id,
            )
            db.add(member)
        member.nickname = display_name
        member.display_name = display_name
        member.last_synced_at = current
        member.status = "active" if change_type == "join" else "left"
        updated_count += 1
    if updated_count:
        db.flush()
    return updated_count


def _build_reply_text(change_type: str, members: list[dict[str, str]]) -> str:
    """当前业务只对新成员发送欢迎语，离开和移除保持静默。"""

    if change_type != "join":
        return ""
    names = "、".join(item["display_name"] for item in members if item.get("display_name"))
    return f"欢迎 {names} 加入群聊。" if names else ""


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status},
    )
