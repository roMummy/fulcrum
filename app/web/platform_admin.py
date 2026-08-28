"""平台员工、租户、套餐、订阅和初始管理员管理页面。"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.core.time import SHANGHAI_TZ, format_shanghai_time
from app.services.platform_admin import (
    PlatformAdminError,
    create_tenant_with_subscription,
    current_subscription,
    get_tenant_detail,
    list_plans,
    list_tenants,
    renew_tenant_subscription,
    reset_tenant_admin_password,
    save_plan,
    set_tenant_status,
)
from app.services.platform_analytics import build_platform_analytics
from app.services.platform_staff import (
    PlatformStaffError,
    create_platform_user,
    list_platform_users,
    reset_platform_user_password,
    update_platform_user,
)
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_LOGOUT_PATH,
    PLATFORM_PASSWORD_PATH,
    require_platform_csrf,
)
from app.web.platform_navigation import build_platform_primary_navigation


router = APIRouter(prefix="/platform", tags=["platform-admin"])
templates = Jinja2Templates(directory="app/templates")


def _require_platform_principal(request: Request):
    """只接受中间件从平台会话恢复的身份，不读取租户身份或表单角色。"""

    principal = getattr(request.state, "platform_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Platform authentication required")
    return principal


def _platform_context(
    request: Request,
    *,
    page_title: str,
    active: str,
    notice: str = "",
    notice_tone: str = "",
    **extra,
) -> dict[str, object]:
    """构造与租户工作台隔离的平台导航和身份操作上下文。"""

    principal = _require_platform_principal(request)
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": principal,
        "primary_nav_items": build_platform_primary_navigation(active),
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": [],
        "module_links": {},
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if notice and "成功" not in notice else "info"),
        "csrf_token": request.cookies.get(PLATFORM_CSRF_COOKIE, ""),
        "password_action": PLATFORM_PASSWORD_PATH,
        "logout_action": PLATFORM_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "current_subscription": current_subscription,
    }
    context.update(extra)
    return context


def _parse_expiry(value: str) -> datetime | None:
    """把日期表单解析为上海时区当天结束，空值表示长期有效。"""

    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise PlatformAdminError("subscription_expiry_invalid") from exc
    return datetime.combine(parsed, time.max, tzinfo=SHANGHAI_TZ)


def _yuan_to_cents(value: str, *, field: str) -> int:
    """用十进制定点数把页面元金额转换成分，拒绝隐藏舍入和浮点误差。"""

    normalized = (value or "0").strip()
    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise PlatformAdminError(f"{field}_invalid") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise PlatformAdminError(f"{field}_invalid")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise PlatformAdminError(f"{field}_invalid")
    return int(cents)


def _format_yuan(cents: int) -> str:
    """把数据库整数分格式化为两位小数，避免模板执行浮点运算。"""

    return f"{Decimal(int(cents)) / Decimal(100):.2f}"


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    """统一生成不包含任何凭证原文的平台页面跳转。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url=path + suffix, status_code=303)


def _platform_staff_error_message(error: PlatformStaffError) -> str:
    """把平台员工内部错误码转换为可操作且不泄露敏感信息的页面提示。"""

    messages = {
        "platform_username_invalid": "请输入有效的平台用户名",
        "platform_username_exists": "该平台用户名已存在",
        "platform_display_name_invalid": "请输入有效的员工姓名",
        "platform_user_not_found": "平台员工不存在",
        "last_platform_admin_required": "必须保留至少一个启用的平台管理员",
    }
    return messages.get(str(error), "平台员工信息保存失败")


@router.get("/dashboard", response_class=HTMLResponse)
async def platform_dashboard_page(request: Request):
    """展示平台跨租户使用、经营、渠道健康和待办数据。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="平台概览",
            active="dashboard",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            # 时间范围只交给聚合服务规范化；路由不接受客户端 tenant_id，也不维护旧四指标双轨数据。
            analytics=build_platform_analytics(db, range_key=request.query_params.get("range", "7d")),
        )
        return templates.TemplateResponse(request, "platform_dashboard.html", context)


@router.get("/users", response_class=HTMLResponse)
async def platform_users_page(request: Request):
    """展示不属于任何租户的平台员工账号。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="平台员工",
            active="users",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            users=list_platform_users(db),
            initial_password="",
        )
        return templates.TemplateResponse(request, "platform_users.html", context)


