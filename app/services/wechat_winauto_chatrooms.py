from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, format_shanghai_time, now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope
from app.services.chatroom_registry import format_chatroom_management_status, management_tone, mark_chatroom_discovered
from app.services.plugin_runtime.registry import get_plugin_schema
from app.services.plugin_schemas import deep_merge_config, dump_config_json, parse_config_json
from app.services.wechat_ipad.chatrooms import (
    materialize_group_plugin_scopes_for_chatroom,
    reset_group_plugin_scope_to_inherited,
    update_group_plugin_scope_config_override,
    update_group_plugin_scope_enabled,
)


def sync_wechat_winauto_chatroom_from_message(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    conversation: Conversation | None = None,
) -> dict[str, bool | str]:
    conversation_payload = normalized.get("conversation") or {}
    if str(conversation_payload.get("chat_type") or "").strip() != "group":
        return {"chatroom_created": False, "chatroom_updated": False, "chatroom_id": ""}

    chatroom_external_id = str(conversation_payload.get("external_id") or "").strip()
    if not chatroom_external_id:
        return {"chatroom_created": False, "chatroom_updated": False, "chatroom_id": ""}

    chatroom_name = str(
        conversation_payload.get("title")
        or getattr(conversation, "title", None)
        or chatroom_external_id
    ).strip() or chatroom_external_id
    synced_at = _resolve_synced_at((normalized.get("details") or {}).get("timestamp"))
    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == chatroom_external_id)
    )
    chatroom_created = False
    if not chatroom:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=chatroom_external_id,
            name=chatroom_name,
            status="message_observed",
        )
        db.add(chatroom)
        db.flush()
        chatroom_created = True
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=synced_at,
        registration_source="passive",
        enable_management=chatroom_created,
    )

    extra = _parse_json(chatroom.extra_json)
    chatroom.name = chatroom_name
    chatroom.status = str(chatroom.status or "").strip() or "message_observed"
    chatroom.last_synced_at = synced_at
    extra.update(
        {
            "source": "wechat_winauto_message_ingest",
            "chat_type": "group",
            "last_event_key": normalized.get("event_key"),
            "last_message_type": (normalized.get("message") or {}).get("message_type"),
            "last_sender_id": (normalized.get("message") or {}).get("sender_id"),
            "last_sender_name": (normalized.get("message") or {}).get("sender_name"),
            "last_message_preview": str((normalized.get("message") or {}).get("content") or "")[:120],
        }
    )
    chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source="wechat_winauto_message_ingest",
    )
    db.flush()
    return {
        "chatroom_created": chatroom_created,
        "chatroom_updated": True,
        "chatroom_id": chatroom_external_id,
    }


def sync_wechat_winauto_chatrooms(db: Session, channel: Channel) -> dict[str, Any]:
    conversations = db.scalars(
        select(Conversation)
        .where(
            Conversation.channel_id == channel.id,
            Conversation.chat_type == "group",
        )
        .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
    ).all()
    created_count = 0
    updated_count = 0
    for conversation in conversations:
        external_id = str(conversation.external_id or "").strip()
        if not external_id:
            continue
        chatroom = db.scalar(
            select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id)
        )
        chatroom_created = False
        if not chatroom:
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id=external_id,
                status="message_observed",
            )
            db.add(chatroom)
            db.flush()
            created_count += 1
            chatroom_created = True
        else:
            updated_count += 1
        observed_at = conversation.last_message_at or conversation.created_at or now_shanghai()
        mark_chatroom_discovered(
            db,
            channel=channel,
            chatroom=chatroom,
            seen_at=observed_at,
            registration_source="passive",
            enable_management=chatroom_created,
        )
        chatroom.name = str(conversation.title or external_id).strip() or external_id
        chatroom.last_synced_at = observed_at
        extra = _parse_json(chatroom.extra_json)
        extra.update(
            {
                "source": "wechat_winauto_conversation_materialize",
                "conversation_id": conversation.id,
                "chat_type": "group",
            }
        )
        chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
        materialize_group_plugin_scopes_for_chatroom(
            db,
            channel=channel,
            chatroom=chatroom,
            source="wechat_winauto_conversation_materialize",
        )
    db.flush()
    return {
        "total": len(conversations),
        "created": created_count,
        "updated": updated_count,
        "last_status_summary": (
            f"已从群会话样本整理 {len(conversations)} 个 wxauto 群，新增 {created_count} 个，更新 {updated_count} 个。"
            if conversations
            else "当前还没有观测到 wxauto 群会话样本。"
        ),
    }


