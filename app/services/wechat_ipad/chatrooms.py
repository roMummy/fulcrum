from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.services.plugin_scopes import binding_group_default_enabled
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, GroupPluginScope
from app.services.chatroom_registry import mark_chatroom_discovered
from app.services.wechat_ipad.cache import remember_chatroom, remember_chatroom_members, remember_member


def sync_wechat_ipad_chatroom_from_message(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    conversation: Conversation | None = None,
) -> dict[str, bool | str]:
    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    details = normalized.get("details") or {}

    if str(conversation_payload.get("chat_type") or "") != "group":
        return {"chatroom_created": False, "member_created": False, "chatroom_id": "", "member_external_id": ""}

    chatroom_external_id = str(conversation_payload.get("external_id") or "").strip()
    if not chatroom_external_id or not chatroom_external_id.endswith("@chatroom"):
        return {"chatroom_created": False, "member_created": False, "chatroom_id": "", "member_external_id": ""}

    synced_at = _resolve_synced_at(details.get("timestamp"))
    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == chatroom_external_id)
    )
    chatroom_created = False
    if not chatroom:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=chatroom_external_id,
            status="message_observed",
        )
        db.add(chatroom)
        db.flush()
        chatroom_created = True
    remember_chatroom(channel.id, chatroom_external_id)
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=synced_at,
        registration_source="passive",
        enable_management=chatroom_created,
    )

    inferred_name = _infer_chatroom_name(conversation_payload, conversation, chatroom_external_id)
    if inferred_name:
        chatroom.name = inferred_name
    chatroom.last_synced_at = synced_at
    chatroom.extra_json = json.dumps(
        {
            "source": "wechat_ipad_message_ingest",
            "last_event_key": normalized.get("event_key"),
            "last_sender_id": message_payload.get("sender_id"),
            "last_sender_name": message_payload.get("sender_name"),
            "last_message_type": message_payload.get("message_type"),
            "name_source": "message_payload_or_conversation",
            "is_group": True,
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    member_external_id = str(message_payload.get("sender_id") or "").strip()
    if not member_external_id:
        return {
            "chatroom_created": chatroom_created,
            "member_created": False,
            "chatroom_id": chatroom_external_id,
            "member_external_id": "",
        }

    member = db.scalar(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == chatroom.id,
            ChatroomMember.member_external_id == member_external_id,
        )
    )
    member_created = False
    if not member:
        member = ChatroomMember(
            chatroom_id=chatroom.id,
            bot_id=channel.bot_id,
            channel_id=channel.id,
            member_external_id=member_external_id,
            status="message_observed",
        )
        db.add(member)
        member_created = True

    member.nickname = _resolve_member_nickname(message_payload, member_external_id)
    member.status = "active"
    member.last_synced_at = synced_at
    member.last_active_at = synced_at
    member.extra_json = json.dumps(
        {
            "source": "wechat_ipad_message_ingest",
            "last_event_key": normalized.get("event_key"),
            "observed_via": "group_message",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    remember_member(channel.id, chatroom_external_id, member_external_id)
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source="wechat_ipad_message_ingest",
    )
    db.flush()
    return {
        "chatroom_created": chatroom_created,
        "member_created": member_created,
        "chatroom_id": chatroom_external_id,
        "member_external_id": member_external_id,
    }


def upsert_wechat_ipad_chatroom_profile(
    db: Session,
    channel: Channel,
    *,
    chatroom_external_id: str,
    profile_payload: dict[str, Any] | None = None,
    member_payload: dict[str, Any] | None = None,
    synced_at: datetime | None = None,
    source: str = "wechat_ipad_chatroom_sync",
) -> Chatroom:
    normalized_chatroom_id = str(chatroom_external_id or "").strip()
    if not normalized_chatroom_id:
        raise ValueError("chatroom_external_id is required")

    resolved_synced_at = synced_at or now_shanghai()
    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == normalized_chatroom_id)
    )
    chatroom_created = False
    if not chatroom:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=normalized_chatroom_id,
            status="pending_sync",
        )
        db.add(chatroom)
        db.flush()
        chatroom_created = True
    remember_chatroom(channel.id, chatroom_external_id)
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=resolved_synced_at,
        registration_source="passive",
        enable_management=chatroom_created,
    )

    profile = _normalize_chatroom_profile(profile_payload or {}, normalized_chatroom_id)
    if profile["name"]:
        chatroom.name = profile["name"]
    if profile["remark"]:
        chatroom.remark = profile["remark"]
    if profile["owner_external_id"]:
        chatroom.owner_external_id = profile["owner_external_id"]

    members = _normalize_chatroom_members(member_payload or {})
    if members:
        _replace_chatroom_members(
            db,
            chatroom=chatroom,
            channel=channel,
            members=members,
            synced_at=resolved_synced_at,
            source=source,
        )
        chatroom.member_count = len(members)
        chatroom.status = "members_synced"
        remember_chatroom_members(channel.id, normalized_chatroom_id, [item["member_external_id"] for item in members])
    else:
        if profile["member_count"] > 0:
            chatroom.member_count = profile["member_count"]
        chatroom.status = "profile_synced"
        remember_chatroom(channel.id, normalized_chatroom_id)

    chatroom.last_synced_at = resolved_synced_at
    chatroom.extra_json = json.dumps(
        {
            "source": source,
            "profile_payload": profile_payload or {},
            "member_payload_present": bool(member_payload),
            "member_count": int(chatroom.member_count or 0),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source=source,
    )
    db.flush()
    return chatroom


def materialize_group_plugin_scopes_for_chatroom(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    source: str = "group_scope_materialized",
) -> list[GroupPluginScope]:
    bindings = db.scalars(
        select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound").order_by(BotPlugin.id.asc())
    ).all()
    existing_scopes = db.scalars(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
        )
    ).all()
    existing_map = {item.bot_plugin_id: item for item in existing_scopes}
    scopes: list[GroupPluginScope] = []
    for binding in bindings:
        scope = existing_map.get(binding.id)
        if not scope:
            scope = GroupPluginScope(
                channel_id=channel.id,
                chatroom_id=chatroom.id,
                bot_plugin_id=binding.id,
            )
            db.add(scope)
            scope.scope_status = "inherited"
            scope.enabled = binding_group_default_enabled(binding)
            scope.action_scope = "all"
            scope.audit_note = "auto materialized from bot_plugin binding"
            scope.extra_json = json.dumps(
                {
                    "source": source,
                    "binding_status": binding.status,
                    "runtime_status": binding.runtime_status,
                    "materialized": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        scopes.append(scope)
    db.flush()
    return scopes


def update_group_plugin_scope_enabled(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    enabled: bool,
    operator_id: int | None = None,
    audit_note: str | None = None,
    source: str = "group_scope_manual_toggle",
) -> GroupPluginScope:
    binding = db.get(BotPlugin, bot_plugin_id)
    if not binding or binding.bot_id != channel.bot_id or binding.status == "unbound":
        raise ValueError("bot_plugin binding not found for channel bot")

    scope = _ensure_group_plugin_scope(
        db,
        channel=channel,
        chatroom=chatroom,
        binding=binding,
        source=source,
    )
    previous_enabled = bool(scope.enabled)
    previous_status = str(scope.scope_status or "inherited")
    scope.scope_status = "overridden"
    scope.enabled = bool(enabled)
    scope.action_scope = scope.action_scope or "all"
    scope.last_operator_id = operator_id
    scope.last_operated_at = now_shanghai()
    scope.audit_note = audit_note or (
        "manual group scope enabled" if enabled else "manual group scope disabled"
    )
    scope.extra_json = json.dumps(
        {
            "source": source,
            "binding_status": binding.status,
            "runtime_status": binding.runtime_status,
            "previous_status": previous_status,
            "previous_enabled": previous_enabled,
            "current_enabled": bool(enabled),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return scope


def reset_group_plugin_scope_to_inherited(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    operator_id: int | None = None,
    audit_note: str | None = None,
    source: str = "group_scope_reset_to_inherited",
) -> GroupPluginScope:
    binding = db.get(BotPlugin, bot_plugin_id)
    if not binding or binding.bot_id != channel.bot_id or binding.status == "unbound":
        raise ValueError("bot_plugin binding not found for channel bot")

    scope = _ensure_group_plugin_scope(
        db,
        channel=channel,
        chatroom=chatroom,
        binding=binding,
        source=source,
    )
    scope.scope_status = "inherited"
    scope.enabled = binding_group_default_enabled(binding)
    scope.action_scope = scope.action_scope or "all"
    scope.policy_effect = None
    scope.config_override_json = None
    scope.last_operator_id = operator_id
    scope.last_operated_at = now_shanghai()
    scope.audit_note = audit_note or "reset group scope to inherited bot_plugin binding"
    scope.extra_json = json.dumps(
        {
            "source": source,
            "binding_status": binding.status,
            "runtime_status": binding.runtime_status,
            "current_enabled": scope.enabled,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return scope


def update_group_plugin_scope_config_override(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    config_override_json: str | None,
    operator_id: int | None = None,
    audit_note: str | None = None,
    source: str = "group_scope_config_override",
) -> GroupPluginScope:
    binding = db.get(BotPlugin, bot_plugin_id)
    if not binding or binding.bot_id != channel.bot_id or binding.status == "unbound":
        raise ValueError("bot_plugin binding not found for channel bot")

    scope = _ensure_group_plugin_scope(
        db,
        channel=channel,
        chatroom=chatroom,
        binding=binding,
        source=source,
    )
    previous_status = str(scope.scope_status or "inherited")
    normalized_override = str(config_override_json or "").strip() or None
    binding_enabled = binding_group_default_enabled(binding)
    has_enable_override = bool(scope.enabled) != binding_enabled
    scope.scope_status = "overridden" if normalized_override is not None or has_enable_override else "inherited"
    if previous_status == "inherited":
        scope.enabled = binding_enabled
    scope.action_scope = scope.action_scope or "all"
    scope.config_override_json = normalized_override
    scope.last_operator_id = operator_id
    scope.last_operated_at = now_shanghai()
    scope.audit_note = audit_note or ("updated group config override" if normalized_override else "cleared group config override")
    scope.extra_json = json.dumps(
        {
            "source": source,
            "binding_status": binding.status,
            "runtime_status": binding.runtime_status,
            "previous_status": previous_status,
            "has_config_override": normalized_override is not None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return scope


def _ensure_group_plugin_scope(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    binding: BotPlugin,
    source: str,
) -> GroupPluginScope:
    scope = db.scalar(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
            GroupPluginScope.bot_plugin_id == binding.id,
        )
    )
    if scope:
        return scope

    materialized = materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source=source,
    )
    scope_map = {item.bot_plugin_id: item for item in materialized}
    scope = scope_map.get(binding.id)
    if scope:
        return scope

    scope = GroupPluginScope(
        channel_id=channel.id,
        chatroom_id=chatroom.id,
        bot_plugin_id=binding.id,
        scope_status="inherited",
        enabled=binding_group_default_enabled(binding),
        action_scope="all",
    )
    db.add(scope)
    db.flush()
    return scope


def _parse_extra_json(raw: str | None) -> dict[str, Any]:
    normalized = str(raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _infer_chatroom_name(
    conversation_payload: dict[str, Any],
    conversation: Conversation | None,
    chatroom_external_id: str,
) -> str | None:
    for candidate in (
        conversation_payload.get("title"),
        getattr(conversation, "title", None),
        chatroom_external_id,
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _resolve_member_nickname(message_payload: dict[str, Any], member_external_id: str) -> str:
    for candidate in (
        message_payload.get("sender_name"),
        member_external_id,
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return member_external_id


def _resolve_synced_at(timestamp: Any) -> datetime:
    if timestamp not in (None, ""):
        try:
            return datetime.fromtimestamp(int(timestamp), tz=now_shanghai().tzinfo)
        except (TypeError, ValueError, OSError):
            pass
    return now_shanghai()


def _normalize_chatroom_profile(profile_payload: dict[str, Any], chatroom_external_id: str) -> dict[str, Any]:
    contact = {}
    if isinstance(profile_payload.get("ContactList"), list) and profile_payload.get("ContactList"):
        first = profile_payload["ContactList"][0]
        if isinstance(first, dict):
            contact = first
    elif isinstance(profile_payload.get("Data"), dict):
        data = profile_payload["Data"]
        if isinstance(data.get("ContactList"), list) and data.get("ContactList"):
            first = data["ContactList"][0]
            if isinstance(first, dict):
                contact = first
        elif isinstance(data, dict):
            contact = data
    elif profile_payload:
        contact = profile_payload

    def _pick(*keys: str) -> str:
        for key in keys:
            value = contact.get(key)
            if isinstance(value, dict):
                value = value.get("string") or value.get("String") or value.get("value")
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    member_count_raw = contact.get("MemberCount") or contact.get("memberCount") or contact.get("member_count") or 0
    try:
        member_count = int(member_count_raw or 0)
    except (TypeError, ValueError):
        member_count = 0

    return {
        "name": _pick("NickName", "nickname", "name") or chatroom_external_id,
        "remark": _pick("Remark", "remark"),
        "owner_external_id": _pick("ChatRoomOwner", "chatRoomOwner", "OwnerUserName", "owner"),
        "member_count": member_count,
    }


def _normalize_chatroom_members(member_payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = member_payload.get("Data")
    if isinstance(data, dict):
        new_chatroom_data = data.get("NewChatroomData")
        if isinstance(new_chatroom_data, dict) and isinstance(new_chatroom_data.get("ChatRoomMember"), list):
            raw_members = new_chatroom_data.get("ChatRoomMember") or []
        elif isinstance(data.get("ChatRoomMember"), list):
            raw_members = data.get("ChatRoomMember") or []
        else:
            raw_members = []
    elif isinstance(member_payload.get("ChatRoomMember"), list):
        raw_members = member_payload.get("ChatRoomMember") or []
    else:
        raw_members = []

    members: list[dict[str, Any]] = []
    for item in raw_members:
        if not isinstance(item, dict):
            continue
        external_id = str(item.get("UserName") or item.get("userName") or item.get("wxid") or "").strip()
        if not external_id:
            continue
        role = "member"
        member_flag = str(item.get("MemberFlag") or item.get("memberFlag") or "").strip()
        if member_flag == "1" or member_flag == "2049":
            role = "admin"
        members.append(
            {
                "member_external_id": external_id,
                "nickname": str(item.get("NickName") or item.get("nickname") or external_id).strip() or external_id,
                "display_name": str(item.get("DisplayName") or item.get("displayName") or "").strip(),
                "role": role,
                "member_flag": member_flag,
                "inviter_user_name": str(item.get("InviterUserName") or item.get("inviterUserName") or "").strip(),
                "alias": str(item.get("Alias") or item.get("alias") or "").strip(),
                "remark": str(item.get("Remark") or item.get("remark") or "").strip(),
                "province": str(item.get("Province") or item.get("province") or "").strip(),
                "city": str(item.get("City") or item.get("city") or "").strip(),
                "small_head_img_url": str(
                    item.get("SmallHeadImgUrl")
                    or item.get("smallHeadImgUrl")
                    or item.get("small_head_img_url")
                    or ""
                ).strip(),
            }
        )
    return members


def _replace_chatroom_members(
    db: Session,
    *,
    chatroom: Chatroom,
    channel: Channel,
    members: list[dict[str, Any]],
    synced_at: datetime,
    source: str,
) -> None:
    existing_members = db.scalars(
        select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id)
    ).all()
    existing_map = {item.member_external_id: item for item in existing_members}
    incoming_ids = {item["member_external_id"] for item in members}

    for existing in existing_members:
        if existing.member_external_id not in incoming_ids:
            existing.status = "left"
            existing.last_synced_at = synced_at

    for item in members:
        member = existing_map.get(item["member_external_id"])
        if not member:
            member = ChatroomMember(
                chatroom_id=chatroom.id,
                bot_id=channel.bot_id,
                channel_id=channel.id,
                member_external_id=item["member_external_id"],
        )
        db.add(member)
        member.nickname = item["nickname"] or item["member_external_id"]
        member.display_name = item["display_name"] or None
        member.role = item["role"] or "member"
        member.status = "active"
        member.last_synced_at = synced_at
        member.extra_json = json.dumps(
            {
                "source": source,
                "observed_via": "chatroom_member_detail",
                "member_flag": item.get("member_flag") or "",
                "inviter_user_name": item.get("inviter_user_name") or "",
                "alias": item.get("alias") or "",
                "remark": item.get("remark") or "",
                "province": item.get("province") or "",
                "city": item.get("city") or "",
                "small_head_img_url": item.get("small_head_img_url") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    remember_chatroom_members(channel.id, chatroom.external_id, [item["member_external_id"] for item in members])