@router.post("/users", response_class=HTMLResponse)
async def platform_user_create_action(
    request: Request,
    username: str = Form(""),
    display_name: str = Form(""),
    csrf_token: str = Form(""),
):
    """创建全权限平台员工，并只在当前成功响应展示初始密码。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            created = create_platform_user(
                db,
                username=username,
                display_name=display_name,
                actor_user_id=principal.user_id,
            )
        except PlatformStaffError as exc:
            return _redirect("/platform/users", _platform_staff_error_message(exc), danger=True)
        context = _platform_context(
            request,
            page_title="平台员工",
            active="users",
            notice="平台员工创建成功。请立即安全交付初始密码，离开本页后无法再次查看。",
            users=list_platform_users(db),
            initial_password=created.initial_password,
        )
        return templates.TemplateResponse(request, "platform_users.html", context, status_code=201)


@router.post("/users/{user_id}")
async def platform_user_update_action(
    user_id: int,
    request: Request,
    display_name: str = Form(""),
    is_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    """更新平台员工姓名与启停状态，角色固定为全权限管理员。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            update_platform_user(
                db,
                user_id=user_id,
                display_name=display_name,
                is_enabled=is_enabled,
                actor_user_id=principal.user_id,
            )
        except PlatformStaffError as exc:
            return _redirect("/platform/users", _platform_staff_error_message(exc), danger=True)
    return _redirect("/platform/users", "平台员工信息已更新")


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
async def platform_user_reset_password_action(
    user_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """重置平台员工密码，并只在当前成功响应展示一次。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            initial_password = reset_platform_user_password(
                db,
                user_id=user_id,
                actor_user_id=principal.user_id,
            )
        except PlatformStaffError as exc:
            return _redirect("/platform/users", _platform_staff_error_message(exc), danger=True)
        context = _platform_context(
            request,
            page_title="平台员工",
            active="users",
            notice="平台员工密码已重置。请立即安全交付，离开本页后无法再次查看。",
            users=list_platform_users(db),
            initial_password=initial_password,
        )
        return templates.TemplateResponse(request, "platform_users.html", context)


@router.get("/tenants", response_class=HTMLResponse)
async def platform_tenants_page(request: Request):
    """展示租户清单和开通表单。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="租户管理",
            active="tenants",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            tenants=list_tenants(db),
            plans=[plan for plan in list_plans(db) if plan.status == "active"],
        )
        return templates.TemplateResponse(request, "platform_tenants.html", context)


@router.post("/tenants", response_class=HTMLResponse)
async def platform_tenant_create_action(
    request: Request,
    name: str = Form(""),
    plan_id: int = Form(0),
    expires_on: str = Form(""),
    admin_mobile: str = Form(""),
    csrf_token: str = Form(""),
):
    """开通租户并在当前响应页一次性展示初始密码。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            created = create_tenant_with_subscription(
                db,
                name=name,
                plan_id=plan_id,
                expires_at=_parse_expiry(expires_on),
                admin_mobile=admin_mobile,
                actor_user_id=principal.user_id,
            )
        except PlatformAdminError as exc:
            return _redirect("/platform/tenants", f"租户创建失败：{exc}", danger=True)
        tenant = get_tenant_detail(db, created.tenant_id)
        context = _platform_context(
            request,
            page_title=f"租户详情：{tenant.name}",
            active="tenants",
            notice="租户创建成功。请立即安全交付初始密码，离开本页后无法再次查看。",
            tenant=tenant,
            subscription=current_subscription(tenant),
            plans=list_plans(db),
            initial_password=created.initial_password,
            reset_user_id=created.admin_user_id,
        )
        return templates.TemplateResponse(request, "platform_tenant_detail.html", context, status_code=201)


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
async def platform_tenant_detail_page(tenant_id: int, request: Request):
    """展示租户订阅、员工和维护动作。"""

    with SessionLocal() as db:
        tenant = get_tenant_detail(db, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        context = _platform_context(
            request,
            page_title=f"租户详情：{tenant.name}",
            active="tenants",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            tenant=tenant,
            subscription=current_subscription(tenant),
            plans=list_plans(db),
            initial_password="",
            reset_user_id=0,
        )
        return templates.TemplateResponse(request, "platform_tenant_detail.html", context)


@router.post("/tenants/{tenant_id}/status")
async def platform_tenant_status_action(
    tenant_id: int,
    request: Request,
    status: str = Form(""),
    csrf_token: str = Form(""),
):
    """启用或停用租户。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            set_tenant_status(db, tenant_id=tenant_id, status=status, actor_user_id=principal.user_id)
        except PlatformAdminError as exc:
            return _redirect(f"/platform/tenants/{tenant_id}", f"状态更新失败：{exc}", danger=True)
    return _redirect(f"/platform/tenants/{tenant_id}", "租户状态已更新")


