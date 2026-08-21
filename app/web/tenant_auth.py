"""企业账号浏览器注销和密码维护路由。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.models.saas import TenantUser
from app.services.saas_auth import (
    create_tenant_session,
    hash_password,
    load_tenant_principal,
    revoke_tenant_session_by_token,
    revoke_tenant_sessions,
    verify_password,
)
from app.web.auth_common import (
    TENANT_CSRF_COOKIE,
    TENANT_HOME_PATH,
    TENANT_LOGIN_PATH,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    build_auth_page_context,
    delete_tenant_auth_cookies,
    request_ip_address,
    request_user_agent,
    require_tenant_csrf,
    set_tenant_auth_cookies,
    tenant_session_token,
    validate_new_password,
)


router = APIRouter(tags=["tenant-auth"])
templates = Jinja2Templates(directory="app/templates")


def _render_password_page(
    request: Request,
    principal,
    user: TenantUser,
    *,
    notice: str = "",
    status_code: int = 200,
):
    """为租户改密页提供身份、租户和 CSRF 上下文。"""

    tenant = user.tenant
    context = build_auth_page_context(
        request,
        page_title="账号安全",
        notice=notice,
        current_user=principal,
        current_user_detail=user,
        identity_kind="tenant",
        password_action=TENANT_PASSWORD_PATH,
        logout_action=TENANT_LOGOUT_PATH,
        csrf_token=request.cookies.get(TENANT_CSRF_COOKIE, ""),
        must_change_password=principal.must_change_password,
        tenant_code=tenant.code if tenant else "",
        tenant_name=tenant.name if tenant else "",
    )
    return templates.TemplateResponse(request, "password.html", context, status_code=status_code)


@router.post(TENANT_LOGOUT_PATH)
async def tenant_logout_action(request: Request, csrf_token: str = Form("")):
    """校验 CSRF 后只吊销当前浏览器租户会话，并删除对应 Cookie。"""

    raw_token = tenant_session_token(request)
    with SessionLocal() as db:
        principal = load_tenant_principal(db, raw_token, touch=False)
        # 已经过期或被吊销的会话没有可继续执行的权限，注销保持幂等并只清理浏览器状态。
        if principal is not None:
            require_tenant_csrf(db, request, csrf_token)
            revoke_tenant_session_by_token(db, raw_token, reason="logout")

    response = RedirectResponse(url=f"{TENANT_LOGIN_PATH}?notice=" + quote("已退出登录"), status_code=303)
    delete_tenant_auth_cookies(response)
    return response


@router.get(TENANT_PASSWORD_PATH, response_class=HTMLResponse)
async def tenant_password_page(request: Request):
    """展示当前租户员工的密码维护页。"""

    raw_token = tenant_session_token(request)
    with SessionLocal() as db:
        principal = load_tenant_principal(db, raw_token)
        if principal is None:
            return RedirectResponse(url=TENANT_LOGIN_PATH, status_code=303)
        user = db.get(TenantUser, principal.user_id)
        if user is None:
            return RedirectResponse(url=TENANT_LOGIN_PATH, status_code=303)
        return _render_password_page(
            request,
            principal,
            user,
            notice=request.query_params.get("notice", ""),
        )


@router.post(TENANT_PASSWORD_PATH)
async def tenant_password_action(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    """更新租户员工密码、吊销全部旧会话并签发当前浏览器的新会话。"""

    raw_token = tenant_session_token(request)
    with SessionLocal() as db:
        principal = load_tenant_principal(db, raw_token, touch=False)
        if principal is None:
            return RedirectResponse(url=TENANT_LOGIN_PATH, status_code=303)
        require_tenant_csrf(db, request, csrf_token)
        user = db.get(TenantUser, principal.user_id)
        if user is None or not user.is_enabled:
            return RedirectResponse(url=TENANT_LOGIN_PATH, status_code=303)

        if not verify_password(current_password, user.password_hash):
            return _render_password_page(request, principal, user, notice="当前密码不正确", status_code=400)
        password_error = validate_new_password(current_password, new_password, confirm_password)
        if password_error:
            return _render_password_page(request, principal, user, notice=password_error, status_code=400)

        # 密码变化属于高风险身份事件：全部旧会话都应立即吊销，再用递增后的认证版本
        # 为当前浏览器创建新会话，确保其他设备不能继续使用旧令牌。
        revoke_tenant_sessions(db, user.id, reason="password_changed")
        user.password_hash = hash_password(new_password)
        user.auth_version += 1
        user.must_change_password = False
        db.commit()
        db.refresh(user)
        created = create_tenant_session(
            db,
            user,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )

    response = RedirectResponse(url=TENANT_HOME_PATH + "?notice=" + quote("密码已更新"), status_code=303)
    set_tenant_auth_cookies(response, created)
    return response
