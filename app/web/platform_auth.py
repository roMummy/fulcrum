"""平台管理员浏览器登录、注销和密码维护路由。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.models.saas import PlatformUser
from app.services.image_captcha import CaptchaChallenge, consume_captcha_challenge, create_captcha_challenge
from app.services.saas_auth import (
    authenticate_platform_user,
    create_platform_session,
    hash_password,
    load_platform_principal,
    revoke_platform_session_by_token,
    revoke_platform_sessions,
    verify_password,
)
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_HOME_PATH,
    PLATFORM_LOGIN_CAPTCHA_PURPOSE,
    PLATFORM_LOGIN_ERROR,
    PLATFORM_LOGIN_PATH,
    PLATFORM_LOGOUT_PATH,
    PLATFORM_PASSWORD_PATH,
    build_auth_page_context,
    delete_platform_auth_cookies,
    record_auth_event,
    platform_session_token,
    request_ip_address,
    request_user_agent,
    require_platform_csrf,
    set_platform_auth_cookies,
    validate_new_password,
)


router = APIRouter(tags=["platform-auth"])
templates = Jinja2Templates(directory="app/templates")


def _render_login(
    request: Request,
    *,
    notice: str = "",
    status_code: int = 200,
    username: str = "",
    challenge: CaptchaChallenge | None = None,
):
    """渲染平台登录页，任何失败都只回填用户名，不回填密码。"""

    resolved_challenge = challenge or create_captcha_challenge(purpose=PLATFORM_LOGIN_CAPTCHA_PURPOSE)
    context = build_auth_page_context(
        request,
        page_title="平台登录",
        notice=notice,
        prefill_username=username,
        identity_kind="platform",
        captcha_token=resolved_challenge.token,
        captcha_image=resolved_challenge.image_data_uri,
        captcha_refresh_url="/auth/captcha?purpose=" + PLATFORM_LOGIN_CAPTCHA_PURPOSE,
    )
    return templates.TemplateResponse(request, "platform_login.html", context, status_code=status_code)


def _render_password_page(
    request: Request,
    principal,
    user: PlatformUser,
    *,
    notice: str = "",
    status_code: int = 200,
):
    """为平台管理员改密页提供独立路径和平台 CSRF 值。"""

    context = build_auth_page_context(
        request,
        page_title="平台账号安全",
        notice=notice,
        current_user=principal,
        current_user_detail=user,
        identity_kind="platform",
        password_action=PLATFORM_PASSWORD_PATH,
        logout_action=PLATFORM_LOGOUT_PATH,
        csrf_token=request.cookies.get(PLATFORM_CSRF_COOKIE, ""),
        must_change_password=principal.must_change_password,
        tenant_code="",
        tenant_name="",
    )
    return templates.TemplateResponse(request, "password.html", context, status_code=status_code)


@router.get(PLATFORM_LOGIN_PATH, response_class=HTMLResponse)
async def platform_login_page(request: Request):
    """展示平台登录页；有效平台会话直接进入平台路径。"""

    raw_token = platform_session_token(request)
    if raw_token:
        with SessionLocal() as db:
            principal = load_platform_principal(db, raw_token)
        if principal is not None:
            target = PLATFORM_PASSWORD_PATH if principal.must_change_password else PLATFORM_HOME_PATH
            return RedirectResponse(url=target, status_code=303)
    return _render_login(request, notice=request.query_params.get("notice", ""))


@router.post(PLATFORM_LOGIN_PATH)
async def platform_login_action(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    captcha_token: str = Form(""),
    captcha_answer: str = Form(""),
):
    """只查询平台账号表并创建平台数据库会话。"""

    normalized_username = username.strip()
    captcha_result = consume_captcha_challenge(
        captcha_token,
        captcha_answer,
        purpose=PLATFORM_LOGIN_CAPTCHA_PURPOSE,
    )
    if not captcha_result.valid:
        record_auth_event(
            request,
            event="login captcha rejected",
            auth_scope="platform",
            reason=captcha_result.reason,
            identity=normalized_username,
        )
        return _render_login(
            request,
            notice="图形验证码错误或已过期",
            status_code=400,
            username=normalized_username,
        )
    if not normalized_username or not password:
        record_auth_event(
            request,
            event="login credentials rejected",
            auth_scope="platform",
            reason="missing_credentials",
            identity=normalized_username,
        )
        return _render_login(
            request,
            notice=PLATFORM_LOGIN_ERROR,
            status_code=400,
            username=normalized_username,
        )

    with SessionLocal() as db:
        user = authenticate_platform_user(db, normalized_username, password)
        if user is None:
            record_auth_event(
                request,
                event="login credentials rejected",
                auth_scope="platform",
                reason="invalid_credentials",
                identity=normalized_username,
            )
            return _render_login(
                request,
                notice=PLATFORM_LOGIN_ERROR,
                status_code=401,
                username=normalized_username,
            )
        created = create_platform_session(
            db,
            user,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
        must_change_password = user.must_change_password

    record_auth_event(
        request,
        event="login succeeded",
        auth_scope="platform",
        reason="authenticated",
        identity=normalized_username,
    )

    target = PLATFORM_PASSWORD_PATH if must_change_password else PLATFORM_HOME_PATH
    response = RedirectResponse(url=target, status_code=303)
    set_platform_auth_cookies(response, created)
    return response


@router.get(PLATFORM_HOME_PATH)
async def platform_home(request: Request):
    """验证平台会话后进入实际平台控制台。"""

    raw_token = platform_session_token(request)
    with SessionLocal() as db:
        principal = load_platform_principal(db, raw_token)
    if principal is None:
        return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
    return RedirectResponse(url="/platform/dashboard", status_code=303)


@router.post(PLATFORM_LOGOUT_PATH)
async def platform_logout_action(request: Request, csrf_token: str = Form("")):
    """校验平台 CSRF 后只吊销当前浏览器会话，并按 /platform Path 删除 Cookie。"""

    raw_token = platform_session_token(request)
    with SessionLocal() as db:
        principal = load_platform_principal(db, raw_token, touch=False)
        if principal is not None:
            require_platform_csrf(db, request, csrf_token)
            revoke_platform_session_by_token(db, raw_token, reason="logout")

    response = RedirectResponse(url=f"{PLATFORM_LOGIN_PATH}?notice=" + quote("已退出登录"), status_code=303)
    delete_platform_auth_cookies(response)
    return response


@router.get(PLATFORM_PASSWORD_PATH, response_class=HTMLResponse)
async def platform_password_page(request: Request):
    """展示当前平台管理员的密码维护页。"""

    raw_token = platform_session_token(request)
    with SessionLocal() as db:
        principal = load_platform_principal(db, raw_token)
        if principal is None:
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        user = db.get(PlatformUser, principal.user_id)
        if user is None:
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        return _render_password_page(
            request,
            principal,
            user,
            notice=request.query_params.get("notice", ""),
        )


@router.post(PLATFORM_PASSWORD_PATH)
async def platform_password_action(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    csrf_token: str = Form(""),
):
    """更新平台密码、吊销全部旧平台会话并签发当前浏览器的新会话。"""

    raw_token = platform_session_token(request)
    with SessionLocal() as db:
        principal = load_platform_principal(db, raw_token, touch=False)
        if principal is None:
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        require_platform_csrf(db, request, csrf_token)
        user = db.get(PlatformUser, principal.user_id)
        if user is None or not user.is_enabled:
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)

        if not verify_password(current_password, user.password_hash):
            return _render_password_page(request, principal, user, notice="当前密码不正确", status_code=400)
        password_error = validate_new_password(current_password, new_password, confirm_password)
        if password_error:
            return _render_password_page(request, principal, user, notice=password_error, status_code=400)

        revoke_platform_sessions(db, user.id, reason="password_changed")
        user.password_hash = hash_password(new_password)
        user.auth_version += 1
        user.must_change_password = False
        db.commit()
        db.refresh(user)
        created = create_platform_session(
            db,
            user,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )

    response = RedirectResponse(url=PLATFORM_HOME_PATH + "?notice=" + quote("密码已更新"), status_code=303)
    set_platform_auth_cookies(response, created)
    return response
