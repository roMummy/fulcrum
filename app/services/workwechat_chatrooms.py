from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, format_shanghai_time, now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, GroupPluginScope, Message, TaskJob
from app.services.channel_adapters.workwechat import WorkWechatAdapter
from app.services.chatroom_registry import format_chatroom_management_status, management_tone, mark_chatroom_discovered
from app.services.plugin_runtime.registry import get_plugin_schema
from app.services.plugin_schemas import deep_merge_config, dump_config_json, parse_config_json
from app.services.wechat_ipad.chatrooms import (
    materialize_group_plugin_scopes_for_chatroom,
    reset_group_plugin_scope_to_inherited,
    update_group_plugin_scope_config_override,
    update_group_plugin_scope_enabled,
)


logger = get_logger(__name__)
PROVIDER_CHATROOM_LIST_PATH = "/chatrooms/list"
PROVIDER_CHATROOM_MEMBERS_PATH = "/chatrooms/members"
MESSAGE_FLOW_LIMIT = 80


def sync_workwechat_chatroom_from_message(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    conversation: Conversation | None = None,
) -> dict[str, bool | str]:
    # 消息回调是最可靠的第一手样本：即使主动群列表接口暂时不可用，也能先把群和发言成员落库。
    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    if str(conversation_payload.get("chat_type") or "").strip() != "group":
        return {"chatroom_created": False, "member_created": False, "chatroom_id": "", "member_external_id": ""}

    chatroom_external_id = str(conversation_payload.get("external_id") or "").strip()
    if not chatroom_external_id:
        return {"chatroom_created": False, "member_created": False, "chatroom_id": "", "member_external_id": ""}

    synced_at = _resolve_synced_at((normalized.get("details") or {}).get("timestamp"))
    chatroom, chatroom_created = _get_or_create_chatroom(
        db,
        channel=channel,
        external_id=chatroom_external_id,
        status="message_observed",
    )
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=synced_at,
        registration_source="passive",
        enable_management=chatroom_created,
    )

    chatroom.name = _infer_chatroom_name(conversation_payload, conversation, chatroom_external_id, current_name=chatroom.name)
    chatroom.status = str(chatroom.status or "").strip() or "message_observed"
    chatroom.last_synced_at = synced_at
    chatroom.extra_json = json.dumps(
        {
            # 这里明确记录来源，后续排查时能区分是 Hook 主动同步还是消息被动观测生成。
            "source": "workwechat_message_ingest",
            "chat_type": "group",
            "last_event_key": normalized.get("event_key"),
            "last_sender_id": message_payload.get("sender_id"),
            "last_sender_name": message_payload.get("sender_name"),
            "last_message_type": message_payload.get("message_type"),
            "last_message_preview": str(message_payload.get("content") or "")[:120],
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    member_external_id = str(message_payload.get("sender_id") or "").strip()
    member_created = False
    if member_external_id:
        member, member_created = _get_or_create_member(
            db,
            channel=channel,
            chatroom=chatroom,
            member_external_id=member_external_id,
        )
        member.nickname = str(message_payload.get("sender_name") or member_external_id).strip() or member_external_id
        member.display_name = member.nickname
        member.role = member.role or "member"
        member.status = "active"
        member.last_synced_at = synced_at
        member.last_active_at = synced_at
        member.extra_json = json.dumps(
            {
                "source": "workwechat_message_ingest",
                "observed_via": "group_message",
                "last_event_key": normalized.get("event_key"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        # 这里先 flush 新增成员，再按数据库真实记录回写群成员数，避免 Session 关闭 autoflush 时统计少一条。
        db.flush()
        chatroom.member_count = _count_active_members(db, chatroom.id)

    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source="workwechat_message_ingest",
    )
    profile_sync_result = _sync_new_chatroom_profile_if_needed(
        db,
        channel=channel,
        chatroom=chatroom,
        conversation=conversation,
        chatroom_created=chatroom_created,
    )
    db.flush()
    return {
        "chatroom_created": chatroom_created,
        "member_created": member_created,
        "chatroom_id": chatroom_external_id,
        "member_external_id": member_external_id,
        "profile_sync_status": str(profile_sync_result.get("status") or ""),
    }


def sync_workwechat_chatrooms(db: Session, channel: Channel) -> dict[str, Any]:
    # 群列表同步优先调用 provider 的 /chatrooms/list，对应底层 Hook type=11038；失败时保留本地会话样本。
    materialized = _materialize_chatrooms_from_conversations(db, channel)
    provider_result = _fetch_provider_chatrooms(channel)
    failed = bool(provider_result and not provider_result.get("ok"))
    applied = apply_workwechat_provider_chatroom_list(db, channel, provider_result)
    rooms = applied["rooms"]
    provider_created = int(applied["created"])
    provider_updated = int(applied["updated"])

    db.flush()
    if provider_result and provider_result.get("ok"):
        summary = f"已从企微 Hook 同步 {len(rooms)} 个群，新增 {provider_created} 个，更新 {provider_updated} 个。"
    elif failed:
        summary = f"企微 Hook 群列表同步失败，已保留本地消息样本；错误：{provider_result.get('error') or provider_result.get('detail') or 'unknown'}。"
    else:
        summary = materialized["last_status_summary"]
    return {
        "total": len(rooms) if rooms else materialized["total"],
        "created": provider_created + int(materialized["created"]),
        "updated": provider_updated + int(materialized["updated"]),
        "provider_response": provider_result,
        "last_status_summary": summary,
    }


def apply_workwechat_provider_chatroom_list(db: Session, channel: Channel, provider_result: dict[str, Any] | None) -> dict[str, Any]:
    # 直连 provider 和云端 provider 回传的群列表都走这里落库，保证两种部署模式下字段归一化完全一致。
    rooms = _extract_provider_items(provider_result, "rooms")
    synced_at = now_shanghai()
    provider_created = 0
    provider_updated = 0

    for room in rooms:
        normalized = _normalize_room_item(room)
        if not normalized["external_id"]:
            continue
        chatroom, created = _get_or_create_chatroom(
            db,
            channel=channel,
            external_id=normalized["external_id"],
            status="profile_synced",
        )
        provider_created += 1 if created else 0
        provider_updated += 0 if created else 1
        _apply_provider_chatroom_profile(
            db,
            channel=channel,
            chatroom=chatroom,
            room=room,
            normalized=normalized,
            synced_at=synced_at,
            enable_management=created,
        )

    return {"rooms": rooms, "created": provider_created, "updated": provider_updated}


def sync_workwechat_chatroom_members(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    # 单群成员同步调用 provider 的 /chatrooms/members，对应底层 Hook type=11040，并用返回结果刷新成员快照。
    provider_result = _fetch_provider_chatroom_members(channel, chatroom.external_id)
    if not provider_result or not provider_result.get("ok"):
        return {
            "ok": False,
            "synced_members": 0,
            "last_status_summary": f"企微 Hook 群成员同步失败：{(provider_result or {}).get('error') or (provider_result or {}).get('detail') or 'provider unavailable'}",
            "provider_response": provider_result,
        }
    applied = apply_workwechat_provider_chatroom_members(db, channel, chatroom, provider_result)
    return {
        "ok": True,
        "synced_members": applied["synced_members"],
        "last_status_summary": f"已同步企微群 {chatroom.name or chatroom.external_id} 的成员快照 {applied['synced_members']} 条。",
        "provider_response": provider_result,
    }


def sync_workwechat_chatroom_profile_from_list(
    db: Session,
    channel: Channel,
    chatroom: Chatroom,
    *,
    conversation: Conversation | None = None,
) -> dict[str, Any]:
    """使用全量群列表接口刷新指定企微群资料。

    当前 Hook 只提供 /chatrooms/list 全量列表，没有稳定的单群资料详情接口。
    因此这里仍然请求全量列表，但只把匹配当前 external_id 的群资料落库，避免自动补资料时误创建其它群。
    """

    provider_result = _fetch_provider_chatrooms(channel)
    if not provider_result or not provider_result.get("ok"):
        return {
            "ok": False,
            "status": "provider_unavailable",
            "updated": False,
            "provider_response": provider_result,
            "last_status_summary": f"企微 Hook 群资料同步失败：{(provider_result or {}).get('error') or (provider_result or {}).get('detail') or 'provider unavailable'}",
        }
    applied = apply_workwechat_provider_chatroom_profile(db, channel, chatroom, provider_result, conversation=conversation)
    return {
        "ok": bool(applied.get("updated")),
        "status": "updated" if applied.get("updated") else "not_found",
        "updated": bool(applied.get("updated")),
        "provider_response": provider_result,
        "last_status_summary": (
            f"已从企微 Hook 群列表刷新 {chatroom.name or chatroom.external_id} 的群资料。"
            if applied.get("updated")
            else f"企微 Hook 群列表中未找到当前群 {chatroom.external_id}。"
        ),
    }


def apply_workwechat_provider_chatroom_profile(
    db: Session,
    channel: Channel,
    chatroom: Chatroom,
    provider_result: dict[str, Any] | None,
    *,
    conversation: Conversation | None = None,
) -> dict[str, Any]:
    """从全量 provider 结果中挑出当前群并刷新资料。

    云端控制任务和直连模式都复用这个函数，保证“自动新群补资料”和“详情页单群同步”的字段映射一致。
    """

    rooms = _extract_provider_items(provider_result, "rooms")
    synced_at = now_shanghai()
    target_external_id = str(chatroom.external_id or "").strip()
    for room in rooms:
        normalized = _normalize_room_item(room)
        if normalized["external_id"] != target_external_id:
            continue
        _apply_provider_chatroom_profile(
            db,
            channel=channel,
            chatroom=chatroom,
            room=room,
            normalized=normalized,
            synced_at=synced_at,
            enable_management=False,
        )
        _refresh_conversation_title_from_chatroom(conversation, normalized["name"], target_external_id)
        return {"updated": True, "rooms": rooms}
    return {"updated": False, "rooms": rooms}


def apply_workwechat_provider_chatroom_members(db: Session, channel: Channel, chatroom: Chatroom, provider_result: dict[str, Any] | None) -> dict[str, Any]:
    # 云端控制任务和直连 provider 都会回传 /chatrooms/members 同构结果；这里集中刷新成员快照，避免两套字段映射漂移。
    members = _extract_provider_items(provider_result, "members")
    synced_at = now_shanghai()

    normalized_members = [_normalize_member_item(item) for item in members]
    normalized_members = [item for item in normalized_members if item["member_external_id"]]
    _replace_chatroom_members(
        db,
        channel=channel,
        chatroom=chatroom,
        members=normalized_members,
        synced_at=synced_at,
        source="workwechat_provider_member_list",
    )
    chatroom.status = "members_synced"
    chatroom.member_count = len(normalized_members)
    chatroom.last_synced_at = synced_at
    extra = _parse_json(chatroom.extra_json)
    extra.update(
        {
            "source": "workwechat_provider_member_list",
            "member_payload_present": True,
            "member_count": len(normalized_members),
        }
    )
    chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source="workwechat_provider_member_list",
    )
    db.flush()
    return {
        "ok": True,
        "synced_members": len(normalized_members),
        "last_status_summary": f"已同步企微群 {chatroom.name or chatroom.external_id} 的成员快照 {len(normalized_members)} 条。",
        "provider_response": provider_result,
    }


def build_workwechat_chatroom_list(db: Session, channel: Channel) -> dict[str, Any]:
    # 页面读取只整理本地样本，不主动打 Hook；主动同步由“同步企微群列表”按钮触发。
    _materialize_chatrooms_from_conversations(db, channel)
    chatrooms = db.scalars(
        select(Chatroom)
        .where(Chatroom.channel_id == channel.id)
        .order_by(Chatroom.last_synced_at.desc(), Chatroom.id.desc())
    ).all()
    bindings = db.scalars(select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id).order_by(BotPlugin.id.asc())).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(GroupPluginScope.channel_id == channel.id)
        .order_by(GroupPluginScope.chatroom_id.asc(), GroupPluginScope.bot_plugin_id.asc())
    ).all()
    member_count_rows = db.execute(
        select(ChatroomMember.chatroom_id, func.count(ChatroomMember.id))
        .where(ChatroomMember.channel_id == channel.id, ChatroomMember.status != "left")
        .group_by(ChatroomMember.chatroom_id)
    ).all()
    local_member_count_map = {int(chatroom_id): int(count or 0) for chatroom_id, count in member_count_rows}

    binding_enabled_map = {binding.id: str(binding.status or "").strip() == "enabled" for binding in bindings}
    scope_map_by_chatroom: dict[int, dict[int, GroupPluginScope]] = {}
    for scope in scope_rows:
        scope_map_by_chatroom.setdefault(int(scope.chatroom_id), {})[int(scope.bot_plugin_id)] = scope

    items: list[dict[str, Any]] = []
    inherited_chatroom_count = 0
    overridden_chatroom_count = 0
    for chatroom in chatrooms:
        scope_map = scope_map_by_chatroom.get(chatroom.id, {})
        overridden_scope_count = sum(1 for scope in scope_map.values() if str(scope.scope_status or "").strip() != "inherited")
        effective_enabled_count = _count_effective_enabled_bindings(bindings, binding_enabled_map, scope_map)
        if overridden_scope_count:
            overridden_chatroom_count += 1
        else:
            inherited_chatroom_count += 1
        local_member_count = local_member_count_map.get(chatroom.id, 0)
        member_count = max(int(chatroom.member_count or 0), local_member_count)
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
                "sync_depth_label": _sync_depth_label(chatroom.status, local_member_count),
                "member_count": member_count,
                "member_record_count": local_member_count,
                "member_role_summary": _member_role_summary(db, chatroom.id),
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
        "member_snapshot_chatroom_count": sum(1 for item in items if int(item["member_record_count"] or 0) > 0),
        "pending_chatroom_count": sum(1 for item in items if item["management_status"] == "discovered"),
        "active_chatroom_count": sum(1 for item in items if item["is_active"]),
        "disabled_chatroom_count": sum(1 for item in items if item["management_status"] == "disabled"),
        "inherited_chatroom_count": inherited_chatroom_count,
        "overridden_chatroom_count": overridden_chatroom_count,
        "supports_sync": True,
        "sync_action": f"/channels/{channel.id}/workwechat/sync-chatrooms",
        "sync_button_label": "同步企微群列表",
        # 租户页面只说明可执行的业务动作，不暴露 Hook 接口和消息观测等内部实现。
        "intro_summary": "集中查看已同步的企业微信群聊和运营状态。",
        "empty_state_message": "暂无企业微信群聊，可以先同步群列表。",
    }


def build_workwechat_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    # 详情页组合群资料、成员快照、群消息流水和群级插件作用域，保持和 wechat 渠道页面结构一致。
    _materialize_chatrooms_from_conversations(db, channel)
    bindings = db.scalars(select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id).order_by(BotPlugin.id.asc())).all()
    members = db.scalars(
        select(ChatroomMember)
        .where(ChatroomMember.chatroom_id == chatroom.id)
        .order_by(ChatroomMember.status.asc(), ChatroomMember.role.asc(), ChatroomMember.display_name.asc(), ChatroomMember.nickname.asc(), ChatroomMember.id.asc())
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
            source="workwechat_chatroom_detail_view",
        )

    binding_items, binding_summary = _build_binding_items(bindings, scope_rows)
    member_items = [_format_member_item(member) for member in members]
    message_flow = _build_message_flow(db, channel, chatroom)
    role_counts = _build_role_counts(members)
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
        "member_count": max(int(chatroom.member_count or 0), len([item for item in members if item.status != "left"])),
        "member_record_count": len(member_items),
        "last_synced_at": _format_dt(chatroom.last_synced_at),
        "observed_source": _extract_chatroom_source(chatroom),
        "sync_depth_label": _sync_depth_label(chatroom.status, len(member_items)),
        "role_counts": role_counts,
        "members": member_items,
        "bindings": binding_items,
        "supports_sync": True,
        "supports_member_snapshot": True,
        "sync_action": f"/channels/{channel.id}/chatrooms/{chatroom.id}/sync",
        "sync_button_label": "同步当前群成员",
        "member_section_hint": "成员快照来自企微 Hook 11040；如果暂未同步，先展示群消息中出现过的发言成员。",
        "message_flow": message_flow,
        "binding_summary": binding_summary,
    }


