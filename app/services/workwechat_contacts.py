from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Channel, WorkWechatContact, WorkWechatContactRelation
from app.services.channel_adapters.workwechat import WorkWechatAdapter
from app.core.logging import get_logger
from app.core.time import now_shanghai


logger = get_logger(__name__)
PROVIDER_CONTACT_LIST_PATH = "/contacts/list"


CONTACT_KIND_LABELS = {
    "internal_member": "内部成员",
    "external_contact": "外部联系人",
    "unknown": "未知",
}
SYNC_STATUS_LABELS = {
    "active": "有效",
    "deleted": "已删除",
    "missing": "同步缺失",
    "unknown": "未知",
}
RELATIONSHIP_STATUS_LABELS = {
    "active": "有效",
    "deleted_by_contact": "客户已删除我方账号",
    "deleted_by_staff": "我方已删除客户",
    "transferred": "已转接",
    "unknown": "未知",
}


def upsert_workwechat_contact(db: Session, channel: Channel, payload: dict[str, Any]) -> WorkWechatContact:
    normalized = normalize_contact_payload(payload)
    external_id = normalized["contact_external_id"]
    if not external_id:
        raise ValueError("contact_external_id is required")

    contact = db.scalar(
        select(WorkWechatContact).where(
            WorkWechatContact.channel_id == channel.id,
            WorkWechatContact.contact_external_id == external_id,
        )
    )
    now = now_shanghai()
    if contact is None:
        contact = WorkWechatContact(
            channel_id=channel.id,
            contact_external_id=external_id,
            first_seen_at=now,
        )
        db.add(contact)

    for key, value in normalized.items():
        if key == "relations":
            continue
        setattr(contact, key, value)
    contact.last_seen_at = now
    contact.last_synced_at = now if normalized["source"] != "manual" else contact.last_synced_at
    if contact.sync_status == "active":
        contact.deleted_at = None
        contact.delete_reason = None
        contact.delete_event_key = None
    db.flush()

    for relation_payload in normalized["relations"]:
        upsert_workwechat_contact_relation(db, channel=channel, contact=contact, payload=relation_payload)
    db.flush()
    return contact


def sync_workwechat_contacts(db: Session, channel: Channel, *, contact_kind: str = "all") -> dict[str, Any]:
    provider_result = _fetch_provider_contacts(channel, contact_kind=contact_kind)
    applied = apply_workwechat_provider_contact_list(db, channel, provider_result)
    synced = int(applied["synced_contacts"])
    if provider_result and provider_result.get("ok"):
        summary = f"已从企微 Hook 同步联系人 {synced} 条。"
    elif provider_result:
        summary = f"企微 Hook 联系人同步失败：{provider_result.get('error') or provider_result.get('detail') or 'unknown'}"
    else:
        summary = "当前渠道未配置 provider，无法同步企微联系人。"
    return {
        "ok": bool(provider_result and provider_result.get("ok")),
        "synced_contacts": synced,
        "provider_response": provider_result,
        "last_status_summary": summary,
    }


def apply_workwechat_provider_contact_list(db: Session, channel: Channel, provider_result: dict[str, Any] | None) -> dict[str, Any]:
    # 云端控制任务和直连 provider 都会返回 /contacts/list 同构结果；联系人字段归一化只保留在这一处，避免部署模式不同导致数据口径不同。
    contacts = _extract_provider_contacts(provider_result)
    synced = 0
    for item in contacts:
        payload = dict(item)
        payload["source"] = "provider_sync"
        payload["sync_status"] = "active"
        if not payload.get("contact_kind"):
            payload["contact_kind"] = _infer_contact_kind_from_provider_payload(payload)
        upsert_workwechat_contact(db, channel, payload)
        synced += 1
    db.flush()
    return {"synced_contacts": synced, "contacts": contacts}


