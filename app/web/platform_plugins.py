"""平台后台的插件管理:查看全部插件、全平台停用/恢复、设置对哪些租户可见。

插件目录全平台共享,租户只能管理自己机器人上的绑定;这里是平台方的管控入口。
所有修改都要求平台会话和 CSRF 令牌,并写入平台审计日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import format_shanghai_time
from app.models.entities import Plugin
from app.models.saas import SaasPlan, Tenant
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.plugin_governance import (
    VISIBILITY_LABELS,
    PluginGovernanceError,
    PluginUsage,
    grants_of,
    set_platform_status,
    set_visibility,
    usage_summary,
)
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_LOGOUT_PATH,
    PLATFORM_PASSWORD_PATH,
    require_platform_csrf,
)
from app.web.platform_navigation import build_platform_primary_navigation
from app.web.platform_operations import _redirect, _require_platform_principal, templates

router = APIRouter(prefix="/platform", tags=["platform-plugins"])


def _plugins_context(request: Request, **extra) -> dict[str, object]:
    """插件管理页的页面上下文:顶级导航高亮"插件管理",身份与令牌都来自平台会话。"""

    notice = request.query_params.get("notice", "")
    context: dict[str, object] = {
        "request": request,
        "page_title": "插件管理",
        "current_user": _require_platform_principal(request),
        "primary_nav_items": build_platform_primary_navigation("plugins"),
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": [],
        "module_links": {},
        "notice": notice,
        "notice_tone": request.query_params.get("tone", "") or ("danger" if notice and "已" not in notice else "info"),
        "csrf_token": request.cookies.get(PLATFORM_CSRF_COOKIE, ""),
        "password_action": PLATFORM_PASSWORD_PATH,
        "logout_action": PLATFORM_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "visibility_labels": VISIBILITY_LABELS,
    }
    context.update(extra)
    return context


def _get_plugin(db, code: str) -> Plugin:
    plugin = db.scalar(select(Plugin).where(Plugin.code == code))
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return plugin


@router.get("/plugins", response_class=HTMLResponse)
async def platform_plugins_page(request: Request):
    """列出全部插件及其状态、可见范围和使用情况。"""

    _require_platform_principal(request)
    with SessionLocal() as db:
        sync_plugins_from_directory(db)
        plugins = db.scalars(select(Plugin).order_by(Plugin.display_name.asc(), Plugin.code.asc())).all()
        usage = usage_summary(db)
        plans = db.scalars(select(SaasPlan).order_by(SaasPlan.id.asc())).all()
        tenants = db.scalars(select(Tenant).order_by(Tenant.id.asc())).all()
        rows = []
        for plugin in plugins:
            plan_ids, tenant_ids = grants_of(db, int(plugin.id))
            rows.append(
                {
                    "plugin": plugin,
                    "usage": usage.get(int(plugin.id), PluginUsage()),
                    "plan_ids": set(plan_ids),
                    "tenant_ids": set(tenant_ids),
                }
            )
        context = _plugins_context(request, rows=rows, plans=plans, tenants=tenants)
        return templates.TemplateResponse(request, "platform_plugins.html", context)


@router.post("/plugins/{code}/status")
async def platform_plugin_status_action(
    code: str,
    request: Request,
    status: str = Form(...),
    note: str = Form(""),
    csrf_token: str = Form(""),
):
    """全平台停用或恢复一个插件。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        plugin = _get_plugin(db, code)
        try:
            set_platform_status(db, plugin, status=status, note=note, actor_user_id=principal.user_id)
        except PluginGovernanceError as exc:
            return _redirect("/platform/plugins", str(exc), danger=True)
        db.commit()
        label = plugin.display_name
    return _redirect("/platform/plugins", f"{label} 已{'恢复' if status == 'active' else '在全平台停用'}")


@router.post("/plugins/{code}/visibility")
async def platform_plugin_visibility_action(
    code: str,
    request: Request,
    visibility: str = Form(...),
    plan_ids: list[int] = Form(default_factory=list),
    tenant_ids: list[int] = Form(default_factory=list),
    csrf_token: str = Form(""),
):
    """设置插件对哪些租户可见(所有 / 指定套餐 / 指定租户 / 仅平台)。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        plugin = _get_plugin(db, code)
        try:
            set_visibility(
                db,
                plugin,
                visibility=visibility,
                plan_ids=plan_ids,
                tenant_ids=tenant_ids,
                actor_user_id=principal.user_id,
            )
        except PluginGovernanceError as exc:
            db.rollback()
            return _redirect("/platform/plugins", str(exc), danger=True)
        db.commit()
        label = plugin.display_name
    return _redirect("/platform/plugins", f"{label} 的可见范围已更新")