def build_wechat_winauto_chatroom_list(db: Session, channel: Channel) -> dict[str, Any]:
    sync_wechat_winauto_chatrooms(db, channel)
    chatrooms = db.scalars(
        select(Chatroom)
        .where(Chatroom.channel_id == channel.id)
        .order_by(Chatroom.last_synced_at.desc(), Chatroom.id.desc())
    ).all()
    bindings = db.scalars(
        select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id).order_by(BotPlugin.id.asc())
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(GroupPluginScope.channel_id == channel.id)
        .order_by(GroupPluginScope.chatroom_id.asc(), GroupPluginScope.bot_plugin_id.asc())
    ).all()

    binding_enabled_map = {binding.id: str(binding.status or "").strip() == "enabled" for binding in bindings}
    scope_map_by_chatroom: dict[int, dict[int, GroupPluginScope]] = {}
    for scope in scope_rows:
        scope_map_by_chatroom.setdefault(int(scope.chatroom_id), {})[int(scope.bot_plugin_id)] = scope

    items: list[dict[str, Any]] = []
    inherited_chatroom_count = 0
    overridden_chatroom_count = 0
    for chatroom in chatrooms:
        scope_map = scope_map_by_chatroom.get(chatroom.id, {})
        overridden_scope_count = 0
        effective_enabled_count = 0
        for binding in bindings:
            scope = scope_map.get(binding.id)
            if scope and str(scope.scope_status or "").strip() != "inherited":
                overridden_scope_count += 1
            if scope is not None:
                if bool(scope.enabled):
                    effective_enabled_count += 1
            elif binding_enabled_map.get(binding.id):
                effective_enabled_count += 1
        if overridden_scope_count > 0:
            overridden_chatroom_count += 1
        else:
            inherited_chatroom_count += 1
        items.append(
            {
                "id": chatroom.id,
                "name": chatroom.name or chatroom.external_id,
                "external_id": chatroom.external_id,
                "remark": chatroom.remark or "-",
                "owner_external_id": chatroom.owner_external_id or "-",
                "status": chatroom.status or "message_observed",
                "status_label": _format_chatroom_status(chatroom.status),
                "management_status": chatroom.management_status or "discovered",
                "management_status_label": format_chatroom_management_status(chatroom.management_status),
                "management_status_tone": management_tone(chatroom.management_status),
                "is_active": bool(chatroom.is_active),
                "sync_depth_label": "会话样本",
                "member_count": int(chatroom.member_count or 0),
                "member_record_count": 0,
                "member_role_summary": "wxauto 暂无成员快照",
                "scope_total": len(bindings),
                "scope_inherited": max(len(bindings) - overridden_scope_count, 0),
                "scope_overridden": overridden_scope_count,
                "scope_effective_enabled": effective_enabled_count,
                "scope_effective_disabled": max(len(bindings) - effective_enabled_count, 0),
                "last_synced_at": _format_dt(chatroom.last_synced_at),
                "observed_source": _extract_chatroom_source(chatroom),
            }
        )

    return {
        "items": items,
        "total": len(items),
        "binding_total": len(bindings),
        "binding_enabled_total": sum(1 for enabled in binding_enabled_map.values() if enabled),
        "member_snapshot_chatroom_count": 0,
        "pending_chatroom_count": sum(1 for item in items if item["management_status"] == "discovered"),
        "active_chatroom_count": sum(1 for item in items if item["is_active"]),
        "disabled_chatroom_count": sum(1 for item in items if item["management_status"] == "disabled"),
        "inherited_chatroom_count": inherited_chatroom_count,
        "overridden_chatroom_count": overridden_chatroom_count,
        "supports_sync": True,
        "sync_action": f"/channels/{channel.id}/wechat-winauto/sync-chatrooms",
        "sync_button_label": "刷新群列表",
        # 产品页面使用个人微信业务名称，不把 wxauto 运行实现暴露给企业员工。
        "intro_summary": "集中查看已连接的个人微信群聊和运营状态。",
        "empty_state_message": "暂无个人微信群聊，收到群消息后会自动显示。",
    }