def apply_workwechat_contact_event(db: Session, channel: Channel, normalized: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(normalized.get("event_type") or "").strip()
    details = normalized.get("details") if isinstance(normalized.get("details"), dict) else {}
    hook_type = _coerce_int(details.get("hook_type"))
    raw_data = details.get("raw_data") if isinstance(details.get("raw_data"), dict) else {}
    if event_type not in {"contact_added", "contact_deleted", "contact_request"} and hook_type not in {11063, 11076, 11077}:
        return None
    payload = dict(raw_data)
    payload["source"] = "webhook"
    payload["contact_kind"] = "external_contact"
    payload["sync_status"] = "active"
    if hook_type == 11077 or event_type == "contact_deleted":
        contact = upsert_workwechat_contact(db, channel, payload)
        mark_workwechat_contact_deleted(
            db,
            contact,
            reason="deleted_by_contact",
            event_key=str(normalized.get("event_key") or "").strip() or None,
        )
        return {"handled": True, "action": "deleted", "contact_id": contact.id}
    if hook_type == 11076 or event_type == "contact_added":
        contact = upsert_workwechat_contact(db, channel, payload)
        return {"handled": True, "action": "upserted", "contact_id": contact.id}
    if hook_type == 11063 or event_type == "contact_request":
        payload["sync_status"] = "unknown"
        contact = upsert_workwechat_contact(db, channel, payload)
        return {"handled": True, "action": "request_observed", "contact_id": contact.id}
    return None


def upsert_workwechat_contact_relation(
    db: Session,
    *,
    channel: Channel,
    contact: WorkWechatContact,
    payload: dict[str, Any],
) -> WorkWechatContactRelation:
    normalized = normalize_relation_payload(payload)
    owner_userid = normalized["owner_userid"]
    query = select(WorkWechatContactRelation).where(
        WorkWechatContactRelation.channel_id == channel.id,
        WorkWechatContactRelation.contact_id == contact.id,
    )
    if owner_userid:
        query = query.where(WorkWechatContactRelation.owner_userid == owner_userid)
    else:
        query = query.where(WorkWechatContactRelation.owner_userid.is_(None))
    relation = db.scalar(query)
    now = now_shanghai()
    if relation is None:
        relation = WorkWechatContactRelation(
            channel_id=channel.id,
            contact_id=contact.id,
            owner_userid=owner_userid or None,
        )
        db.add(relation)

    for key, value in normalized.items():
        setattr(relation, key, value)
    relation.last_seen_at = now
    relation.last_synced_at = now
    if not relation.is_deleted:
        relation.deleted_at = None
        relation.delete_reason = None
        relation.delete_event_key = None
    db.flush()
    return relation


def build_workwechat_contact_list(
    db: Session,
    channel: Channel,
    *,
    query: str = "",
    sync_status: str = "",
    contact_kind: str = "",
) -> dict[str, Any]:
    stmt = select(WorkWechatContact).where(WorkWechatContact.channel_id == channel.id)
    normalized_query = str(query or "").strip()
    if normalized_query:
        pattern = f"%{normalized_query}%"
        stmt = stmt.where(
            or_(
                WorkWechatContact.name.ilike(pattern),
                WorkWechatContact.display_name.ilike(pattern),
                WorkWechatContact.alias.ilike(pattern),
                WorkWechatContact.mobile.ilike(pattern),
                WorkWechatContact.email.ilike(pattern),
                WorkWechatContact.contact_external_id.ilike(pattern),
                WorkWechatContact.conversation_external_id.ilike(pattern),
                WorkWechatContact.corp_name.ilike(pattern),
            )
        )
    if sync_status:
        stmt = stmt.where(WorkWechatContact.sync_status == sync_status)
    if contact_kind:
        stmt = stmt.where(WorkWechatContact.contact_kind == contact_kind)
    contacts = db.scalars(stmt.order_by(WorkWechatContact.updated_at.desc(), WorkWechatContact.id.desc())).all()
    status_counts = dict(
        db.execute(
            select(WorkWechatContact.sync_status, func.count(WorkWechatContact.id))
            .where(WorkWechatContact.channel_id == channel.id)
            .group_by(WorkWechatContact.sync_status)
        ).all()
    )
    kind_counts = dict(
        db.execute(
            select(WorkWechatContact.contact_kind, func.count(WorkWechatContact.id))
            .where(WorkWechatContact.channel_id == channel.id)
            .group_by(WorkWechatContact.contact_kind)
        ).all()
    )
    return {
        "items": [_format_contact_item(contact) for contact in contacts],
        "total": len(contacts),
        "status_counts": {str(key or "unknown"): int(value or 0) for key, value in status_counts.items()},
        "kind_counts": {str(key or "unknown"): int(value or 0) for key, value in kind_counts.items()},
        "filters": {
            "query": normalized_query,
            "sync_status": sync_status,
            "contact_kind": contact_kind,
        },
        "supports_sync": False,
        "intro_summary": "联系人是渠道级资料，独立于群成员快照；删除只标记状态，不物理删除。",
    }


def build_workwechat_contact_summary(db: Session, channel: Channel) -> dict[str, Any]:
    total = int(db.scalar(select(func.count(WorkWechatContact.id)).where(WorkWechatContact.channel_id == channel.id)) or 0)
    active = int(
        db.scalar(
            select(func.count(WorkWechatContact.id)).where(
                WorkWechatContact.channel_id == channel.id,
                WorkWechatContact.sync_status == "active",
            )
        )
        or 0
    )
    deleted = int(
        db.scalar(
            select(func.count(WorkWechatContact.id)).where(
                WorkWechatContact.channel_id == channel.id,
                WorkWechatContact.sync_status == "deleted",
            )
        )
        or 0
    )
    recent = db.scalars(
        select(WorkWechatContact)
        .where(WorkWechatContact.channel_id == channel.id)
        .order_by(WorkWechatContact.updated_at.desc(), WorkWechatContact.id.desc())
        .limit(6)
    ).all()
    return {
        "total": total,
        "active": active,
        "deleted": deleted,
        "recent_contacts": [_format_contact_item(contact) for contact in recent],
        "list_url": f"/channels/{channel.id}/workwechat/contacts",
        "create_url": f"/channels/{channel.id}/workwechat/contacts/new",
        "intro_summary": "渠道级企微联系人，不与群成员混用。",
    }


def build_workwechat_contact_detail(db: Session, channel: Channel, contact: WorkWechatContact) -> dict[str, Any]:
    relations = db.scalars(
        select(WorkWechatContactRelation)
        .where(WorkWechatContactRelation.channel_id == channel.id, WorkWechatContactRelation.contact_id == contact.id)
        .order_by(WorkWechatContactRelation.id.asc())
    ).all()
    item = _format_contact_item(contact)
    item.update(
        {
            "relations": [_format_relation_item(relation) for relation in relations],
            "raw_contact": _parse_json(contact.raw_contact_json),
            "external_profile": _parse_json(contact.external_profile_json),
            "extattr": _parse_json(contact.extattr_json),
            "department_ids": _parse_json(contact.department_ids_json, default=[]),
        }
    )
    return item


def mark_workwechat_contact_deleted(
    db: Session,
    contact: WorkWechatContact,
    *,
    reason: str,
    event_key: str | None = None,
) -> WorkWechatContact:
    now = now_shanghai()
    contact.sync_status = "deleted"
    contact.deleted_at = now
    contact.delete_reason = str(reason or "manual_archive").strip() or "manual_archive"
    contact.delete_event_key = str(event_key or "").strip() or None
    for relation in contact.relations:
        relation.relationship_status = _relation_status_for_delete_reason(contact.delete_reason)
        relation.is_deleted = True
        relation.deleted_at = now
        relation.delete_reason = contact.delete_reason
        relation.delete_event_key = contact.delete_event_key
    db.flush()
    return contact


def restore_workwechat_contact(db: Session, contact: WorkWechatContact) -> WorkWechatContact:
    contact.sync_status = "active"
    contact.deleted_at = None
    contact.delete_reason = None
    contact.delete_event_key = None
    for relation in contact.relations:
        relation.relationship_status = "active"
        relation.is_deleted = False
        relation.deleted_at = None
        relation.delete_reason = None
        relation.delete_event_key = None
    db.flush()
    return contact


def normalize_contact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    external_userid = _first_text(raw, "external_userid", "external_user_id", "ExternalUserID")
    userid = _first_text(raw, "userid", "user_id", "UserID", "acctid")
    contact_external_id = _first_text(raw, "contact_external_id", "external_id") or external_userid or userid
    contact_kind = _normalize_contact_kind(raw.get("contact_kind") or raw.get("kind"), external_userid=external_userid, userid=userid)
    source = _normalize_source(raw.get("source"))
    sync_status = _normalize_sync_status(raw.get("sync_status") or raw.get("status_key"))
    return {
        "contact_kind": contact_kind,
        "contact_external_id": contact_external_id,
        "conversation_external_id": _first_text(raw, "conversation_external_id", "conversation_id"),
        "external_userid": external_userid or None,
        "userid": userid or None,
        "open_userid": _first_text(raw, "open_userid", "openUserId"),
        "unionid": _first_text(raw, "unionid", "union_id"),
        "name": _first_text(raw, "name", "Name", "nickname", "realname", "username"),
        "display_name": _first_text(raw, "display_name", "displayName", "remark", "name", "nickname"),
        "alias": _first_text(raw, "alias", "Alias", "acctid"),
        "english_name": _first_text(raw, "english_name", "englishName"),
        "avatar_url": _first_text(raw, "avatar_url", "avatar", "head_img_url", "thumb_avatar"),
        "gender": _normalize_gender(raw.get("gender") or raw.get("sex")),
        "contact_type": _normalize_contact_type(raw.get("type") or raw.get("contact_type")),
        "corp_name": _first_text(raw, "corp_name", "corpName"),
        "corp_full_name": _first_text(raw, "corp_full_name", "corpFullName"),
        "corp_id": _first_text(raw, "corp_id", "corpid"),
        "position": _first_text(raw, "position", "job", "title"),
        "mobile": _first_text(raw, "mobile", "phone"),
        "email": _first_text(raw, "email", "mail"),
        "biz_mail": _first_text(raw, "biz_mail", "bizMail"),
        "telephone": _first_text(raw, "telephone", "tel"),
        "address": _first_text(raw, "address"),
        "department_ids_json": _json_or_none(raw.get("department") or raw.get("department_ids")),
        "main_department_id": _coerce_int(raw.get("main_department") or raw.get("main_department_id")),
        "is_leader_in_dept_json": _json_or_none(raw.get("is_leader_in_dept")),
        "direct_leader_json": _json_or_none(raw.get("direct_leader") or raw.get("direct_leaders")),
        "hide_mobile": _coerce_bool(raw.get("hide_mobile")),
        "enable": _coerce_bool(raw.get("enable"), default=True),
        "member_status": _first_text(raw, "member_status", "status"),
        "external_profile_json": _json_or_none(raw.get("external_profile")),
        "extattr_json": _json_or_none(raw.get("extattr")),
        "raw_contact_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
        "source": source,
        "sync_status": sync_status,
        "relations": _normalize_relation_list(raw),
    }


def normalize_relation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    relationship_status = _normalize_relationship_status(raw.get("relationship_status") or raw.get("status"))
    is_deleted = bool(raw.get("is_deleted")) or relationship_status.startswith("deleted_")
    return {
        "owner_userid": _first_text(raw, "owner_userid", "userid", "user_id"),
        "owner_name": _first_text(raw, "owner_name", "name", "username"),
        "relationship_status": relationship_status,
        "is_deleted": is_deleted,
        "deleted_at": _parse_datetime(raw.get("deleted_at")),
        "delete_reason": _first_text(raw, "delete_reason"),
        "delete_event_key": _first_text(raw, "delete_event_key", "event_key"),
        "remark": _first_text(raw, "remark"),
        "description": _first_text(raw, "description"),
        "remark_corp_name": _first_text(raw, "remark_corp_name", "remarkCorpName"),
        "remark_mobiles_json": _json_or_none(raw.get("remark_mobiles")),
        "tags_json": _json_or_none(raw.get("tags")),
        "add_way": _first_text(raw, "add_way", "addWay"),
        "state": _first_text(raw, "state"),
        "oper_userid": _first_text(raw, "oper_userid", "operUserID"),
        "createtime": _parse_datetime(raw.get("createtime") or raw.get("create_time")),
        "raw_follow_user_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
    }


def _format_contact_item(contact: WorkWechatContact) -> dict[str, Any]:
    name = contact.display_name or contact.name or contact.alias or contact.contact_external_id
    return {
        "id": contact.id,
        "channel_id": contact.channel_id,
        "name": name,
        "contact_external_id": contact.contact_external_id,
        "conversation_external_id": contact.conversation_external_id or "-",
        "contact_kind": contact.contact_kind,
        "contact_kind_label": CONTACT_KIND_LABELS.get(contact.contact_kind, contact.contact_kind or "未知"),
        "external_userid": contact.external_userid or "-",
        "userid": contact.userid or "-",
        "alias": contact.alias or "-",
        "avatar_url": contact.avatar_url or "",
        "has_avatar": bool(contact.avatar_url),
        "corp_name": contact.corp_name or "-",
        "position": contact.position or "-",
        "mobile": contact.mobile or "-",
        "email": contact.email or "-",
        "sync_status": contact.sync_status,
        "sync_status_label": SYNC_STATUS_LABELS.get(contact.sync_status, contact.sync_status or "未知"),
        "source": contact.source or "manual",
        "deleted_at": contact.deleted_at,
        "delete_reason": contact.delete_reason or "-",
        "updated_at": contact.updated_at,
        "detail_url": f"/channels/{contact.channel_id}/workwechat/contacts/{contact.id}",
    }


def _format_relation_item(relation: WorkWechatContactRelation) -> dict[str, Any]:
    return {
        "id": relation.id,
        "owner_userid": relation.owner_userid or "-",
        "owner_name": relation.owner_name or "-",
        "relationship_status": relation.relationship_status,
        "relationship_status_label": RELATIONSHIP_STATUS_LABELS.get(relation.relationship_status, relation.relationship_status or "未知"),
        "is_deleted": bool(relation.is_deleted),
        "deleted_at": relation.deleted_at,
        "delete_reason": relation.delete_reason or "-",
        "remark": relation.remark or "-",
        "description": relation.description or "-",
        "remark_corp_name": relation.remark_corp_name or "-",
        "add_way": relation.add_way or "-",
        "state": relation.state or "-",
        "oper_userid": relation.oper_userid or "-",
        "createtime": relation.createtime,
    }


def _normalize_relation_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = raw.get("follow_user") or raw.get("follow_users") or raw.get("relations")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    if isinstance(candidates, dict):
        return [candidates]
    owner_userid = _first_text(raw, "owner_userid", "follow_userid")
    if owner_userid:
        return [{"owner_userid": owner_userid, "owner_name": _first_text(raw, "owner_name")}]
    return []


def _fetch_provider_contacts(channel: Channel, *, contact_kind: str) -> dict[str, Any] | None:
    if not channel.api_base:
        return None
    adapter = WorkWechatAdapter()
    adapter_config = adapter._load_adapter_config(channel)
    target_url = adapter._build_absolute_url(channel.api_base, PROVIDER_CONTACT_LIST_PATH)
    body = {"contact_kind": contact_kind, "page_size": 100, "max_pages": 50}
    headers = adapter._build_headers(channel, body, adapter_config)
    timeout = adapter._resolve_timeout(body, adapter_config)
    try:
        return adapter._post_json(target_url, body, headers=headers, timeout=timeout)
    except Exception as exc:
        logger.exception("workwechat provider contacts request failed", extra={"channel_id": channel.id, "target_url": target_url})
        return {"ok": False, "error": str(exc), "target_url": target_url}


def _extract_provider_contacts(provider_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not provider_result or not provider_result.get("ok"):
        return []
    raw_items = provider_result.get("contacts") or (provider_result.get("data") or {}).get("contacts")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _infer_contact_kind_from_provider_payload(payload: dict[str, Any]) -> str:
    if payload.get("external_userid"):
        return "external_contact"
    if payload.get("userid") or payload.get("user_id"):
        return "internal_member"
    return "unknown"


def _normalize_contact_kind(value: Any, *, external_userid: str, userid: str) -> str:
    raw = str(value or "").strip()
    if raw in CONTACT_KIND_LABELS:
        return raw
    if external_userid:
        return "external_contact"
    if userid:
        return "internal_member"
    return "unknown"


def _normalize_source(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw in {"manual", "provider_sync", "webhook", "api"} else "manual"


def _normalize_sync_status(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw in SYNC_STATUS_LABELS else "active"


def _normalize_relationship_status(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw in RELATIONSHIP_STATUS_LABELS else "active"


def _normalize_gender(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"1", "male", "m", "男"}:
        return "male"
    if raw in {"2", "female", "f", "女"}:
        return "female"
    return "unknown"


def _normalize_contact_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"1", "wechat"}:
        return "wechat"
    if raw in {"2", "workwechat", "wecom"}:
        return "workwechat"
    return raw or "unknown"


def _relation_status_for_delete_reason(reason: str) -> str:
    if reason in {"deleted_by_contact", "del_external_contact"}:
        return "deleted_by_contact"
    if reason in {"deleted_by_staff", "del_follow_user"}:
        return "deleted_by_staff"
    return "unknown"


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json(value: Any, *, default: Any | None = None) -> Any:
    if default is None:
        default = {}
    if not value:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=now_shanghai().tzinfo)
        except (OverflowError, ValueError):
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
