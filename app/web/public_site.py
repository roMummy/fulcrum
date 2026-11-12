"""Fulcrum 公开营销、企业注册和企业统一登录路由。"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.models.saas import SaasPlan, TenantUser
from app.services.billing import list_public_plans
from app.services.image_captcha import (
    CAPTCHA_PURPOSES,
    CaptchaChallenge,
    consume_captcha_challenge,
    create_captcha_challenge,
)
from app.services.registration import RegistrationError, register_trial_tenant
from app.services.saas_auth import authenticate_tenant_account, create_tenant_session
from app.web.auth_common import (
    REGISTRATION_CAPTCHA_PURPOSE,
    TENANT_HOME_PATH,
    TENANT_LOGIN_CAPTCHA_PURPOSE,
    TENANT_LOGIN_ERROR,
    record_auth_event,
    request_ip_address,
    request_user_agent,
    set_tenant_auth_cookies,
)


router = APIRouter(tags=["public-site"])
templates = Jinja2Templates(directory="app/templates")


def _public_plans(db) -> list[SaasPlan]:
    """只返回平台启用并明确公开的套餐，页面不能自行写死价格。"""

    return list_public_plans(db)


def _format_yuan(cents: int) -> str:
    """把整数分格式化为两位小数，公开页面不执行浮点金额运算。"""

    return f"{Decimal(int(cents)) / Decimal(100):.2f}"


def _context(request: Request, *, page_title: str, **extra) -> dict[str, object]:
    """构造公开站点共享上下文，只暴露是否已登录，不输出租户主键等内部信息。"""

    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": getattr(request.state, "current_user", None),
        "format_yuan": _format_yuan,
        "brand_name": "Fulcrum",
    }
    context.update(extra)
    return context


def _render_register(
    request: Request,
    *,
    notice: str = "",
    status_code: int = 200,
    form_values: dict[str, str] | None = None,
):
    """每次渲染注册页都签发新验证码，失败时只回填非敏感文本字段。"""

    challenge = create_captcha_challenge(purpose=REGISTRATION_CAPTCHA_PURPOSE)
    context = _context(
        request,
        page_title="免费试用",
        notice=notice,
        notice_tone="danger" if notice else "",
        captcha_token=challenge.token,
        captcha_image=challenge.image_data_uri,
        captcha_refresh_url="/auth/captcha?purpose=" + REGISTRATION_CAPTCHA_PURPOSE,
        form_values=form_values or {},
    )
    return templates.TemplateResponse(request, "register.html", context, status_code=status_code)


def _render_enterprise_login(
    request: Request,
    *,
    notice: str = "",
    status_code: int = 200,
    mobile: str = "",
    challenge: CaptchaChallenge | None = None,
):
    """渲染企业账号手机号登录页，认证失败时绝不回填密码。"""

    resolved_challenge = challenge or create_captcha_challenge(purpose=TENANT_LOGIN_CAPTCHA_PURPOSE)
    context = _context(
        request,
        page_title="企业账号登录",
        notice=notice,
        notice_tone="danger" if notice else "",
        prefill_mobile=mobile,
        captcha_token=resolved_challenge.token,
        captcha_image=resolved_challenge.image_data_uri,
        captcha_refresh_url="/auth/captcha?purpose=" + TENANT_LOGIN_CAPTCHA_PURPOSE,
    )
    return templates.TemplateResponse(request, "enterprise_login.html", context, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
async def marketing_home_page(request: Request):
    """展示 Fulcrum 公开首页，并按当前租户会话切换主行动入口。"""

    with SessionLocal() as db:
        plans = _public_plans(db)[:3]
    return templates.TemplateResponse(
        request,
        "marketing_home.html",
        _context(request, page_title="Fulcrum", plans=plans),
    )


@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    """展示数据库中的公开套餐和月年价格。"""

    with SessionLocal() as db:
        plans = _public_plans(db)
    return templates.TemplateResponse(
        request,
        "pricing.html",
        _context(request, page_title="套餐价格", plans=plans),
    )


@router.get("/auth/captcha", response_class=JSONResponse)
async def auth_captcha(purpose: str = REGISTRATION_CAPTCHA_PURPOSE):
    """签发认证页换图所需的短时挑战，不建立数据库记录。"""

    normalized_purpose = (purpose or "").strip().lower()
    if normalized_purpose not in CAPTCHA_PURPOSES:
        raise HTTPException(status_code=400, detail="captcha_purpose_invalid")
    challenge = create_captcha_challenge(purpose=normalized_purpose)
    return JSONResponse(
        {
            "purpose": challenge.purpose,
            "token": challenge.token,
            "image": challenge.image_data_uri,
            "expires_at": challenge.expires_at,
        },
        headers={"Cache-Control": "no-store"},  # 一次性挑战不能被浏览器或代理缓存
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """展示企业自助注册页；已有租户会话直接进入控制台。"""

    if getattr(request.state, "current_user", None) is not None:
        return RedirectResponse(url=TENANT_HOME_PATH, status_code=303)
    return _render_register(request, notice=request.query_params.get("notice", ""))


@router.post("/register")
async def register_action(
    request: Request,
    mobile: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    enterprise_name: str = Form(""),
    contact_name: str = Form(""),
    captcha_token: str = Form(""),
    captcha_answer: str = Form(""),
):
    """校验验证码后创建试用企业，并为企业所有者签发现有租户会话。"""

    if getattr(request.state, "current_user", None) is not None:
        return RedirectResponse(url=TENANT_HOME_PATH, status_code=303)
    safe_values = {
        "mobile": (mobile or "").strip(),
        "enterprise_name": (enterprise_name or "").strip(),
        "contact_name": (contact_name or "").strip(),
    }
    captcha_result = consume_captcha_challenge(
        captcha_token,
        captcha_answer,
        purpose=REGISTRATION_CAPTCHA_PURPOSE,
    )
    if not captcha_result.valid:
        record_auth_event(
            request,
            event="registration captcha rejected",
            auth_scope="registration",
            reason=captcha_result.reason,
            identity=mobile,
        )
        return _render_register(
            request,
            notice="图形验证码错误或已过期",
            status_code=400,
            form_values=safe_values,
        )
    if password != confirm_password:
        return _render_register(
            request,
            notice="两次输入的密码不一致",
            status_code=400,
            form_values=safe_values,
        )

    with SessionLocal() as db:
        try:
            created = register_trial_tenant(
                db,
                mobile=mobile,
                password=password,
                enterprise_name=enterprise_name,
                contact_name=contact_name,
            )
        except RegistrationError as exc:
            error_messages = {
                "mobile_invalid": "请输入有效的手机号",
                "mobile_already_registered": "该手机号已注册，请直接登录",
                "password_too_short": "密码至少需要 8 位",
                "password_too_long": "密码长度不能超过 128 位",
                "enterprise_name_invalid": "请输入有效的企业名称",
                "contact_name_invalid": "请输入联系人姓名",
                "trial_plan_not_found": "免费试用暂不可用，请稍后再试",
                "trial_plan_disabled": "免费试用暂不可用，请稍后再试",
                "trial_plan_invalid": "免费试用暂不可用，请稍后再试",
            }
            return _render_register(
                request,
                notice=error_messages.get(str(exc), "注册失败，请稍后重试"),
                status_code=400,
                form_values=safe_values,
            )
        owner = db.get(TenantUser, created.owner_user_id)
        if owner is None:
            # 注册事务已经成功但所有者读取异常时不伪造会话，交由登录入口恢复。
            return RedirectResponse(
                url="/login?notice=" + quote("注册成功，请使用手机号登录"),
                status_code=303,
            )
        session = create_tenant_session(
            db,
            owner,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )

    response = RedirectResponse(url="/account/onboarding", status_code=303)
    set_tenant_auth_cookies(response, session)
    return response


@router.get("/login", response_class=HTMLResponse)
async def enterprise_login_page(request: Request):
    """展示企业所有者与企业员工共用的手机号登录页。"""

    if getattr(request.state, "current_user", None) is not None:
        return RedirectResponse(url=TENANT_HOME_PATH, status_code=303)
    return _render_enterprise_login(request, notice=request.query_params.get("notice", ""))


@router.post("/login")
async def enterprise_login_action(
    request: Request,
    mobile: str = Form(""),
    password: str = Form(""),
    captcha_token: str = Form(""),
    captcha_answer: str = Form(""),
):
    """只按企业账号手机号验证，由账号记录恢复租户，不接受客户端提交租户身份。"""

    normalized_mobile = (mobile or "").strip()
    captcha_result = consume_captcha_challenge(
        captcha_token,
        captcha_answer,
        purpose=TENANT_LOGIN_CAPTCHA_PURPOSE,
    )
    if not captcha_result.valid:
        record_auth_event(
            request,
            event="login captcha rejected",
            auth_scope="tenant",
            reason=captcha_result.reason,
            identity=normalized_mobile,
        )
        return _render_enterprise_login(
            request,
            notice="图形验证码错误或已过期",
            status_code=400,
            mobile=normalized_mobile,
        )
    if not normalized_mobile or not password:
        record_auth_event(
            request,
            event="login credentials rejected",
            auth_scope="tenant",
            reason="missing_credentials",
            identity=normalized_mobile,
        )
        return _render_enterprise_login(
            request,
            notice=TENANT_LOGIN_ERROR,
            status_code=400,
            mobile=normalized_mobile,
        )
    with SessionLocal() as db:
        user = authenticate_tenant_account(db, normalized_mobile, password)
        if user is None:
            record_auth_event(
                request,
                event="login credentials rejected",
                auth_scope="tenant",
                reason="invalid_credentials",
                identity=normalized_mobile,
            )
            return _render_enterprise_login(
                request,
                notice=TENANT_LOGIN_ERROR,
                status_code=401,
                mobile=normalized_mobile,
            )
        session = create_tenant_session(
            db,
            user,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )

    record_auth_event(
        request,
        event="login succeeded",
        auth_scope="tenant",
        reason="authenticated",
        identity=normalized_mobile,
    )

    response = RedirectResponse(url=TENANT_HOME_PATH, status_code=303)
    set_tenant_auth_cookies(response, session)
    return response