def build_wechat_winauto_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    sync_wechat_winauto_chatrooms(db, channel)
    bindings = db.scalars(
        select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id).order_by(BotPlugin.id.asc())
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(GroupPluginScope.channel_id == channel.id, GroupPluginScope.chatroom_id == chatroom.id)
        .order_by(GroupPluginScope.id.asc())
    ).all()
    if not scope_rows and bindings:
        scope_rows = materialize_group_plugin_scopes_for_chatroom(
            db,
            channel=channel,
            chatroom=chatroom,
            source="wechat_winauto_chatroom_detail_view",
        )

    scope_map = {item.bot_plugin_id: item for item in scope_rows}
    binding_items: list[dict[str, Any]] = []
    inherited_scope_count = 0
    overridden_scope_count = 0
    enabled_binding_count = 0
    effective_enabled_count = 0
    for binding in bindings:
        plugin = binding.plugin
        binding_enabled = str(binding.status or "").strip() == "enabled"
        if binding_enabled:
            enabled_binding_count += 1
        scope = scope_map.get(binding.id)
        scope_status = str(getattr(scope, "scope_status", "") or "inherited")
        scope_enabled = bool(getattr(scope, "enabled", binding_enabled))
        if scope_status == "inherited":
            inherited_scope_count += 1
        else:
            overridden_scope_count += 1
        if scope_enabled:
            effective_enabled_count += 1
        binding_config = parse_config_json(binding.config_json)
        scope_config = parse_config_json(getattr(scope, "config_override_json", None))
        effective_config = _build_effective_group_config(
            plugin_code=plugin.code if plugin else None,
            binding_config=binding_config,
            scope_config=scope_config,
        )
        binding_items.append(
            {
                "binding_id": binding.id,
                "plugin_code": plugin.code if plugin else "-",
                "plugin_name": plugin.display_name if plugin else f"Plugin #{binding.plugin_id}",
                "binding_status": binding.status or "unknown",
                "binding_status_label": "已启用" if binding_enabled else "已停用",
                "binding_status_tone": "success" if binding_enabled else "muted",
                "runtime_status": binding.runtime_status or "idle",
                "last_execution_status": binding.last_execution_status or "-",
                "last_execution_summary": binding.last_execution_summary or "-",
                "last_execution_at": _format_dt(binding.last_execution_at),
                "group_scope_status_label": "继承 bot" if scope_status == "inherited" else "群级覆盖",
                "group_scope_status_tone": "outline" if scope_status == "inherited" else "success",
                "group_scope_enabled_label": "启用" if scope_enabled else "停用",
                "group_scope_action_scope": str(getattr(scope, "action_scope", "") or "all"),
                "group_scope_effect": str(getattr(scope, "policy_effect", "") or "") or "-",
                "group_scope_status": scope_status,
                "group_scope_enabled": scope_enabled,
                "group_scope_mode": "inherited" if scope_status == "inherited" else ("enabled" if scope_enabled else "disabled"),
                "group_scope_summary": (
                    "当前没有群级覆盖配置；该群直接继承 bot_plugin 的启停与配置。"
                    if scope_status == "inherited"
                    else "当前已存在群级作用域覆盖记录；该群会优先使用单群启停和配置覆盖。"
                ),
                "group_scope_boundary_note": "wxauto 当前只基于已观察到的群会话做轻量群管理，不含成员级控制。",
                "group_scope_can_enable": not scope_enabled,
                "group_scope_can_disable": scope_enabled,
                "group_scope_can_reset": scope_status != "inherited",
                "group_scope_has_config_override": bool(getattr(scope, "config_override_json", None)),
                "group_scope_config_override_json": str(getattr(scope, "config_override_json", "") or "").strip(),
                "group_scope_effective_config_json": _format_config_for_editor(effective_config),
                "group_scope_effective_config_has_values": bool(effective_config),
                "binding_config_json": dump_config_json(binding_config) or "",
            }
        )

    return {
        "chatroom_id": chatroom.id,
        "chatroom_external_id": chatroom.external_id,
        "name": chatroom.name or chatroom.external_id,
        "remark": chatroom.remark or "-",
        "owner_external_id": chatroom.owner_external_id or "-",
        "status": chatroom.status or "message_observed",
        "status_label": _format_chatroom_status(chatroom.status),
        "management_status": chatroom.management_status or "discovered",
        "management_status_label": format_chatroom_management_status(chatroom.management_status),
        "management_status_tone": management_tone(chatroom.management_status),
        "is_active": bool(chatroom.is_active),
        "member_count": int(chatroom.member_count or 0),
        "member_record_count": 0,
        "last_synced_at": _format_dt(chatroom.last_synced_at),
        "observed_source": _extract_chatroom_source(chatroom),
        "sync_depth_label": "会话样本",
        "role_counts": {},
        "members": [],
        "bindings": binding_items,
        "supports_sync": True,
        "supports_member_snapshot": False,
        "sync_action": f"/channels/{channel.id}/chatrooms/{chatroom.id}/sync",
        "sync_button_label": "刷新当前群",
        "member_section_hint": "wxauto 当前没有稳定的成员快照能力，这里先展示群级插件控制。",
        "binding_summary": {
            "total": len(binding_items),
            "enabled": enabled_binding_count,
            "disabled": max(len(binding_items) - enabled_binding_count, 0),
            "materialized_scope_count": len(scope_rows),
            "inherited_scope_count": inherited_scope_count,
            "overridden_scope_count": overridden_scope_count,
            "effective_enabled_count": effective_enabled_count,
            "effective_disabled_count": max(len(binding_items) - effective_enabled_count, 0),
            "operator_summary": (
                f"当前 bot 共绑定 {len(binding_items)} 个插件；"
                f"wxauto 群管理已落库 {len(scope_rows)} 条群级作用域记录，继承 {inherited_scope_count} / 覆盖 {overridden_scope_count}；"
                f"当前有效启用 {effective_enabled_count} / 停用 {max(len(binding_items) - effective_enabled_count, 0)}。"
            ),
        },
    }


