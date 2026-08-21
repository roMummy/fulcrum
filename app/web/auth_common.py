"""平台与租户浏览器认证共用的 HTTP 边界辅助函数。

本模块只负责 Cookie、请求元信息、模板基础上下文和首次改密跳转判断。密码哈希、
数据库会话校验及吊销仍由 ``app.services.saas_auth`` 负责，避免 Web 层复制安全逻辑。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.time import now_shanghai
from app.services.csrf import verify_csrf_token
from app.services.saas_auth import CreatedSession, PlatformPrincipal, TenantPrincipal


# 新 SaaS 身份使用全新 Cookie 名称，旧单租户后台的 ``mba_auth`` 即使仍可验签，
# 也不会被任何新路由当作平台或租户身份。平台 Cookie 进一步限制在 /platform 路径。
TENANT_SESSION_COOKIE = "mbs_tenant_session"
TENANT_CSRF_COOKIE = "mbs_tenant_csrf"
PLATFORM_SESSION_COOKIE = "mbs_platform_session"
PLATFORM_CSRF_COOKIE = "mbs_platform_csrf"
LEGACY_AUTH_COOKIE = "mba_auth"

TENANT_COOKIE_PATH = "/"
PLATFORM_COOKIE_PATH = "/platform"

# 企业所有者与企业员工统一使用手机号入口，租户身份始终由服务端账号记录恢复。
TENANT_LOGIN_PATH = "/login"
TENANT_LOGOUT_PATH = "/logout"
TENANT_HOME_PATH = "/dashboard"
TENANT_PASSWORD_PATH = "/profile/password"

PLATFORM_LOGIN_PATH = "/platform/login"
PLATFORM_LOGOUT_PATH = "/platform/logout"
PLATFORM_HOME_PATH = "/platform"
PLATFORM_PASSWORD_PATH = "/platform/profile/password"

TENANT_LOGIN_ERROR = "手机号或密码错误"
PLATFORM_LOGIN_ERROR = "用户名或密码错误"

# 中间件发现账号仍使用一次性初始密码时，只允许进入改密和注销端点。静态资源、
# 登录页及机器接口应在主应用的公共路径判断中提前处理，不应混入此业务白名单。
TENANT_MUST_CHANGE_ALLOWED_PATHS = frozenset({TENANT_PASSWORD_PATH, TENANT_LOGOUT_PATH})
PLATFORM_MUST_CHANGE_ALLOWED_PATHS = frozenset({PLATFORM_PASSWORD_PATH, PLATFORM_LOGOUT_PATH})


def build_auth_page_context(
    request: Request,
    *,
    page_title: str,
    notice: str = "",
    notice_tone: str = "",
    current_user: TenantPrincipal | PlatformPrincipal | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造可以直接交给共享 ``base.html`` 的稳定模板上下文。

    认证 Router 不能反向导入 ``app.main.render``，否则主应用注册 Router 时会产生循环
    依赖。这里显式提供导航占位和通知色调，让登录页、改密页在独立测试应用中也能渲染。
    """

    resolved_tone = notice_tone or ("danger" if notice else "info")
    context: dict[str, Any] = {
        "request": request,
        "page_title": page_title,
        "notice": notice,
        "notice_tone": resolved_tone,
        "current_user": current_user,
        "app_started_at": now_shanghai(),
        "nav_items": [],
        "primary_nav_items": [],
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": [],
        "module_links": {},
    }
    context.update(extra)
    return context


def request_ip_address(request: Request) -> str | None:
    """读取与当前 TCP 连接对应的客户端地址，并限制为数据库字段可接受长度。

    这里故意不直接信任 ``X-Forwarded-For``。只有部署层明确配置可信反向代理后，才应
    在统一代理中间件中重写 ``request.client``，否则攻击者可伪造头部绕过登录限速。
    """

    if request.client is None:
        return None
    return (request.client.host or "").strip()[:64] or None


def request_user_agent(request: Request) -> str | None:
    """提取浏览器标识用于会话审计，过长值在进入数据库前主动截断。"""

    return (request.headers.get("user-agent") or "").strip()[:1000] or None


def tenant_session_token(request: Request) -> str | None:
    """返回租户会话 Cookie 原始值；调用方不得写入日志。"""

    return request.cookies.get(TENANT_SESSION_COOKIE)


def platform_session_token(request: Request) -> str | None:
    """返回平台会话 Cookie 原始值；调用方不得写入日志。"""

    return request.cookies.get(PLATFORM_SESSION_COOKIE)


def _set_session_cookie(response: Response, name: str, value: str, *, path: str) -> None:
    """设置仅浏览器 HTTP 请求可见的高熵会话令牌。"""

    response.set_cookie(
        name,
        value,
        max_age=settings.auth_cookie_max_age_seconds,
        httponly=True,
        secure=settings.should_use_secure_cookie,
        samesite=settings.auth_cookie_samesite,
        path=path,
    )


