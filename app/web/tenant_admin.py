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
from app.models.entities import AiDecision
from app.core.tenant_context import require_tenant_id
from app.services import ai_chat, ai_dispatch, ai_memory
from app.services.tenant_admin import require_tenant_mutation_allowed
from app.services.local_proxy_connector import (
    LocalProxyConnectorError,
    create_proxy_connector,
    disable_proxy_connector,
    issue_activation_code,
    list_proxy_connectors,
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


@router.get("/connectors", response_class=HTMLResponse)
async def tenant_connectors_page(request: Request):
    """租户管理员管理 Windows 本地代理连接器和一次性激活码。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        context = _tenant_context(
            request,
            page_title="本地代理连接器",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            connectors=list_proxy_connectors(db),
            activation_code=request.query_params.get("activation_code", ""),
        )
        return templates.TemplateResponse(request, "tenant_connectors.html", context)
    finally:
        db.close()


@router.post("/connectors")
async def tenant_connector_create_action(
    request: Request,
    name: str = Form(""),
    csrf_token: str = Form(""),
):
    """创建连接器并在当前响应中一次性展示激活码。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            connector = create_proxy_connector(db, name=name)
            code = issue_activation_code(db, connector_id=connector.id)
        except LocalProxyConnectorError as exc:
            return _redirect("/tenant/connectors", f"连接器创建失败：{exc}", danger=True)
        return templates.TemplateResponse(
            request,
            "tenant_connectors.html",
            _tenant_context(
                request,
                page_title="本地代理连接器",
                notice="连接器已创建，请复制本次激活码。离开本页后无法再次查看。",
                connectors=list_proxy_connectors(db),
                activation_code=code,
            ),
            status_code=201,
        )
    finally:
        db.close()


@router.post("/connectors/{connector_id}/disable")
async def tenant_connector_disable_action(
    connector_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """停用连接器并吊销设备密钥，断开后的登录会话由统一回收逻辑结束。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            disable_proxy_connector(db, connector_id=connector_id)
        except LocalProxyConnectorError as exc:
            return _redirect("/tenant/connectors", f"连接器停用失败：{exc}", danger=True)
    finally:
        db.close()
    return _redirect("/tenant/connectors", "连接器已停用")


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


def _ai_page_response(request: Request, db, *, notice: str = "", tone: str = ""):
    """渲染 AI 设置页：配置、今日用量和最近的决策记录放在同一页。"""

    setting = ai_dispatch.get_ai_setting(db)
    decisions = db.scalars(select(AiDecision).order_by(AiDecision.id.desc()).limit(30)).all()
    # 记忆库个数来自记忆服务;服务没配置或连不上时为 None,页面据此提示。
    banks = ai_memory.list_banks(require_tenant_id())
    memory_count = None if banks is None else len(banks)
    context = _tenant_context(
        request,
        page_title="AI 助手",
        notice=notice,
        notice_tone=tone,
        setting=setting,
        key_configured=bool(setting and setting.api_key_encrypted),
        ready=ai_dispatch.is_ai_ready(setting),
        used_today=ai_dispatch.used_today(db),
        decisions=decisions,
        memory_count=memory_count,
        default_persona=ai_chat.DEFAULT_PERSONA,
    )
    return templates.TemplateResponse(request, "tenant_ai.html", context)


@router.get("/ai", response_class=HTMLResponse)
async def tenant_ai_page(request: Request):
    """所有角色可查看 AI 设置状态与决策记录，密钥永不回显。"""

    _require_principal(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        return _ai_page_response(
            request, db, notice=request.query_params.get("notice", ""), tone=request.query_params.get("tone", "")
        )
    finally:
        db.close()


@router.post("/ai")
async def tenant_ai_save_action(
    request: Request,
    enabled: str = Form(""),
    api_base: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    chat_enabled: str = Form(""),
    persona: str = Form(""),
    action: str = Form("save"),
    csrf_token: str = Form(""),
):
    """保存 AI 设置；action=test 时额外做一次连接测试，并把中文结果带回页面。"""

    principal = _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        require_tenant_mutation_allowed(principal)
        if action == "clear_memory":
            removed = ai_memory.clear_tenant_memories(require_tenant_id())
            if removed is None:
                return _redirect("/tenant/ai", "记忆服务未配置或暂时连不上，没有清空", danger=True)
            return _redirect("/tenant/ai", f"已清空 {removed} 个记忆库")
        setting = ai_dispatch.save_ai_setting(
            db,
            enabled=enabled == "on",
            api_base=api_base,
            model=model,
            api_key=api_key,
            chat_enabled=chat_enabled == "on",
            persona=persona,
        )
        db.commit()
        if action == "test":
            ok, message = ai_dispatch.test_connection(ai_dispatch.snapshot_setting(setting))
            return _redirect("/tenant/ai", message, danger=not ok)
        return _redirect("/tenant/ai", "AI 设置已保存")
    finally:
        db.close()
