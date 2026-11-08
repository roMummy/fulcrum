from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.models.saas import PlatformUser, SaasPlan, Tenant, TenantSession, TenantUser
from app.services.image_captcha import create_captcha_challenge, verify_captcha
from app.services.saas_auth import hash_password
from app.web.auth_common import PLATFORM_SESSION_COOKIE, TENANT_SESSION_COOKIE


OWNER_PASSWORD = "Owner-secret-123"
EMPLOYEE_PASSWORD = "Employee-secret-123"


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    """把主应用、公开站点和认证 Router 统一切换到隔离 SQLite 数据库。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'public-site-auth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(db_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "init_db_with_seed", lambda _db: None)
    monkeypatch.setattr(main_module, "is_setup_required", lambda _db: False)
    monkeypatch.setattr(
        main_module,
        "database_readiness",
        lambda: {"ready": True, "error": "", "expected_revision": "test-head"},
    )
    monkeypatch.setattr(main_module, "start_background_workers", lambda: None)
    monkeypatch.setattr(main_module, "stop_background_workers", lambda: None)

    for module_name in (
        "app.web.billing",
        "app.web.public_site",
        "app.web.tenant_auth",
        "app.web.platform_auth",
    ):
        try:
            route_module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        if hasattr(route_module, "SessionLocal"):
            monkeypatch.setattr(route_module, "SessionLocal", factory)

    with factory() as db:
        trial = SaasPlan(
            code="trial",
            name="免费试用",
            provider_node_limit=1,
            bot_limit=1,
            trial_days=7,
            status="active",
        )
        starter = SaasPlan(
            code="starter",
            name="入门版",
            provider_node_limit=1,
            bot_limit=1,
            monthly_price_cents=9_900,
            yearly_price_cents=99_000,
            is_public=True,
            display_order=10,
            status="active",
        )
        tenant = Tenant(code="alpha", name="甲企业", status="active")
        db.add_all([trial, starter, tenant])
        db.flush()
        db.add_all(
            [
                TenantUser(
                    tenant_id=tenant.id,
                    username="owner",
                    mobile="13800138000",
                    display_name="企业所有者",
                    role="tenant_admin",
                    password_hash=hash_password(OWNER_PASSWORD),
                    is_owner=True,
                    must_change_password=False,
                ),
                TenantUser(
                    tenant_id=tenant.id,
                    username="employee",
                    mobile="13700137000",
                    display_name="企业员工",
                    role="operator",
                    password_hash=hash_password(EMPLOYEE_PASSWORD),
                    is_owner=False,
                    must_change_password=False,
                ),
                PlatformUser(
                    username="platform",
                    display_name="平台管理员",
                    role="platform_admin",
                    password_hash=hash_password("Platform-secret-123"),
                    must_change_password=False,
                ),
            ]
        )
        db.commit()

    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def client(session_factory):
    """使用真实中间件验证公开路径、会话 Cookie 和跳转边界。"""

    with TestClient(main_module.app, follow_redirects=False) as test_client:
        yield test_client


def test_registration_captcha_is_signed_png_and_rejects_wrong_answer() -> None:
    """验证码答案不进入令牌明文，PNG 可渲染且签名验证拒绝错误答案。"""

    challenge = create_captcha_challenge(answer="23456")

    assert challenge.image_data_uri.startswith("data:image/png;base64,")
    assert "23456" not in challenge.token
    assert verify_captcha(challenge.token, "23456") is True
    assert verify_captcha(challenge.token, "23457") is False
    assert verify_captcha(challenge.token + "tampered", "23456") is False


def test_public_pages_are_accessible_but_dashboard_requires_tenant_login(client: TestClient) -> None:
    """营销、套餐、注册和登录公开可读，租户控制台仍要求可信租户会话。"""

    for path in ("/", "/pricing", "/register", "/login"):
        response = client.get(path)
        assert response.status_code == 200, path

    # 旧员工入口已从产品和路由中删除，不保留跳转或降级认证。
    assert client.get("/employee/login").status_code == 404
    assert client.post("/employee/login", data={}).status_code == 404

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/login"


def test_owner_and_employee_share_mobile_login_and_tenant_session_cookie(
    client: TestClient,
    session_factory,
) -> None:
    """企业主与企业员工都通过全局唯一手机号签发同类租户会话。"""

    owner_login = client.post(
        "/login",
        data={"mobile": "+86 138-0013-8000", "password": OWNER_PASSWORD},
    )
    assert owner_login.status_code == 303
    assert owner_login.headers["location"] == "/dashboard"
    assert TENANT_SESSION_COOKIE in owner_login.headers.get("set-cookie", "")
    assert client.get("/dashboard").status_code == 200

    client.cookies.clear()
    employee_login = client.post(
        "/login",
        data={"mobile": "13700137000", "password": EMPLOYEE_PASSWORD},
    )
    assert employee_login.status_code == 303
    assert employee_login.headers["location"] == "/dashboard"
    assert TENANT_SESSION_COOKIE in employee_login.headers.get("set-cookie", "")

    with session_factory() as db:
        assert len(db.scalars(select(TenantSession)).all()) == 2


def test_tenant_console_hides_platform_tools_and_old_internal_pages_return_404(
    client: TestClient,
) -> None:
    """企业用户只能看到租户业务能力，旧内部工具路径不保留跳转或兼容页面。"""

    login = client.post(
        "/login",
        data={"mobile": "13800138000", "password": OWNER_PASSWORD},
    )
    assert login.status_code == 303

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    for forbidden_text in ("重启服务", "开放接口", "实验台"):
        assert forbidden_text not in dashboard.text

    # 路由从应用注册表中彻底删除，企业会话通过收藏地址访问时也只能得到原生 404。
    for path in ("/monitoring", "/errors", "/lab", "/open-api/apps"):
        response = client.get(path)
        assert response.status_code == 404, path


def test_open_api_machine_route_remains_outside_tenant_page_authentication(client: TestClient) -> None:
    """机器签名接口继续进入 Open API 鉴权，不被企业页面收口误删或重定向。"""

    response = client.get("/api/open/jobs/not-found")

    assert response.status_code == 401
    assert response.headers.get("location") is None


def test_payment_webhook_machine_route_is_in_public_prefixes() -> None:
    """支付回调前缀必须归入机器接口白名单，避免被重定向到租户登录页。"""

    from app.main import PUBLIC_MACHINE_PREFIXES

    assert "/api/payment/" in PUBLIC_MACHINE_PREFIXES


def test_platform_operations_require_platform_login(client: TestClient) -> None:
    """未登录请求访问平台运维与日志时固定回到独立平台登录入口。"""

    for path in ("/platform/operations", "/platform/logs"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/platform/login"


def test_owner_login_does_not_accept_the_old_employee_form_contract(client: TestClient) -> None:
    """企业登录只接收手机号，租户编码和用户名不能恢复任何身份。"""

    response = client.post(
        "/login",
        data={"tenant_code": "alpha", "username": "employee", "password": EMPLOYEE_PASSWORD},
    )
    assert response.status_code == 400
    assert TENANT_SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_platform_cookie_cannot_authenticate_owner_entry(client: TestClient) -> None:
    """即使请求携带平台 Cookie，企业主入口也只能读取租户所有者手机号凭据。"""

    client.cookies.set(PLATFORM_SESSION_COOKIE, "forged-platform-token", path="/")
    page = client.get("/login")
    failed = client.post("/login", data={"mobile": "13800138000", "password": "wrong-password"})

    assert page.status_code == 200
    assert failed.status_code == 401
    assert TENANT_SESSION_COOKIE not in failed.headers.get("set-cookie", "")


def test_register_route_creates_session_and_redirects_to_onboarding(
    client: TestClient,
    session_factory,
    monkeypatch,
) -> None:
    """验证码通过后的公开注册会创建企业主会话并进入新手引导。"""

    public_site = importlib.import_module("app.web.public_site")
    monkeypatch.setattr(public_site, "verify_captcha", lambda _token, _answer: True)
    response = client.post(
        "/register",
        data={
            "mobile": "13900139000",
            "password": "Register-secret-123",
            "confirm_password": "Register-secret-123",
            "enterprise_name": "新注册企业",
            "contact_name": "新企业主",
            "captcha_token": "test-token",
            "captcha_answer": "12345",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/onboarding"
    assert TENANT_SESSION_COOKIE in response.headers.get("set-cookie", "")
    assert client.get("/account/onboarding").status_code == 200
    with session_factory() as db:
        owner = db.scalar(select(TenantUser).where(TenantUser.mobile == "13900139000"))
        assert owner is not None
        assert owner.is_owner is True


def test_setup_redirect_keeps_priority_over_public_site(client: TestClient, monkeypatch) -> None:
    """首次安装未完成时仍先进入平台初始化，不能直接开放注册。"""

    monkeypatch.setattr(main_module, "is_setup_required", lambda _db: True)
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