def update_workwechat_chatroom_scope_enabled(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    enabled: bool,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_enabled(db, channel=channel, chatroom=chatroom, bot_plugin_id=bot_plugin_id, enabled=enabled, operator_id=operator_id, audit_note=audit_note, source="workwechat_chatroom_scope_toggle")


def reset_workwechat_chatroom_scope(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return reset_group_plugin_scope_to_inherited(db, channel=channel, chatroom=chatroom, bot_plugin_id=bot_plugin_id, operator_id=operator_id, audit_note=audit_note, source="workwechat_chatroom_scope_reset")


def update_workwechat_chatroom_scope_config_override(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    config_override_json: str | None,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_config_override(db, channel=channel, chatroom=chatroom, bot_plugin_id=bot_plugin_id, config_override_json=config_override_json, operator_id=operator_id, audit_note=audit_note, source="workwechat_chatroom_scope_config_override")


def _fetch_provider_chatrooms(channel: Channel) -> dict[str, Any] | None:
    return _post_provider_json(channel, PROVIDER_CHATROOM_LIST_PATH, {"page_size": 100, "max_pages": 20})


def _fetch_provider_chatroom_members(channel: Channel, conversation_id: str) -> dict[str, Any] | None:
    return _post_provider_json(channel, PROVIDER_CHATROOM_MEMBERS_PATH, {"conversation_id": conversation_id, "page_size": 100, "max_pages": 30})


def _post_provider_json(channel: Channel, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    if not channel.api_base:
        return None
    adapter = WorkWechatAdapter()
    adapter_config = adapter._load_adapter_config(channel)
    target_url = adapter._build_absolute_url(channel.api_base, path)
    headers = adapter._build_headers(channel, body, adapter_config)
    timeout = adapter._resolve_timeout(body, adapter_config)
    try:
        return adapter._post_json(target_url, body, headers=headers, timeout=timeout)
    except Exception as exc:
        logger.exception("workwechat provider chatroom request failed", extra={"channel_id": channel.id, "target_url": target_url})
        return {"ok": False, "error": str(exc), "target_url": target_url}


def _sync_new_chatroom_profile_if_needed(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    conversation: Conversation | None,
    chatroom_created: bool,
) -> dict[str, Any]:
    """新群首次被动纳管后，自动补一次中文群资料。

    企微消息回调经常只带 R: 开头的会话 ID，不带中文群名；继续使用全量群列表接口可拿到真实名称。
    这里限定只在“新群刚创建且展示名不可读”时触发，避免每条消息都请求 provider。
    """

    if not chatroom_created or not _needs_chatroom_profile_refresh(chatroom.name, chatroom.external_id):
        return {"status": "skipped"}
    try:
        from app.services.workwechat_cloud_poll import is_cloud_poll_enabled, notify_cloud_job_enqueued
        from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job

        if is_cloud_poll_enabled(channel):
            job = _enqueue_cloud_chatroom_profile_sync_job(
                db,
                channel=channel,
                chatroom=chatroom,
                job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                enqueue_job_func=enqueue_job,
            )
            if job:
                notify_cloud_job_enqueued(channel, job)
                return {"status": "queued", "job_id": job.id}
            return {"status": "deduplicated"}
        return sync_workwechat_chatroom_profile_from_list(db, channel, chatroom, conversation=conversation)
    except Exception as exc:
        # 自动补资料不能影响消息入库和插件调度；失败只记录日志，后续仍可手动同步群列表补齐。
        logger.exception(
            "workwechat auto chatroom profile sync failed",
            extra={"channel_id": channel.id, "chatroom_id": chatroom.id, "external_id": chatroom.external_id, "error": str(exc)},
        )
        return {"status": "failed", "error": str(exc)}


def _enqueue_cloud_chatroom_profile_sync_job(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    job_type: str,
    enqueue_job_func,
) -> TaskJob | None:
    # 同一个群资料同步任务如果还在等待/执行中，就不重复下发，避免新群连续消息把云端控制队列堆满。
    existing = db.scalar(
        select(TaskJob).where(
            TaskJob.job_type == job_type,
            TaskJob.status.in_(("pending", "running")),
            TaskJob.related_type == "chatroom",
            TaskJob.related_id == chatroom.id,
        )
    )
    if existing:
        return None
    return enqueue_job_func(
        db,
        job_type=job_type,
        payload={
            "channel_id": channel.id,
            "action": "sync_chatrooms",
            "target_chatroom_id": chatroom.id,
            "target_external_id": chatroom.external_id,
            "payload": {"page_size": 100, "max_pages": 20, "target_external_id": chatroom.external_id},
        },
        priority=95,
        related_type="chatroom",
        related_id=chatroom.id,
        max_attempts=3,
    )


def _apply_provider_chatroom_profile(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    room: dict[str, Any],
    normalized: dict[str, Any],
    synced_at: datetime,
    enable_management: bool,
) -> None:
    # provider 群列表是当前唯一稳定的企微群资料来源；字段更新集中在这里，避免全量同步和单群补资料行为不一致。
    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=synced_at,
        registration_source="passive",
        enable_management=enable_management,
    )
    chatroom.name = normalized["name"] or chatroom.name or normalized["external_id"]
    chatroom.owner_external_id = normalized["owner_external_id"] or chatroom.owner_external_id
    chatroom.member_count = normalized["member_count"] if normalized["member_count"] >= 0 else int(chatroom.member_count or 0)
    chatroom.status = "profile_synced"
    chatroom.last_synced_at = synced_at
    chatroom.extra_json = json.dumps(
        {
            "source": "workwechat_provider_chatroom_list",
            "raw_room": room,
            "is_external": normalized["is_external"],
            "is_admin": normalized["is_admin"],
            "is_creator": normalized["is_creator"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    materialize_group_plugin_scopes_for_chatroom(
        db,
        channel=channel,
        chatroom=chatroom,
        source="workwechat_provider_chatroom_list",
    )


def _extract_provider_items(provider_result: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    if not provider_result or not provider_result.get("ok"):
        return []
    raw_items = provider_result.get(key) or (provider_result.get("data") or {}).get(key)
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _materialize_chatrooms_from_conversations(db: Session, channel: Channel) -> dict[str, Any]:
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.channel_id == channel.id, Conversation.chat_type == "group")
        .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
    ).all()
    created_count = 0
    updated_count = 0
    for conversation in conversations:
        external_id = str(conversation.external_id or "").strip()
        if not external_id:
            continue
        chatroom, created = _get_or_create_chatroom(db, channel=channel, external_id=external_id, status="message_observed")
        created_count += 1 if created else 0
        updated_count += 0 if created else 1
        observed_at = conversation.last_message_at or conversation.created_at or now_shanghai()
        mark_chatroom_discovered(
            db,
            channel=channel,
            chatroom=chatroom,
            seen_at=observed_at,
            registration_source="passive",
            enable_management=created,
        )
        chatroom.name = _infer_chatroom_name(
            {"title": conversation.title},
            conversation,
            external_id,
            current_name=chatroom.name,
        )
        chatroom.last_synced_at = observed_at
        extra = _parse_json(chatroom.extra_json)
        extra.update({"source": extra.get("source") or "workwechat_conversation_materialize", "conversation_id": conversation.id, "chat_type": "group"})
        chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
        materialize_group_plugin_scopes_for_chatroom(db, channel=channel, chatroom=chatroom, source="workwechat_conversation_materialize")
    db.flush()
    return {
        "total": len(conversations),
        "created": created_count,
        "updated": updated_count,
        "last_status_summary": (
            f"已从企微群会话样本整理 {len(conversations)} 个群，新增 {created_count} 个，更新 {updated_count} 个。"
            if conversations
            else "当前还没有观测到企微群会话样本。"
        ),
    }


def _get_or_create_chatroom(db: Session, *, channel: Channel, external_id: str, status: str) -> tuple[Chatroom, bool]:
    chatroom = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id))
    if chatroom:
        return chatroom, False
    chatroom = Chatroom(bot_id=channel.bot_id, channel_id=channel.id, external_id=external_id, status=status)
    db.add(chatroom)
    db.flush()
    return chatroom, True


def _get_or_create_member(db: Session, *, channel: Channel, chatroom: Chatroom, member_external_id: str) -> tuple[ChatroomMember, bool]:
    member = db.scalar(
        select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id, ChatroomMember.member_external_id == member_external_id)
    )
    if member:
        return member, False
    member = ChatroomMember(chatroom_id=chatroom.id, bot_id=channel.bot_id, channel_id=channel.id, member_external_id=member_external_id, status="message_observed")
    db.add(member)
    db.flush()
    return member, True


def _replace_chatroom_members(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    members: list[dict[str, Any]],
    synced_at: datetime,
    source: str,
) -> None:
    existing_members = db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id)).all()
    existing_map = {item.member_external_id: item for item in existing_members}
    incoming_ids = {item["member_external_id"] for item in members}
    for existing in existing_members:
        if existing.member_external_id not in incoming_ids:
            existing.status = "left"
            existing.last_synced_at = synced_at
    for item in members:
        member = existing_map.get(item["member_external_id"])
        if not member:
            member = ChatroomMember(chatroom_id=chatroom.id, bot_id=channel.bot_id, channel_id=channel.id, member_external_id=item["member_external_id"])
            db.add(member)
        member.nickname = item["nickname"] or item["member_external_id"]
        member.display_name = item["display_name"] or member.nickname
        member.role = item["role"] or "member"
        member.status = "active"
        member.last_synced_at = synced_at
        member.extra_json = json.dumps(
            {
                "source": source,
                "observed_via": "hook_11040_member_list",
                "avatar": item.get("avatar") or "",
                "avatar_url": item.get("avatar_url") or "",
                "small_head_img_url": item.get("small_head_img_url") or "",
                "big_head_img_url": item.get("big_head_img_url") or "",
                "head_img_url": item.get("head_img_url") or "",
                "acctid": item.get("acctid") or "",
                "user_id": item.get("user_id") or "",
                "username": item.get("username") or "",
                "realname": item.get("realname") or "",
                "corp_id": item.get("corp_id") or "",
                "corp_name": item.get("corp_name") or "",
                "mobile": item.get("mobile") or "",
                "email": item.get("email") or "",
                "position": item.get("position") or "",
                "department": item.get("department") or "",
                "remark": item.get("remark") or "",
                "invite_user_id": item.get("invite_user_id") or "",
                "join_scene": item.get("join_scene") or "",
                "join_time": item.get("join_time") or "",
                "sex": item.get("sex") or "",
                # 企微 Hook 版本差异较大，保留原始成员对象，便于后续继续补字段映射。
                "raw_member": item.get("raw_member") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _normalize_room_item(item: dict[str, Any]) -> dict[str, Any]:
    member_count = _coerce_int(item.get("total") or item.get("member_count") or item.get("member_total"), default=0)
    return {
        "external_id": str(item.get("conversation_id") or item.get("room_conversation_id") or item.get("room_id") or "").strip(),
        "name": str(item.get("nickname") or item.get("room_name") or item.get("name") or "").strip(),
        "owner_external_id": str(item.get("create_user_id") or item.get("owner_external_id") or item.get("owner") or "").strip(),
        "member_count": member_count,
        "is_external": item.get("is_external"),
        "is_admin": item.get("is_admin"),
        "is_creator": item.get("is_creator"),
    }


def _normalize_member_item(item: dict[str, Any]) -> dict[str, Any]:
    external_id = _first_text(item, "user_id", "userid", "acctid", "member_external_id", "wxid")
    is_admin = _coerce_int(item.get("is_admin"), default=0)
    is_creator = _coerce_int(item.get("is_creator"), default=0)
    return {
        "member_external_id": external_id,
        "nickname": _first_text(item, "room_nickname", "nickname", "username", "realname", "name") or external_id,
        "display_name": _first_text(item, "room_nickname", "display_name", "username", "realname", "nickname"),
        "role": "owner" if is_creator else ("admin" if is_admin else "member"),
        "avatar": _resolve_avatar_url(item),
        "avatar_url": _first_text(item, "avatar_url", "head_img_url", "small_head_img_url", "big_head_img_url", "thumb_avatar"),
        "small_head_img_url": _first_text(item, "small_head_img_url", "smallHeadImgUrl", "small_head_url"),
        "big_head_img_url": _first_text(item, "big_head_img_url", "bigHeadImgUrl", "big_head_url"),
        "head_img_url": _first_text(item, "head_img_url", "headImgUrl", "head_url"),
        "acctid": _first_text(item, "acctid", "account", "alias"),
        "user_id": external_id,
        "username": _first_text(item, "username", "name"),
        "realname": _first_text(item, "realname", "real_name"),
        "corp_id": _first_text(item, "corp_id", "corpid"),
        "corp_name": _first_text(item, "corp_name", "corpName"),
        "mobile": _first_text(item, "mobile", "phone"),
        "email": _first_text(item, "email", "mail"),
        "position": _first_text(item, "position", "job", "title"),
        "department": _first_text(item, "department", "department_name", "dept_name"),
        "remark": _first_text(item, "remark", "remarks"),
        "invite_user_id": _first_text(item, "invite_user_id", "inviter_user_id", "inviter"),
        "join_scene": item.get("join_scene") or item.get("joinScene"),
        "join_time": item.get("join_time") or item.get("joinTime"),
        "sex": item.get("sex") or item.get("gender"),
        "raw_member": item,
    }


def _build_message_flow(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.channel_id == channel.id,
            Conversation.external_id == chatroom.external_id,
        )
    )
    if not conversation:
        return {"items": [], "total": 0, "conversation_id": None, "limit": MESSAGE_FLOW_LIMIT}
    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(MESSAGE_FLOW_LIMIT)
    ).all()
    from app.services.identity_directory import IdentityDirectory

    identities = IdentityDirectory(db).for_messages(messages)
    return {
        "items": [
            {
                "id": message.id,
                "direction": message.direction,
                "sender": identities[int(message.id)]["name"],
                "sender_id": message.sender_id or "",
                "identity": identities[int(message.id)],
                "message_type": message.message_type or "text",
                "status": message.status or "-",
                "content": message.content or "",
                "created_at": _format_dt(message.created_at),
            }
            for message in messages
        ],
        "total": len(messages),
        "conversation_id": conversation.id,
        "limit": MESSAGE_FLOW_LIMIT,
    }


