"""面向业务系统的账号、联系人和群资源开放接口。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.open_api import (
    _authenticate_request,
    _guard_request,
    _load_authenticated_app,
)
from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel, Chatroom, ChatroomMember, OpenApiApp, OpenApiAppChannel, WorkWechatContact
from app.services.open_api_capabilities import list_open_api_capabilities
from app.services.open_api_quotas import finalize_open_api_usage


router = APIRouter(tags=["open-api-resources"])


def _authorized_channel_ids(db, app: OpenApiApp) -> list[int]:
    """读取应用授权渠道，后续资源查询全部复用这一范围。"""

    return [
        int(value)
        for value in db.scalars(
            select(OpenApiAppChannel.channel_id)
            .where(
                OpenApiAppChannel.app_id == int(app.id),
                OpenApiAppChannel.tenant_id == int(app.tenant_id),
            )
            .order_by(OpenApiAppChannel.channel_id.asc())
        ).all()
    ]


def _page_limit(value: int, default: int = 50, maximum: int = 200) -> int:
    """统一限制开放接口分页大小，避免业务系统一次读取过大的快照。"""

    return min(max(1, int(value or default)), maximum)


def _cursor(value: str | None) -> int:
    """资源游标使用单调递增的数据库主键，非法游标统一按首屏处理。"""

    try:
        return max(0, int(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _channel_view(channel: Channel) -> dict[str, Any]:
    """只返回业务账号资料，不暴露协议地址、设备指纹和登录凭证。"""

    capabilities = [
        {
            "code": item.code,
            "title": item.title,
            "operation": item.operation,
            "status": item.status,
        }
        for item in list_open_api_capabilities(channel_type=channel.channel_type)
    ]
    return {
        "id": int(channel.id),
        "name": channel.name,
        "channel_type": channel.channel_type,
        "status": channel.status,
        "login_state": channel.login_state,
        "last_login_at": channel.last_login_at.isoformat() if channel.last_login_at else None,
        "last_message_sync_at": channel.last_message_sync_at.isoformat() if channel.last_message_sync_at else None,
        "capabilities": capabilities,
    }


def _contact_view(contact: WorkWechatContact) -> dict[str, Any]:
    """联系人响应只保留稳定业务字段，并隐藏手机号等敏感资料。"""

    return {
        "id": int(contact.id),
        "channel_id": int(contact.channel_id),
        "external_id": contact.contact_external_id,
        "name": contact.display_name or contact.name or contact.alias or contact.contact_external_id,
        "alias": contact.alias,
        "contact_type": contact.contact_type,
        "corp_name": contact.corp_name,
        "position": contact.position,
        "avatar_url": contact.avatar_url,
        "status": contact.member_status or contact.sync_status,
        "last_seen_at": contact.last_seen_at.isoformat() if contact.last_seen_at else None,
    }


def _group_view(group: Chatroom) -> dict[str, Any]:
    """群响应使用外部群标识，不让调用方依赖内部 ORM 字段结构。"""

    return {
        "id": int(group.id),
        "channel_id": int(group.channel_id),
        "external_id": group.external_id,
        "name": group.name or group.remark or group.external_id,
        "remark": group.remark,
        "status": group.status,
        "is_active": bool(group.is_active),
        "member_count": int(group.member_count or 0),
        "last_synced_at": group.last_synced_at.isoformat() if group.last_synced_at else None,
    }


def _member_view(member: ChatroomMember) -> dict[str, Any]:
    """群成员响应只使用统一外部标识和显示资料。"""

    return {
        "id": int(member.id),
        "external_id": member.member_external_id,
        "nickname": member.nickname,
        "display_name": member.display_name or member.nickname or member.member_external_id,
        "role": member.role,
        "status": member.status,
        "last_active_at": member.last_active_at.isoformat() if member.last_active_at else None,
    }


def _authenticate_and_open(request: Request, raw_body: bytes):
    """完成公共鉴权并返回开放应用身份，保持资源路由处理逻辑一致。"""

    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    return credential


@router.get("/api/open/accounts")
async def open_accounts_api(request: Request):
    """列出当前开放应用已授权的业务账号和能力。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(db, app=app, request_id=request.headers.get("X-Request-ID"))
        channels = db.scalars(
            select(Channel)
            .where(Channel.id.in_(_authorized_channel_ids(db, app)), Channel.is_enabled.is_(True))
            .order_by(Channel.id.asc())
        ).all()
        result = {"ok": True, "items": [_channel_view(channel) for channel in channels]}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/accounts/{account_id}")