def update_wechat_winauto_chatroom_scope_enabled(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    enabled: bool,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_enabled(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        enabled=enabled,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_winauto_chatroom_scope_toggle",
    )


def reset_wechat_winauto_chatroom_scope(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return reset_group_plugin_scope_to_inherited(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_winauto_chatroom_scope_reset",
    )


def update_wechat_winauto_chatroom_scope_config_override(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    config_override_json: str | None,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_config_override(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        config_override_json=config_override_json,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_winauto_chatroom_scope_config_override",
    )


def _parse_json(raw: str | None) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_dt(value: Any) -> str:
    return format_shanghai_time(value)


def _extract_chatroom_source(chatroom: Chatroom) -> str:
    extra = _parse_json(chatroom.extra_json)
    source = str(extra.get("source") or "").strip()
    if source == "wechat_winauto_message_ingest":
        return "消息观测"
    if source == "wechat_winauto_conversation_materialize":
        return "会话整理"
    return source or "本地记录"


def _format_chatroom_status(status: str | None) -> str:
    normalized = str(status or "").strip()
    return {
        "message_observed": "已观测",
        "profile_synced": "资料已同步",
        "members_synced": "成员已同步",
        "pending_sync": "待同步",
    }.get(normalized, normalized or "未知")


def _resolve_synced_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return ensure_shanghai_datetime(parsed)
        except ValueError:
            pass
    return now_shanghai()


def _build_effective_group_config(
    *,
    plugin_code: str | None,
    binding_config: dict[str, Any],
    scope_config: dict[str, Any],
) -> dict[str, Any]:
    base: dict[str, Any] = {}
    schema = get_plugin_schema(plugin_code)
    if schema:
        for field in schema.fields:
            if field.default not in (None, "", []):
                base[field.name] = field.default
    return deep_merge_config(deep_merge_config(base, binding_config), scope_config)


def _format_config_for_editor(config: dict[str, Any]) -> str:
    if not config:
        return ""
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True)