def _set_csrf_cookie(response: Response, name: str, value: str, *, path: str) -> None:
    """设置双提交 CSRF Cookie。

    CSRF 原始值需要由服务端模板写入隐藏表单字段，因此不设置 HttpOnly；真正的登录
    身份仍只保存在独立的 HttpOnly 会话 Cookie 中，单独得到 CSRF 值不能恢复身份。
    """

    response.set_cookie(
        name,
        value,
        max_age=settings.auth_cookie_max_age_seconds,
        httponly=False,
        secure=settings.should_use_secure_cookie,
        samesite=settings.auth_cookie_samesite,
        path=path,
    )


def set_tenant_auth_cookies(response: Response, created: CreatedSession) -> None:
    """把新租户数据库会话对应的两个原始令牌交给浏览器。"""

    _set_session_cookie(response, TENANT_SESSION_COOKIE, created.raw_token, path=TENANT_COOKIE_PATH)
    _set_csrf_cookie(response, TENANT_CSRF_COOKIE, created.raw_csrf_token, path=TENANT_COOKIE_PATH)
    # 登录成功后清掉同路径旧 Cookie，避免浏览器继续携带已废弃身份造成排障混淆。
    response.delete_cookie(
        LEGACY_AUTH_COOKIE,
        path=TENANT_COOKIE_PATH,
        secure=settings.should_use_secure_cookie,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )


def set_platform_auth_cookies(response: Response, created: CreatedSession) -> None:
    """设置只会发送给 /platform 路径的平台会话与 CSRF Cookie。"""

    _set_session_cookie(response, PLATFORM_SESSION_COOKIE, created.raw_token, path=PLATFORM_COOKIE_PATH)
    _set_csrf_cookie(response, PLATFORM_CSRF_COOKIE, created.raw_csrf_token, path=PLATFORM_COOKIE_PATH)


def _delete_cookie(response: Response, name: str, *, path: str, httponly: bool) -> None:
    """使用与创建时完全一致的安全属性和 Path 删除指定 Cookie。"""

    response.delete_cookie(
        name,
        path=path,
        secure=settings.should_use_secure_cookie,
        httponly=httponly,
        samesite=settings.auth_cookie_samesite,
    )


def delete_tenant_auth_cookies(response: Response) -> None:
    """删除租户会话 Cookie 和配套 CSRF Cookie。"""

    _delete_cookie(response, TENANT_SESSION_COOKIE, path=TENANT_COOKIE_PATH, httponly=True)
    _delete_cookie(response, TENANT_CSRF_COOKIE, path=TENANT_COOKIE_PATH, httponly=False)


def delete_platform_auth_cookies(response: Response) -> None:
    """删除平台路径下的会话 Cookie 和配套 CSRF Cookie。"""

    _delete_cookie(response, PLATFORM_SESSION_COOKIE, path=PLATFORM_COOKIE_PATH, httponly=True)
    _delete_cookie(response, PLATFORM_CSRF_COOKIE, path=PLATFORM_COOKIE_PATH, httponly=False)


def require_tenant_csrf(db: Session, request: Request, submitted_token: str | None) -> None:
    """校验租户修改请求的会话、Cookie 与表单 CSRF 三方一致性。"""

    if not verify_csrf_token(
        db,
        tenant_session_token(request),
        request.cookies.get(TENANT_CSRF_COOKIE),
        submitted_token,
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def require_platform_csrf(db: Session, request: Request, submitted_token: str | None) -> None:
    """校验平台修改请求，明确选择平台会话表，防止租户令牌跨入口复用。"""

    if not verify_csrf_token(
        db,
        platform_session_token(request),
        request.cookies.get(PLATFORM_CSRF_COOKIE),
        submitted_token,
        platform=True,
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def tenant_must_change_password_redirect(principal: TenantPrincipal | None, request_path: str) -> str | None:
    """返回租户首次改密重定向地址；不需要重定向时返回 ``None``。"""

    if principal is None or not principal.must_change_password:
        return None
    return None if request_path in TENANT_MUST_CHANGE_ALLOWED_PATHS else TENANT_PASSWORD_PATH


def platform_must_change_password_redirect(
    principal: PlatformPrincipal | None,
    request_path: str,
) -> str | None:
    """返回平台首次改密重定向地址；平台与租户白名单始终保持独立。"""

    if principal is None or not principal.must_change_password:
        return None
    return None if request_path in PLATFORM_MUST_CHANGE_ALLOWED_PATHS else PLATFORM_PASSWORD_PATH


def validate_new_password(current_password: str, new_password: str, confirm_password: str) -> str | None:
    """执行两类账号共用的基础改密输入校验，返回可展示错误或 ``None``。"""

    if new_password != confirm_password:
        return "两次输入的新密码不一致"
    if len(new_password) < 8:
        return "密码至少需要 8 位"
    if len(new_password) > 128:
        return "密码长度不能超过 128 位"
    if new_password == current_password:
        return "新密码不能与当前密码相同"
    return None