async def open_account_detail_api(account_id: int, request: Request):
    """读取单个授权账号的业务状态。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="account.read",
            channel_id=account_id,
        )
        channel = db.scalar(select(Channel).where(Channel.id == int(account_id), Channel.is_enabled.is_(True)))
        if channel is None:
            raise HTTPException(status_code=404, detail="account_not_found")
        result = {"ok": True, "account": _channel_view(channel)}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/accounts/{account_id}/capabilities")
async def open_account_capabilities_api(account_id: int, request: Request):
    """按账号返回可用能力，供业务系统动态展示功能。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="account.read",
            channel_id=account_id,
        )
        channel = db.scalar(select(Channel).where(Channel.id == int(account_id), Channel.is_enabled.is_(True)))
        if channel is None:
            raise HTTPException(status_code=404, detail="account_not_found")
        result = {"ok": True, "account_id": int(account_id), "items": _channel_view(channel)["capabilities"]}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/contacts")
async def open_contacts_api(request: Request, channel_id: int | None = None, cursor: str = "", limit: int = 50, q: str = ""):
    """按授权渠道读取联系人快照，并使用主键游标分页。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="contact.read",
            channel_id=channel_id,
        )
        channel_ids = [int(channel_id)] if channel_id else _authorized_channel_ids(db, app)
        statement = select(WorkWechatContact).where(
            WorkWechatContact.channel_id.in_(channel_ids),
            WorkWechatContact.id > _cursor(cursor),
        )
        keyword = str(q or "").strip().lower()
        if keyword:
            pattern = f"%{keyword}%"
            statement = statement.where(
                WorkWechatContact.contact_external_id.ilike(pattern)
                | WorkWechatContact.name.ilike(pattern)
                | WorkWechatContact.display_name.ilike(pattern)
                | WorkWechatContact.alias.ilike(pattern)
            )
        items = db.scalars(statement.order_by(WorkWechatContact.id.asc()).limit(_page_limit(limit))).all()
        result = {
            "ok": True,
            "items": [_contact_view(item) for item in items],
            "next_cursor": str(items[-1].id) if items else None,
        }
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/contacts/{contact_external_id}")
async def open_contact_detail_api(contact_external_id: str, request: Request, channel_id: int | None = None):
    """读取单个联系人的业务详情。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="contact.read",
            channel_id=channel_id,
        )
        statement = select(WorkWechatContact).where(
            WorkWechatContact.contact_external_id == str(contact_external_id).strip(),
            WorkWechatContact.channel_id.in_([int(channel_id)] if channel_id else _authorized_channel_ids(db, app)),
        )
        contact = db.scalar(statement)
        if contact is None:
            raise HTTPException(status_code=404, detail="contact_not_found")
        result = {"ok": True, "contact": _contact_view(contact)}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/groups")
async def open_groups_api(request: Request, channel_id: int | None = None, cursor: str = "", limit: int = 50, q: str = ""):
    """按授权渠道读取群快照。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="group.read",
            channel_id=channel_id,
        )
        channel_ids = [int(channel_id)] if channel_id else _authorized_channel_ids(db, app)
        statement = select(Chatroom).where(Chatroom.channel_id.in_(channel_ids), Chatroom.id > _cursor(cursor))
        if str(q or "").strip():
            pattern = f"%{str(q).strip()}%"
            statement = statement.where(Chatroom.external_id.ilike(pattern) | Chatroom.name.ilike(pattern))
        items = db.scalars(statement.order_by(Chatroom.id.asc()).limit(_page_limit(limit))).all()
        result = {"ok": True, "items": [_group_view(item) for item in items], "next_cursor": str(items[-1].id) if items else None}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/groups/{external_id}/members")
async def open_group_members_api(external_id: str, request: Request, channel_id: int, cursor: str = "", limit: int = 100):
    """读取授权群成员快照。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="group.members.read",
            channel_id=channel_id,
        )
        group = db.scalar(select(Chatroom).where(Chatroom.channel_id == int(channel_id), Chatroom.external_id == external_id))
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")
        members = db.scalars(
            select(ChatroomMember)
            .where(ChatroomMember.chatroom_id == group.id, ChatroomMember.id > _cursor(cursor))
            .order_by(ChatroomMember.id.asc())
            .limit(_page_limit(limit, default=100))
        ).all()
        result = {"ok": True, "group": _group_view(group), "items": [_member_view(item) for item in members], "next_cursor": str(members[-1].id) if members else None}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/groups/{external_id}/qrcode")
async def open_group_qrcode_api(external_id: str, request: Request, channel_id: int):
    """返回已落库的临时群二维码引用，未缓存时明确返回资源暂不可用。"""

    credential = _authenticate_and_open(request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            capability="group.qrcode.read",
            channel_id=channel_id,
        )
        group = db.scalar(select(Chatroom).where(Chatroom.channel_id == int(channel_id), Chatroom.external_id == external_id))
        if group is None:
            raise HTTPException(status_code=404, detail="group_not_found")
        raw = str(group.extra_json or "").strip()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        qrcode = payload.get("qrcode") if isinstance(payload, dict) else None
        if not qrcode:
            raise HTTPException(status_code=503, detail="group_qrcode_unavailable")
        result = {"ok": True, "group_external_id": group.external_id, "qrcode": qrcode}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result

