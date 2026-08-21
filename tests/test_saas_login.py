from __future__ import annotations

import importlib
from http.cookies import Morsel, SimpleCookie

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.models.entities import AdminUser
from app.models.saas import PlatformSession, PlatformUser, Tenant, TenantSession, TenantUser
from app.services.auth import build_auth_cookie, hash_password as hash_legacy_password
from app.services.saas_auth import hash_credential, hash_password


# 新 SaaS 身份必须使用全新的 Cookie 名称，避免浏览器把旧单租户后台的签名身份
# 自动升级成租户或平台身份。平台 Cookie 再通过独立 Path 缩小发送范围。
TENANT_SESSION_COOKIE = "mbs_tenant_session"
TENANT_CSRF_COOKIE = "mbs_tenant_csrf"
PLATFORM_SESSION_COOKIE = "mbs_platform_session"
PLATFORM_CSRF_COOKIE = "mbs_platform_csrf"
TENANT_LOGIN_ERROR = "手机号或密码错误"
PLATFORM_LOGIN_ERROR = "用户名或密码错误"

TENANT_PASSWORD = "Tenant-secret-123"
PLATFORM_PASSWORD = "Platform-secret-123"


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    """把 HTTP 路由及数据库依赖统一切换到当前用例的临时 SQLite 数据库。

    登录路由会经过应用中间件、路由模块和启动生命周期。测试同时替换这些入口
    可能持有的 SessionLocal，避免任何认证请求读写开发数据库，也避免启动后台
    Worker 给聚焦路由测试引入线程和外部服务噪声。
    """

    engine = create_engine(
        f"sqlite:///{tmp_path / 'saas-login.db'}",
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
    # 本用例直接用 SQLAlchemy 元数据构造隔离临时库，并跳过正式启动初始化；因此
    # 需要显式声明数据库已就绪，避免新的生产 revision 门禁把聚焦登录请求拦为 503。
    monkeypatch.setattr(
        main_module,
        "database_readiness",
        lambda: {"ready": True, "error": "", "expected_revision": "test-head"},
    )
    monkeypatch.setattr(main_module, "start_background_workers", lambda: None)
    monkeypatch.setattr(main_module, "stop_background_workers", lambda: None)

    # Task 6 会把两类认证路由拆到 app.web。这里兼容路由模块直接导入
    # SessionLocal 的实现方式，使测试在模块落地后仍只连接当前临时数据库。
    for module_name in (
        "app.web.public_site",
        "app.web.tenant_auth",
        "app.web.platform_auth",
        "app.web.tenant_admin",
    ):
        try:
            route_module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in {"app.web", module_name}:
                continue
            raise
        if hasattr(route_module, "SessionLocal"):
            monkeypatch.setattr(route_module, "SessionLocal", factory)
        if hasattr(route_module, "TenantSessionLocal"):
            monkeypatch.setattr(route_module, "TenantSessionLocal", factory)

    def override_get_db():
        """为使用 FastAPI Depends(get_db) 的新路由提供同一临时会话工厂。"""

        with factory() as db:
            yield db

    original_overrides = dict(main_module.app.dependency_overrides)
    main_module.app.dependency_overrides[db_module.get_db] = override_get_db
    try:
        yield factory
    finally:
        main_module.app.dependency_overrides.clear()
        main_module.app.dependency_overrides.update(original_overrides)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def identities(session_factory) -> None:
    """创建同名的平台账号和租户员工，用不同密码证明两个入口不会串表。"""

    with session_factory() as db:
        tenant = Tenant(code="alpha", name="甲租户", status="active")
        db.add(tenant)
        db.flush()
        db.add_all(
            [
                TenantUser(
                    tenant_id=tenant.id,
                    username="ops",
                    mobile="13600136000",
                    display_name="租户运营",
                    role="operator",
                    password_hash=hash_password(TENANT_PASSWORD),
                    is_enabled=True,
                    auth_version=1,
                    must_change_password=False,
                ),
                PlatformUser(
                    username="ops",
                    display_name="平台运营",
                    role="platform_admin",
                    password_hash=hash_password(PLATFORM_PASSWORD),
                    is_enabled=True,
                    auth_version=1,
                    must_change_password=False,
                ),
                AdminUser(
                    username="legacy-admin",
                    display_name="旧后台管理员",
                    password_hash=hash_legacy_password("legacy-secret"),
                    role="admin",
                    is_enabled=True,
                    auth_version=1,
                ),
            ]
        )
        db.commit()


@pytest.fixture()
def client(session_factory):
    """使用真实 FastAPI 中间件和 Cookie 容器验证浏览器可见的认证契约。"""

    with TestClient(main_module.app, follow_redirects=False) as test_client:
        yield test_client


def _response_cookie(response, name: str) -> Morsel[str]:
    """从多个 Set-Cookie 响应头中精确取出目标 Cookie 及其属性。"""

    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        if name in parsed:
            return parsed[name]
    raise AssertionError(f"响应没有设置 Cookie: {name}")


def _assert_session_cookie(morsel: Morsel[str], *, expected_path: str) -> None:
    """会话 Cookie 必须限制路径，并禁止前端脚本读取原始会话令牌。"""

    assert morsel.value
    assert morsel["path"] == expected_path
    assert morsel["httponly"] is True
    assert morsel["samesite"].lower() == "lax"


def _assert_csrf_cookie(morsel: Morsel[str], *, expected_path: str) -> None:
    """双提交 CSRF Cookie 与对应身份同路径，且允许页面提交其原始值。"""

    assert morsel.value
    assert morsel["path"] == expected_path
    assert morsel["httponly"] == ""
    assert morsel["samesite"].lower() == "lax"


def _assert_deleted_cookie(response, name: str, *, expected_path: str) -> None:
    """注销必须用创建时相同的 Path 删除 Cookie，否则浏览器仍会继续发送旧值。"""

    morsel = _response_cookie(response, name)
    assert morsel.value == ""
    assert morsel["path"] == expected_path
    assert morsel["max-age"] == "0"


def test_enterprise_and_platform_login_pages_are_separate(client: TestClient) -> None:
    """企业用户与平台员工使用两个明确分离的登录入口。"""

    enterprise_page = client.get("/login")
    removed_employee_page = client.get("/employee/login")
    platform_page = client.get("/platform/login")

    assert enterprise_page.status_code == 200
    assert "手机号" in enterprise_page.text
    assert 'action="/login"' in enterprise_page.text
    assert removed_employee_page.status_code == 404
    assert platform_page.status_code == 200
    assert "平台" in platform_page.text
    assert 'action="/platform/login"' in platform_page.text


def test_login_errors_are_uniform_and_identity_tables_cannot_be_mixed(
    client: TestClient,
    identities: None,
) -> None:
    """同名账号也只能用所属入口登录，失败提示不能泄露租户或账号是否存在。"""

    tenant_wrong_password = client.post(
        "/login",
        data={"mobile": "13600136000", "password": PLATFORM_PASSWORD},
    )
    tenant_unknown = client.post(
        "/login",
        data={"mobile": "13500135000", "password": TENANT_PASSWORD},
    )
    platform_wrong_password = client.post(
        "/platform/login",
        data={"username": "ops", "password": TENANT_PASSWORD},
    )
    platform_unknown = client.post(
        "/platform/login",
        data={"username": "nobody", "password": PLATFORM_PASSWORD},
    )

    for response in (tenant_wrong_password, tenant_unknown):
        assert response.status_code == 401
        assert TENANT_LOGIN_ERROR in response.text
    for response in (platform_wrong_password, platform_unknown):
        assert response.status_code == 401
        assert PLATFORM_LOGIN_ERROR in response.text


def test_tenant_login_creates_database_session_and_logout_revokes_it(
    client: TestClient,
    session_factory,
    identities: None,
) -> None:
    """租户登录只向浏览器返回原始令牌，注销后数据库会话和两个 Cookie 同时失效。"""

    login = client.post(
        "/login",
        data={"mobile": "13600136000", "password": TENANT_PASSWORD},
    )

    assert login.status_code == 303
    assert login.headers["location"] == "/dashboard"
    session_cookie = _response_cookie(login, TENANT_SESSION_COOKIE)
    csrf_cookie = _response_cookie(login, TENANT_CSRF_COOKIE)
    _assert_session_cookie(session_cookie, expected_path="/")
    _assert_csrf_cookie(csrf_cookie, expected_path="/")
    assert PLATFORM_SESSION_COOKIE not in login.headers.get("set-cookie", "")
    assert PLATFORM_CSRF_COOKIE not in login.headers.get("set-cookie", "")

    with session_factory() as db:
        records = db.scalars(select(TenantSession)).all()
        assert len(records) == 1
        assert records[0].session_token_hash == hash_credential(session_cookie.value)
        assert records[0].csrf_token_hash == hash_credential(csrf_cookie.value)
        assert records[0].revoked_at is None

    logout = client.post("/logout", data={"csrf_token": csrf_cookie.value})

    assert logout.status_code == 303
    assert logout.headers["location"].startswith("/login")
    _assert_deleted_cookie(logout, TENANT_SESSION_COOKIE, expected_path="/")
    _assert_deleted_cookie(logout, TENANT_CSRF_COOKIE, expected_path="/")
    with session_factory() as db:
        record = db.scalar(select(TenantSession))
        assert record is not None
        assert record.revoked_at is not None
        assert record.revoke_reason


def test_platform_login_uses_platform_path_and_logout_revokes_session(
    client: TestClient,
    session_factory,
    identities: None,
) -> None:
    """平台会话使用独立表和 /platform 路径，不能污染租户后台的根路径 Cookie。"""

    login = client.post(
        "/platform/login",
        data={"username": "ops", "password": PLATFORM_PASSWORD},
    )

    assert login.status_code == 303
    assert login.headers["location"].startswith("/platform")
    assert not login.headers["location"].startswith("/platform/login")
    session_cookie = _response_cookie(login, PLATFORM_SESSION_COOKIE)
    csrf_cookie = _response_cookie(login, PLATFORM_CSRF_COOKIE)
    _assert_session_cookie(session_cookie, expected_path="/platform")
    _assert_csrf_cookie(csrf_cookie, expected_path="/platform")
    assert TENANT_SESSION_COOKIE not in login.headers.get("set-cookie", "")
    assert TENANT_CSRF_COOKIE not in login.headers.get("set-cookie", "")

    with session_factory() as db:
        records = db.scalars(select(PlatformSession)).all()
        assert len(records) == 1
        assert records[0].session_token_hash == hash_credential(session_cookie.value)
        assert records[0].csrf_token_hash == hash_credential(csrf_cookie.value)
        assert records[0].revoked_at is None

    logout = client.post("/platform/logout", data={"csrf_token": csrf_cookie.value})

    assert logout.status_code == 303
    assert logout.headers["location"].startswith("/platform/login")
    _assert_deleted_cookie(logout, PLATFORM_SESSION_COOKIE, expected_path="/platform")
    _assert_deleted_cookie(logout, PLATFORM_CSRF_COOKIE, expected_path="/platform")
    with session_factory() as db:
        record = db.scalar(select(PlatformSession))
        assert record is not None
        assert record.revoked_at is not None
        assert record.revoke_reason


def test_legacy_mba_auth_cookie_cannot_create_new_saas_identity(
    client: TestClient,
    session_factory,
    identities: None,
) -> None:
    """旧 AdminUser 签名 Cookie 即使仍可验签，也不能进入新的租户 SaaS 页面。"""

    with session_factory() as db:
        legacy_user = db.scalar(select(AdminUser).where(AdminUser.username == "legacy-admin"))
        assert legacy_user is not None
        legacy_cookie = build_auth_cookie(legacy_user)

    client.cookies.set("mba_auth", legacy_cookie, path="/")
    response = client.get("/dashboard")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    with session_factory() as db:
        assert db.scalar(select(TenantSession)) is None
        assert db.scalar(select(PlatformSession)) is None
