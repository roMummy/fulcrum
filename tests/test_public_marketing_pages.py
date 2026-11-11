from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.models.saas import SaasPlan


@pytest.fixture()
def marketing_client(tmp_path, monkeypatch):
    """把公开站点切换到隔离数据库，确保价格和推荐状态来自服务端套餐数据。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'public-marketing-pages.db'}",
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

    # 公开 Router 在导入时保存 SessionLocal 引用，测试必须同步替换，避免误读开发数据库。
    public_site = importlib.import_module("app.web.public_site")
    monkeypatch.setattr(public_site, "SessionLocal", factory)

    with factory() as db:
        db.add_all(
            [
                SaasPlan(
                    code="starter",
                    name="入门版",
                    provider_node_limit=1,
                    bot_limit=1,
                    monthly_price_cents=9_900,
                    yearly_price_cents=99_000,
                    is_public=True,
                    display_order=10,
                    status="active",
                ),
                SaasPlan(
                    code="standard",
                    name="标准版",
                    provider_node_limit=3,
                    bot_limit=3,
                    monthly_price_cents=25_900,
                    yearly_price_cents=259_000,
                    is_public=True,
                    is_featured=True,
                    display_order=20,
                    status="active",
                ),
                SaasPlan(
                    code="professional",
                    name="专业版",
                    provider_node_limit=10,
                    bot_limit=10,
                    monthly_price_cents=69_900,
                    yearly_price_cents=699_000,
                    is_public=True,
                    display_order=30,
                    status="active",
                ),
                # 非公开套餐用于证明营销页面只呈现平台明确开放的商业选项。
                SaasPlan(
                    code="internal",
                    name="内部套餐",
                    provider_node_limit=99,
                    bot_limit=99,
                    monthly_price_cents=1,
                    yearly_price_cents=1,
                    is_public=False,
                    display_order=1,
                    status="active",
                ),
            ]
        )
        db.commit()

    with TestClient(main_module.app, follow_redirects=False) as client:
        yield client

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_home_uses_yaoshu_brand_generic_positioning_and_interactive_soft_hero(marketing_client: TestClient) -> None:
    """首页必须突出 Fulcrum，并用无截图 Soft UI 场景承载全渠道产品定位。"""

    response = marketing_client.get("/")

    assert response.status_code == 200
    assert '<h1>Fulcrum</h1>' in response.text
    assert "一个后台，统一运营所有智能机器人" in response.text
    assert "支持微信、企业微信、Telegram 等渠道" in response.text
    assert "免费试用 7 天" in response.text
    assert 'class="marketing-hero-scene"' in response.text
    assert "data-hero-scene" in response.text
    assert "yaoshu-console.webp" not in response.text
    assert "marketing-product-visual" not in response.text
    assert "仅企业微信" not in response.text
    assert "Provider" not in response.text


def test_pricing_renders_three_public_plans_with_server_side_prices(marketing_client: TestClient) -> None:
    """套餐页只使用数据库金额快照展示三档公开套餐，不接受模板写死价格。"""

    response = marketing_client.get("/pricing")

    assert response.status_code == 200
    for plan_name in ("入门版", "标准版", "专业版"):
        assert plan_name in response.text
    for price in ("¥99.00", "¥990.00", "¥259.00", "¥2590.00", "¥699.00", "¥6990.00"):
        assert price in response.text
    assert "内部套餐" not in response.text
    assert "推荐套餐" in response.text
    assert "Provider" not in response.text


def test_public_auth_forms_have_labels_autocomplete_and_live_error_container(marketing_client: TestClient) -> None:
    """注册和统一企业登录保留明确标签、自动填充语义及可感知错误区域。"""

    register = marketing_client.get("/register?notice=测试错误")
    enterprise_login = marketing_client.get("/login?notice=测试错误")
    removed_employee_login = marketing_client.get("/employee/login")

    assert register.status_code == enterprise_login.status_code == 200
    assert removed_employee_login.status_code == 404
    for field_name, autocomplete in (
        ("enterprise_name", "organization"),
        ("contact_name", "name"),
        ("mobile", "tel"),
        ("password", "new-password"),
        ("confirm_password", "new-password"),
    ):
        assert f'name="{field_name}"' in register.text
        assert f'autocomplete="{autocomplete}"' in register.text
    assert 'role="alert"' in register.text
    assert 'aria-live="polite"' in register.text
    assert 'name="mobile"' in enterprise_login.text
    assert 'autocomplete="username"' in enterprise_login.text
    assert 'autocomplete="current-password"' in enterprise_login.text
    assert 'role="alert"' in enterprise_login.text
    assert 'name="tenant_code"' not in enterprise_login.text