@router.post("/tenants/{tenant_id}/renew")
async def platform_tenant_renew_action(
    tenant_id: int,
    request: Request,
    plan_id: int = Form(0),
    expires_on: str = Form(""),
    csrf_token: str = Form(""),
):
    """为租户切换套餐或延长有效期。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            renew_tenant_subscription(
                db,
                tenant_id=tenant_id,
                plan_id=plan_id,
                expires_at=_parse_expiry(expires_on),
                actor_user_id=principal.user_id,
            )
        except PlatformAdminError as exc:
            return _redirect(f"/platform/tenants/{tenant_id}", f"续期失败：{exc}", danger=True)
    return _redirect(f"/platform/tenants/{tenant_id}", "租户套餐和有效期已更新")


@router.post("/tenants/{tenant_id}/admins/{user_id}/reset-password", response_class=HTMLResponse)
async def platform_tenant_admin_reset_action(
    tenant_id: int,
    user_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """重置租户管理员密码，并只在本次响应展示原文。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            initial_password = reset_tenant_admin_password(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                actor_user_id=principal.user_id,
            )
        except PlatformAdminError as exc:
            return _redirect(f"/platform/tenants/{tenant_id}", f"密码重置失败：{exc}", danger=True)
        tenant = get_tenant_detail(db, tenant_id)
        context = _platform_context(
            request,
            page_title=f"租户详情：{tenant.name}",
            active="tenants",
            notice="管理员密码已重置。请立即安全交付，离开本页后无法再次查看。",
            tenant=tenant,
            subscription=current_subscription(tenant),
            plans=list_plans(db),
            initial_password=initial_password,
            reset_user_id=user_id,
        )
        return templates.TemplateResponse(request, "platform_tenant_detail.html", context)


@router.get("/plans", response_class=HTMLResponse)
async def platform_plans_page(request: Request):
    """展示套餐清单和创建编辑表单。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="套餐管理",
            active="plans",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            plans=list_plans(db),
            format_yuan=_format_yuan,
        )
        return templates.TemplateResponse(request, "platform_plans.html", context)


@router.post("/plans")
async def platform_plan_save_action(
    request: Request,
    plan_id: int = Form(0),
    code: str = Form(""),
    name: str = Form(""),
    provider_node_limit: int = Form(1),
    bot_limit: int = Form(1),
    monthly_price: str = Form("0"),
    yearly_price: str = Form("0"),
    trial_days: int = Form(0),
    is_public: bool = Form(False),
    is_featured: bool = Form(False),
    display_order: int = Form(0),
    status: str = Form("active"),
    description: str = Form(""),
    csrf_token: str = Form(""),
):
    """创建或更新套餐。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            save_plan(
                db,
                plan_id=plan_id,
                code=code,
                name=name,
                provider_node_limit=provider_node_limit,
                bot_limit=bot_limit,
                monthly_price_cents=_yuan_to_cents(monthly_price, field="monthly_price"),
                yearly_price_cents=_yuan_to_cents(yearly_price, field="yearly_price"),
                trial_days=trial_days,
                is_public=is_public,
                is_featured=is_featured,
                display_order=display_order,
                status=status,
                description=description,
                actor_user_id=principal.user_id,
            )
        except PlatformAdminError as exc:
            return _redirect("/platform/plans", f"套餐保存失败：{exc}", danger=True)
    return _redirect("/platform/plans", "套餐已保存")
