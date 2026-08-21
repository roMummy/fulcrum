"""租户员工和订阅信息管理页面。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.db import TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.core.time import format_shanghai_time
from app.models.saas import Tenant
from app.services.saas_auth import has_tenant_role
from app.services.tenant_admin import (
    TenantAdminError,
    create_tenant_user,
    list_tenant_users,
    reset_tenant_user_password,
    update_tenant_user,
)
from app.web.auth_common import (
    TENANT_CSRF_COOKIE,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    require_tenant_csrf,
)
from app.web.tenant_navigation import build_tenant_navigation


router = APIRouter(prefix="/tenant", tags=["tenant-admin"])
templates = Jinja2Templates(directory="app/templates")


def _require_principal(request: Request):
    """只使用中间件恢复的当前租户员工身份。"""

    principal = getattr(request.state, "current_user", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def _require_tenant_admin(request: Request):
    """员工变更只允许固定角色 tenant_admin。"""

    principal = _require_principal(request)
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal


def _tenant_context(
    request: Request,
    *,
    page_title: str,
    notice: str = "",
    notice_tone: str = "",
    **extra,
) -> dict[str, object]:
    """构造租户治理页面上下文，并保持与业务工作台一致的身份操作。"""

    principal = _require_principal(request)
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": principal,
        "module_links": {},
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if notice and "成功" not in notice else "info"),
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH,
        "logout_action": TENANT_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "can_manage_users": has_tenant_role(principal, "tenant_admin"),
    }
    context.update(build_tenant_navigation(request.url.path))
    context.update(extra)
    return context


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    """跳转提示中不携带初始密码或其他敏感凭证。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url=path + suffix, status_code=303)


def _tenant_admin_error_message(error: TenantAdminError) -> str:
    """把内部错误码转换为稳定中文提示，手机号冲突不泄露其他企业信息。"""

    messages = {
        "mobile_invalid": "请输入有效的手机号",
        "mobile_already_registered": "该手机号已被其他企业账号使用",
        "username_required": "请输入员工账号标识",
        "username_exists": "当前企业已存在相同账号标识",
        "display_name_required": "请输入员工姓名",
        "tenant_role_invalid": "请选择有效的员工角色",
        "last_tenant_admin_required": "必须保留至少一个启用的企业管理员",
        "tenant_user_not_found": "员工账号不存在",
    }
    return messages.get(str(error), "员工信息保存失败")


@router.get("/users", response_class=HTMLResponse)
async def tenant_users_page(request: Request):
    """租户管理员查看员工并执行管理，其他角色只能查看清单。"""

    db = open_tenant_session(TenantSessionLocal)
    try:
        context = _tenant_context(
            request,
            page_title="员工管理",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            users=list_tenant_users(db),
            initial_password="",
        )
        return templates.TemplateResponse(request, "tenant_users.html", context)
    finally:
        db.close()


@router.post("/users", response_class=HTMLResponse)
async def tenant_user_create_action(
    request: Request,
    username: str = Form(""),
    mobile: str = Form(""),
    display_name: str = Form(""),
    role: str = Form("readonly"),
    csrf_token: str = Form(""),
):
    """创建员工并在当前成功响应中一次性展示初始密码。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            created = create_tenant_user(
                db,
                username=username,
                mobile=mobile,
                display_name=display_name,
                role=role,
            )
        except TenantAdminError as exc:
            return _redirect("/tenant/users", _tenant_admin_error_message(exc), danger=True)
        context = _tenant_context(
            request,
            page_title="员工管理",
            notice="员工创建成功。请立即安全交付初始密码，离开本页后无法再次查看。",
            users=list_tenant_users(db),
            initial_password=created.initial_password,
        )
        return templates.TemplateResponse(request, "tenant_users.html", context, status_code=201)
    finally:
        db.close()


@router.post("/users/{user_id}")
async def tenant_user_update_action(
    user_id: int,
    request: Request,
    mobile: str = Form(""),
    display_name: str = Form(""),
    role: str = Form("readonly"),
    is_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    """更新员工显示名、角色和启停状态。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            update_tenant_user(
                db,
                user_id=user_id,
                mobile=mobile,
                display_name=display_name,
                role=role,
                is_enabled=is_enabled,
            )
        except TenantAdminError as exc:
            return _redirect("/tenant/users", _tenant_admin_error_message(exc), danger=True)
    finally:
        db.close()
    return _redirect("/tenant/users", "员工信息已更新")


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
async def tenant_user_reset_password_action(
    user_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """重置员工密码，并只在当前响应展示一次。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            initial_password = reset_tenant_user_password(db, user_id=user_id)
        except TenantAdminError as exc:
            return _redirect("/tenant/users", f"密码重置失败：{exc}", danger=True)
        context = _tenant_context(
            request,
            page_title="员工管理",
            notice="员工密码已重置。请立即安全交付，离开本页后无法再次查看。",
            users=list_tenant_users(db),
            initial_password=initial_password,
        )
        return templates.TemplateResponse(request, "tenant_users.html", context)
    finally:
        db.close()


@router.get("/subscription", response_class=HTMLResponse)
async def tenant_subscription_page(request: Request):
    """所有租户角色都可以查看当前状态和订阅有效期。"""

    principal = _require_principal(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        tenant = db.scalar(select(Tenant).where(Tenant.id == principal.tenant_id))
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        context = _tenant_context(
            request,
            page_title="订阅信息",
            tenant=tenant,
        )
        return templates.TemplateResponse(request, "tenant_subscription.html", context)
    finally:
        db.close()
