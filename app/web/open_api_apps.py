"""租户自助开放应用管理页面。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.db import TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, OpenApiApp
from app.services.channel_catalog import list_supported_channel_types
from app.services.open_api_apps import (
    OpenApiAppError,
    OpenApiAppInput,
    disable_open_api_app,
    generate_open_api_secret,
    list_authorized_channel_ids,
    list_open_api_apps,
    require_active_open_api_subscription,
    rotate_open_api_secret,
    upsert_open_api_app,
)
from app.web.auth_common import require_tenant_csrf
from app.web.tenant_admin import _require_tenant_admin, _tenant_context


router = APIRouter(prefix="/tenant/open-apps", tags=["tenant-open-api-apps"])
templates = Jinja2Templates(directory="app/templates")


def _redirect(notice: str, *, danger: bool = False) -> RedirectResponse:
    """把操作结果转换为不携带密钥的短提示。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url="/tenant/open-apps" + suffix, status_code=303)


def _open_api_error_message(action: str, error: OpenApiAppError) -> str:
    """把订阅和额度错误转换为租户可以直接处理的页面提示。"""

    if error.code == "subscription_required":
        return f"开放应用{action}失败：订阅已失效，请先购买或续费套餐"
    if error.code == "quota_exceeded":
        return f"开放应用{action}失败：当前套餐额度不足，请升级套餐或减少授权范围"
    return f"开放应用{action}失败：{error.code}"


def _channels(db):
    """只列出正式目录中的启用渠道，避免授权表接收废弃类型。"""

    return db.scalars(
        select(Channel)
        .where(Channel.is_enabled.is_(True), Channel.deleted_at.is_(None), Channel.channel_type.in_(list_supported_channel_types()))
        .order_by(Channel.id.asc())
    ).all()


def _app_view(db, app: OpenApiApp) -> dict[str, object]:
    """构造页面展示对象，绝不把可解密密钥或哈希原文回显。"""

    return {
        "app": app,
        "authorized_channel_ids": set(list_authorized_channel_ids(db, app.id)),
        "secret_configured": bool(app.app_secret_encrypted),
        "webhook_configured": bool(app.webhook_url),
    }


def _page_context(request: Request, db, *, secret: str = "") -> dict[str, object]:
    """构造开放应用页面上下文，并把新密钥限制在当前响应。"""

    apps = list_open_api_apps(db)
    return _tenant_context(
        request,
        page_title="开放应用",
        notice=request.query_params.get("notice", ""),
        notice_tone=request.query_params.get("tone", ""),
        open_api_apps=[_app_view(db, app) for app in apps],
        channels=_channels(db),
        generated_secret=secret,
    )


@router.get("", response_class=HTMLResponse)
async def open_api_apps_page(request: Request):
    """企业管理员查看开放应用和授权渠道。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        return templates.TemplateResponse(request, "open_api_apps.html", _page_context(request, db))
    finally:
        db.close()


@router.post("")
async def open_api_app_create_action(
    request: Request,
    name: str = Form(""),
    app_key: str = Form(""),
    app_secret: str = Form(""),
    default_channel_id: int = Form(0),
    authorized_channel_ids: list[int] = Form([]),
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    csrf_token: str = Form(""),
):
    """创建开放应用，密钥只在创建成功响应中展示一次。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            require_active_open_api_subscription(db)
            generated_secret = app_secret.strip() or generate_open_api_secret()
            upsert_open_api_app(
                db,
                0,
                OpenApiAppInput(
                    name=name,
                    app_key=app_key,
                    app_secret=generated_secret,
                    default_channel_id=default_channel_id,
                    authorized_channel_ids=tuple(authorized_channel_ids or ()),
                    webhook_url=webhook_url,
                    webhook_secret=webhook_secret,
                ),
            )
        except OpenApiAppError as exc:
            return _redirect(_open_api_error_message("创建", exc), danger=True)
        except (ValueError, TypeError) as exc:
            return _redirect(f"开放应用创建失败：{exc}", danger=True)
        return templates.TemplateResponse(
            request,
            "open_api_apps.html",
            _tenant_context(
                request,
                page_title="开放应用",
                notice="开放应用已创建，请立即保存本次密钥。离开本页后无法再次查看。",
                open_api_apps=[_app_view(db, app) for app in list_open_api_apps(db)],
                channels=_channels(db),
                generated_secret=generated_secret,
            ),
            status_code=201,
        )
    finally:
        db.close()


@router.post("/{app_id}")
async def open_api_app_update_action(
    app_id: int,
    request: Request,
    name: str = Form(""),
    app_key: str = Form(""),
    default_channel_id: int = Form(0),
    authorized_channel_ids: list[int] = Form([]),
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    csrf_token: str = Form(""),
):
    """更新开放应用资料和授权范围，留空密钥字段时保持原密钥。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            require_active_open_api_subscription(db)
            upsert_open_api_app(
                db,
                app_id,
                OpenApiAppInput(
                    name=name,
                    app_key=app_key,
                    app_secret="",
                    default_channel_id=default_channel_id,
                    authorized_channel_ids=tuple(authorized_channel_ids or ()),
                    webhook_url=webhook_url,
                    webhook_secret=webhook_secret,
                ),
            )
        except OpenApiAppError as exc:
            return _redirect(_open_api_error_message("更新", exc), danger=True)
        except (ValueError, TypeError) as exc:
            return _redirect(f"开放应用更新失败：{exc}", danger=True)
        return _redirect("开放应用已更新")
    finally:
        db.close()


@router.post("/{app_id}/rotate-secret")
async def open_api_app_rotate_secret_action(
    app_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """轮换开放应用密钥，旧密钥立即失效。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            require_active_open_api_subscription(db)
            _app, secret = rotate_open_api_secret(db, app_id=app_id)
        except OpenApiAppError as exc:
            if exc.code == "app_not_found":
                raise HTTPException(status_code=404, detail="Open API app not found") from exc
            return _redirect(_open_api_error_message("密钥轮换", exc), danger=True)
        context = _page_context(request, db, secret=secret)
        context["notice"] = "密钥已轮换，请立即保存本次密钥。旧密钥已经失效。"
        return templates.TemplateResponse(request, "open_api_apps.html", context, status_code=201)
    finally:
        db.close()


@router.post("/{app_id}/disable")
async def open_api_app_disable_action(
    app_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """停用开放应用并立即阻断新的 API 请求。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            disable_open_api_app(db, app_id=app_id)
        except OpenApiAppError as exc:
            if exc.code == "app_not_found":
                raise HTTPException(status_code=404, detail="Open API app not found") from exc
            raise
    finally:
        db.close()
    return _redirect("开放应用已停用")

