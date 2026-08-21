"""曜枢 AI 公开营销、企业注册和企业主登录路由。"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.saas import SaasPlan, TenantUser
from app.services.image_captcha import create_captcha_challenge, verify_captcha
from app.services.registration import RegistrationError, register_trial_tenant
from app.services.saas_auth import authenticate_tenant_owner, create_tenant_session
from app.web.auth_common import (
    TENANT_HOME_PATH,
    TENANT_OWNER_LOGIN_ERROR,
    request_ip_address,
    request_user_agent,
    set_tenant_auth_cookies,
)


router = APIRouter(tags=["public-site"])
templates = Jinja2Templates(directory="app/templates")


def _public_plans(db) -> list[SaasPlan]:
    """只返回平台启用并明确公开的套餐，页面不能自行写死价格。"""

    return db.scalars(
        select(SaasPlan)
        .where(SaasPlan.status == "active", SaasPlan.is_public.is_(True))
        .order_by(SaasPlan.display_order.asc(), SaasPlan.id.asc())
    ).all()


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
        "brand_name": "曜枢 AI",
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

    challenge = create_captcha_challenge()
    context = _context(
        request,
        page_title="免费试用",
        notice=notice,
        notice_tone="danger" if notice else "",
        captcha_token=challenge.token,
        captcha_image=challenge.image_data_uri,
        form_values=form_values or {},
    )
    return templates.TemplateResponse(request, "register.html", context, status_code=status_code)


def _render_owner_login(
    request: Request,
    *,
    notice: str = "",
    status_code: int = 200,
    mobile: str = "",
):
    """渲染企业主手机号登录页，认证失败时绝不回填密码。"""

    context = _context(
        request,
        page_title="企业主登录",
        notice=notice,
        notice_tone="danger" if notice else "",
        prefill_mobile=mobile,
    )
    return templates.TemplateResponse(request, "owner_login.html", context, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
async def marketing_home_page(request: Request):
    """展示曜枢 AI 公开首页，并按当前租户会话切换主行动入口。"""

    with SessionLocal() as db:
        plans = _public_plans(db)[:3]
    return templates.TemplateResponse(
        request,
        "marketing_home.html",
        _context(request, page_title="曜枢 AI", plans=plans),
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
    if not verify_captcha(captcha_token, captcha_answer):
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
async def owner_login_page(request: Request):
    """展示企业所有者手机号登录页。"""

    if getattr(request.state, "current_user", None) is not None:
        return RedirectResponse(url=TENANT_HOME_PATH, status_code=303)
    return _render_owner_login(request, notice=request.query_params.get("notice", ""))


@router.post("/login/owner", include_in_schema=False)
@router.post("/login")
async def owner_login_action(
    request: Request,
    mobile: str = Form(""),
    password: str = Form(""),
):
    """只按所有者手机号验证，不接收企业编码或员工用户名作为身份依据。"""

    normalized_mobile = (mobile or "").strip()
    if not normalized_mobile or not password:
        return _render_owner_login(
            request,
            notice=TENANT_OWNER_LOGIN_ERROR,
            status_code=400,
            mobile=normalized_mobile,
        )
    with SessionLocal() as db:
        owner = authenticate_tenant_owner(db, normalized_mobile, password)
        if owner is None:
            return _render_owner_login(
                request,
                notice=TENANT_OWNER_LOGIN_ERROR,
                status_code=401,
                mobile=normalized_mobile,
            )
        session = create_tenant_session(
            db,
            owner,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )

    response = RedirectResponse(url=TENANT_HOME_PATH, status_code=303)
    set_tenant_auth_cookies(response, session)
    return response