def _build_binding_items(bindings: list[BotPlugin], scope_rows: list[GroupPluginScope]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scope_map = {item.bot_plugin_id: item for item in scope_rows}
    binding_items: list[dict[str, Any]] = []
    inherited_scope_count = 0
    overridden_scope_count = 0
    enabled_binding_count = 0
    effective_enabled_count = 0
    for binding in bindings:
        plugin = binding.plugin
        binding_enabled = str(binding.status or "").strip() == "enabled"
        enabled_binding_count += 1 if binding_enabled else 0
        scope = scope_map.get(binding.id)
        scope_status = str(getattr(scope, "scope_status", "") or "inherited")
        scope_enabled = bool(getattr(scope, "enabled", binding_enabled))
        inherited_scope_count += 1 if scope_status == "inherited" else 0
        overridden_scope_count += 0 if scope_status == "inherited" else 1
        effective_enabled_count += 1 if scope_enabled else 0
        binding_config = parse_config_json(binding.config_json)
        scope_config = parse_config_json(getattr(scope, "config_override_json", None))
        effective_config = _build_effective_group_config(plugin_code=plugin.code if plugin else None, binding_config=binding_config, scope_config=scope_config)
        binding_items.append(
            {
                "binding_id": binding.id,
                "plugin_code": plugin.code if plugin else "-",
                "plugin_name": plugin.display_name if plugin else f"Plugin #{binding.plugin_id}",
                # 群详情插件列表只展示一句话说明，技术字段默认折叠。
                "plugin_description": (plugin.description or "").strip() if plugin else "",
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
                "group_scope_summary": "当前没有群级覆盖配置；该群直接继承 bot_plugin 的启停与配置。" if scope_status == "inherited" else "当前已存在群级作用域覆盖记录；该群会优先使用单群启停和配置覆盖。",
                "group_scope_boundary_note": "workwechat 支持按群做插件启停；成员快照用于排查和后续成员级策略扩展。",
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
    summary = {
        "total": len(binding_items),
        "enabled": enabled_binding_count,
        "disabled": max(len(binding_items) - enabled_binding_count, 0),
        "materialized_scope_count": len(scope_rows),
        "inherited_scope_count": inherited_scope_count,
        "overridden_scope_count": overridden_scope_count,
        "effective_enabled_count": effective_enabled_count,
        "effective_disabled_count": max(len(binding_items) - effective_enabled_count, 0),
        "operator_summary": f"当前 bot 共绑定 {len(binding_items)} 个插件；企微群管理已落库 {len(scope_rows)} 条群级作用域记录，继承 {inherited_scope_count} / 覆盖 {overridden_scope_count}；当前有效启用 {effective_enabled_count} / 停用 {max(len(binding_items) - effective_enabled_count, 0)}。",
    }
    return binding_items, summary


def _format_member_item(member: ChatroomMember) -> dict[str, Any]:
    extra = _parse_json(member.extra_json)
    avatar_url = str(
        extra.get("avatar_url")
        or extra.get("avatar")
        or extra.get("small_head_img_url")
        or extra.get("head_img_url")
        or extra.get("big_head_img_url")
        or ""
    ).strip()
    corp_label = " / ".join(part for part in (extra.get("corp_name"), extra.get("corp_id"), extra.get("position")) if part)
    return {
        "member_external_id": member.member_external_id,
        "nickname": member.nickname or member.member_external_id,
        "display_name": member.display_name or "-",
        "role_label": _format_member_role(member.role),
        "member_flag_label": _format_member_status(member.status),
        "member_flag": str(extra.get("is_admin") or extra.get("member_flag") or "-"),
        "status_label": _format_member_status(member.status),
        "region_label": corp_label or extra.get("department") or "-",
        "alias": extra.get("acctid") or extra.get("user_id") or "-",
        "remark": extra.get("remark") or "-",
        "inviter_user_name": extra.get("invite_user_id") or "-",
        "has_avatar": bool(avatar_url),
        "avatar_url": avatar_url,
        "mobile": extra.get("mobile") or "-",
        "email": extra.get("email") or "-",
        "join_time": extra.get("join_time") or "-",
        "last_active_at": _format_dt(member.last_active_at),
        "last_synced_at": _format_dt(member.last_synced_at),
    }


def _build_role_counts(members: list[ChatroomMember]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for member in members:
        label = _format_member_role(member.role)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _count_effective_enabled_bindings(bindings: list[BotPlugin], binding_enabled_map: dict[int, bool], scope_map: dict[int, GroupPluginScope]) -> int:
    count = 0
    for binding in bindings:
        scope = scope_map.get(binding.id)
        if scope is not None:
            count += 1 if bool(scope.enabled) else 0
        elif binding_enabled_map.get(binding.id):
            count += 1
    return count


def _count_active_members(db: Session, chatroom_id: int) -> int:
    return int(db.scalar(select(func.count(ChatroomMember.id)).where(ChatroomMember.chatroom_id == chatroom_id, ChatroomMember.status != "left")) or 0)


def _member_role_summary(db: Session, chatroom_id: int) -> str:
    rows = db.execute(
        select(ChatroomMember.role, func.count(ChatroomMember.id))
        .where(ChatroomMember.chatroom_id == chatroom_id, ChatroomMember.status != "left")
        .group_by(ChatroomMember.role)
    ).all()
    if not rows:
        return "暂无成员快照"
    return " / ".join(f"{_format_member_role(role)} {int(count or 0)}" for role, count in rows)


def _infer_chatroom_name(
    conversation_payload: dict[str, Any],
    conversation: Conversation | None,
    external_id: str,
    *,
    current_name: str | None = None,
) -> str:
    """推断企微群展示名，并避免把已同步群名降级成 external_id。

    企微消息回调有时只给 `R:...` 会话 ID，不给群名；群列表接口后续才会给真实名称。
    因此当前已有可读名称时，不能被空标题或 external_id 覆盖。
    """
    external = str(external_id or "").strip()
    current = str(current_name or "").strip()
    incoming = str(conversation_payload.get("title") or getattr(conversation, "title", None) or "").strip()
    if incoming and incoming != external:
        return incoming
    if current and current != external:
        return current
    return incoming or current or external


def _needs_chatroom_profile_refresh(name: str | None, external_id: str | None) -> bool:
    external = str(external_id or "").strip()
    current = str(name or "").strip()
    # 空名称或直接显示 R: 技术 ID 时，说明消息回调没有给真实群名，需要通过群列表接口补资料。
    return bool(external) and (not current or current == external)


def _refresh_conversation_title_from_chatroom(conversation: Conversation | None, chatroom_name: str | None, external_id: str | None) -> None:
    if conversation is None:
        return
    resolved_name = str(chatroom_name or "").strip()
    external = str(external_id or "").strip()
    current = str(getattr(conversation, "title", None) or "").strip()
    if not resolved_name or resolved_name == external:
        return
    # Conversation 是消息清单使用的展示名；群资料补齐后同步更新，避免列表仍显示 R:xxx。
    if not current or current == external:
        conversation.title = resolved_name


def _extract_chatroom_source(chatroom: Chatroom) -> str:
    source = str(_parse_json(chatroom.extra_json).get("source") or "").strip()
    return {
        "workwechat_message_ingest": "企微消息观测",
        "workwechat_conversation_materialize": "企微会话整理",
        "workwechat_provider_chatroom_list": "Hook 群列表",
        "workwechat_provider_member_list": "Hook 成员列表",
    }.get(source, source or "本地记录")


def _sync_depth_label(status: str | None, member_record_count: int) -> str:
    if member_record_count > 0:
        return "成员快照"
    if str(status or "") == "profile_synced":
        return "群资料"
    return "消息样本"


def _format_chatroom_status(status: str | None) -> str:
    return {"message_observed": "已观测", "profile_synced": "资料已同步", "members_synced": "成员已同步", "pending_sync": "待同步"}.get(str(status or "").strip(), str(status or "").strip() or "未知")


def _format_member_role(role: str | None) -> str:
    return {"admin": "管理员", "owner": "群主", "member": "成员"}.get(str(role or "").strip(), str(role or "").strip() or "成员")


def _format_member_status(status: str | None) -> str:
    return {"active": "活跃", "left": "已离群", "message_observed": "消息观测"}.get(str(status or "").strip(), str(status or "").strip() or "未知")


def _first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _resolve_avatar_url(item: dict[str, Any]) -> str:
    """从不同 Hook 版本的头像字段中提取可展示 URL。"""
    avatar = item.get("avatar")
    if isinstance(avatar, dict):
        for key in ("small", "large", "url", "avatar_url", "head_img_url", "small_head_img_url"):
            text = str(avatar.get(key) or "").strip()
            if text:
                return text
    if avatar:
        text = str(avatar).strip()
        if text:
            return text
    return _first_text(
        item,
        "avatar_url",
        "small_head_img_url",
        "smallHeadImgUrl",
        "head_img_url",
        "headImgUrl",
        "big_head_img_url",
        "bigHeadImgUrl",
        "thumb_avatar",
    )


def _resolve_synced_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    text = str(value or "").strip()
    if text:
        try:
            if any(marker in text for marker in ("T", "-", ":")):
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return ensure_shanghai_datetime(parsed)
            numeric = int(text)
            if numeric > 10_000_000_000:
                numeric = numeric / 1000
            return datetime.fromtimestamp(numeric, tz=now_shanghai().tzinfo)
        except (ValueError, OSError, OverflowError):
            pass
    return now_shanghai()


def _build_effective_group_config(*, plugin_code: str | None, binding_config: dict[str, Any], scope_config: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {}
    schema = get_plugin_schema(plugin_code)
    if schema:
        for field in schema.fields:
            if field.default not in (None, "", []):
                base[field.name] = field.default
    return deep_merge_config(deep_merge_config(base, binding_config), scope_config)


def _format_config_for_editor(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) if config else ""


def _parse_json(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_dt(value: Any) -> str:
    return format_shanghai_time(value)


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
