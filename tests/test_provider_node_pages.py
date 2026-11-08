from __future__ import annotations

import importlib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
import app.web.provider_nodes as provider_nodes_web
from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel
from app.models.saas import BotBinding, PlatformProtocolService, ProviderNode, ProviderNodeAccount, SaasPlan, Tenant, TenantSubscription, TenantUser
from app.services.provider_nodes import ProviderNodeError, validate_channel_executor_binding
from app.services.saas_auth import hash_password


TENANT_PASSWORD = "Provider-pages-123"


@pytest.fixture()
def provider_page_context(tmp_path, monkeypatch):
    """使用真实认证中间件、租户会话和页面路由验证 Provider 管理工作流。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-pages.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(db_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(provider_nodes_web, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "init_db_with_seed", lambda _db: None)
    monkeypatch.setattr(main_module, "is_setup_required", lambda _db: False)
    # 页面测试已经自行创建完整临时结构，且不会验证 Alembic；显式放行 readiness
    # 只影响当前 monkeypatch 生命周期，不会削弱生产启动的 revision 校验。
    monkeypatch.setattr(
        main_module,
        "database_readiness",
        lambda: {"ready": True, "error": "", "expected_revision": "test-head"},
    )
    monkeypatch.setattr(main_module, "start_background_workers", lambda: None)
    monkeypatch.setattr(main_module, "stop_background_workers", lambda: None)

    # 企业登录已经归入公开站点 Router，测试数据库必须同时注入登录和后台管理模块。
    for module_name in (
        "app.web.public_site",
        "app.web.tenant_auth",
        "app.web.platform_auth",
        "app.web.tenant_admin",
        "app.web.open_api_apps",
    ):
        route_module = importlib.import_module(module_name)
        if hasattr(route_module, "SessionLocal"):
            monkeypatch.setattr(route_module, "SessionLocal", factory)
        if hasattr(route_module, "TenantSessionLocal"):
            monkeypatch.setattr(route_module, "TenantSessionLocal", factory)

    with factory() as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        plan = SaasPlan(
            code="provider-pages",
            name="节点页面套餐",
            provider_node_limit=5,
            bot_limit=5,
            status="active",
        )
        db.add_all([alpha, beta, plan])
        db.flush()
        db.add(
            TenantSubscription(
                tenant_id=alpha.id,
                plan_id=plan.id,
                status="active",
                starts_at=now_shanghai(),
                expires_at=now_shanghai() + timedelta(days=30),
            )
        )
        users = [
            TenantUser(
                tenant_id=alpha.id,
                username=username,
                mobile=mobile,
                display_name=display_name,
                role=role,
                password_hash=hash_password(TENANT_PASSWORD),
                is_enabled=True,
                must_change_password=False,
            )
            for username, mobile, display_name, role in (
                ("admin", "13300133001", "甲管理员", "tenant_admin"),
                ("operator", "13300133002", "甲运营", "operator"),
                ("readonly", "13300133003", "甲只读", "readonly"),
            )
        ]
        alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人")
        db.add_all([*users, alpha_bot])
        db.flush()
        alpha_open_channel = Channel(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            name="甲 iPad 账号",
            channel_type="wechat_ipad",
            api_base="http://ipad.platform.internal:9064",
            is_enabled=True,
        )
        active_node = ProviderNode(
            tenant_id=alpha.id,
            name="客服电脑A",
            status="active",
            device_id="provider-pages-alpha-active",
        )
        disabled_node = ProviderNode(tenant_id=alpha.id, name="停用电脑", status="disabled")
        beta_node = ProviderNode(
            tenant_id=beta.id,
            name="乙节点",
            status="active",
            device_id="provider-pages-beta-active",
        )
        # 租户接入页使用平台托管 iPad 服务；页面夹具显式准备平台配置，避免测试依赖生产初始化数据。
        ipad_protocol_service = PlatformProtocolService(
            channel_type="wechat_ipad",
            api_base="http://ipad.platform.internal:9064",
            is_enabled=True,
        )
        db.add_all([alpha_open_channel, active_node, disabled_node, beta_node, ipad_protocol_service])
        db.flush()
        account_a = ProviderNodeAccount(
            tenant_id=alpha.id,
            provider_node_id=active_node.id,
            account_key="account-a",
            external_user_id="workwechat-account-a",
            display_name="企微甲",
            status="online",
            last_seen_at=now_shanghai(),
        )
        account_b = ProviderNodeAccount(
            tenant_id=alpha.id,
            provider_node_id=active_node.id,
            account_key="account-b",
            display_name="企微乙",
            status="online",
            last_seen_at=now_shanghai(),
        )
        disabled_account = ProviderNodeAccount(
            tenant_id=alpha.id,
            provider_node_id=disabled_node.id,
            account_key="disabled-account",
            status="online",
        )
        beta_account = ProviderNodeAccount(
            tenant_id=beta.id,
            provider_node_id=beta_node.id,
            account_key="beta-account",
            status="online",
        )
        db.add_all([account_a, account_b, disabled_account, beta_account])
        db.commit()
        ids = {
            "alpha": int(alpha.id),
            "beta": int(beta.id),
            "bot": int(alpha_bot.id),
            "channel_id": int(alpha_open_channel.id),
            "active_node": int(active_node.id),
            "disabled_node": int(disabled_node.id),
            "beta_node": int(beta_node.id),
            "account_a": int(account_a.id),
            "account_b": int(account_b.id),
            "disabled_account": int(disabled_account.id),
            "beta_account": int(beta_account.id),
            "factory": factory,
        }

    with TestClient(main_module.app, follow_redirects=False) as client:
        yield client, ids

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _login(client: TestClient, username: str) -> str:
    """切换租户测试角色并返回该会话的 CSRF 原文。"""

    # 测试仍以角色名选择账号，但真实登录请求只能提交该账号绑定的全局唯一手机号。
    mobile_by_username = {
        "admin": "13300133001",
        "operator": "13300133002",
        "readonly": "13300133003",
    }
    client.cookies.clear()
    response = client.post(
        "/login",
        data={"mobile": mobile_by_username[username], "password": TENANT_PASSWORD},
    )
    assert response.status_code == 303
    csrf_token = client.cookies.get("mbs_tenant_csrf")
    assert csrf_token
    return csrf_token


def test_admin_creates_node_and_activation_code_is_only_visible_once(provider_page_context) -> None:
    """创建节点成功页展示一次激活码，刷新详情后不能再次读取原文。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    created = client.post(
        "/provider-nodes",
        data={"csrf_token": csrf_token, "name": "客服电脑B", "notes": "二楼客服区"},
    )

    assert created.status_code == 201
    code_match = re.search(r"pact_[A-Za-z0-9_-]+", created.text)
    assert code_match is not None
    activation_code = code_match.group(0)
    with ids["factory"]() as db:
        node = db.scalar(select(ProviderNode).where(ProviderNode.tenant_id == ids["alpha"], ProviderNode.name == "客服电脑B"))
        node_id = int(node.id)

    refreshed = client.get(f"/provider-nodes/{node_id}")
    assert refreshed.status_code == 200
    assert activation_code not in refreshed.text


def test_operator_and_readonly_can_view_but_cannot_manage_nodes(provider_page_context) -> None:
    """运营和只读角色可以看状态，直接调用管理路由仍由服务端返回 403。"""

    client, ids = provider_page_context
    operator_csrf = _login(client, "operator")
    assert client.get("/provider-nodes").status_code == 200
    created = client.post(
        "/provider-nodes",
        data={"csrf_token": operator_csrf, "name": "越权节点", "notes": ""},
    )
    assert created.status_code == 403

    readonly_csrf = _login(client, "readonly")
    assert client.get(f"/provider-nodes/{ids['active_node']}").status_code == 200
    issued = client.post(
        f"/provider-nodes/{ids['active_node']}/reactivate",
        data={"csrf_token": readonly_csrf},
    )
    assert issued.status_code == 403


def test_cross_tenant_node_is_hidden_and_channel_binding_is_strict(provider_page_context) -> None:
    """跨租户节点不可见；停用节点、错父账号和已占用账号都不能绑定。"""

    client, ids = provider_page_context
    _login(client, "admin")
    assert client.get(f"/provider-nodes/{ids['beta_node']}").status_code == 404

    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        node, account_a = validate_channel_executor_binding(
            db,
            channel_type="workwechat",
            provider_node_id=ids["active_node"],
            provider_account_id=ids["account_a"],
        )
        first = Channel(
            bot_id=ids["bot"],
            name="企微渠道甲",
            channel_type="workwechat",
            provider_node_id=node.id,
            provider_account_id=account_a.id,
        )
        db.add(first)
        db.commit()

        # 同节点的另一个在线账号仍可绑定第二个企微渠道。
        same_node, account_b = validate_channel_executor_binding(
            db,
            channel_type="workwechat",
            provider_node_id=ids["active_node"],
            provider_account_id=ids["account_b"],
        )
        assert same_node.id == ids["active_node"]
        assert account_b.id == ids["account_b"]

        with pytest.raises(ProviderNodeError, match="provider_account_already_bound"):
            validate_channel_executor_binding(
                db,
                channel_type="workwechat",
                provider_node_id=ids["active_node"],
                provider_account_id=ids["account_a"],
            )
        with pytest.raises(ProviderNodeError, match="provider_node_not_active"):
            validate_channel_executor_binding(
                db,
                channel_type="workwechat",
                provider_node_id=ids["disabled_node"],
                provider_account_id=ids["disabled_account"],
            )
        with pytest.raises(ProviderNodeError, match="provider_node_not_found"):
            validate_channel_executor_binding(
                db,
                channel_type="workwechat",
                provider_node_id=ids["beta_node"],
                provider_account_id=ids["beta_account"],
            )


def test_tenant_cannot_unbind_commercial_account_from_provider_page(provider_page_context) -> None:
    """租户节点页只能查看已绑定账号，商业绑定释放必须保留给平台管理员。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        channel = Channel(
            bot_id=ids["bot"],
            name="正式企微账号",
            channel_type="workwechat",
            provider_node_id=ids["active_node"],
            provider_account_id=ids["account_a"],
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        db.add(
            BotBinding(
                tenant_id=ids["alpha"],
                bot_id=ids["bot"],
                channel_id=channel.id,
                adapter_type="workwechat",
                bot_account_id="workwechat-account-a",
                device_fingerprint_hash="a" * 64,
                status="active",
            )
        )
        db.commit()
        channel_id = int(channel.id)

    page = client.get(f"/provider-nodes/{ids['active_node']}")
    assert page.status_code == 200
    assert f"/provider-nodes/{ids['active_node']}/channels/{channel_id}/unbind" not in page.text
    assert "解绑" not in page.text

    response = client.post(
        f"/provider-nodes/{ids['active_node']}/channels/{channel_id}/unbind",
        data={"csrf_token": csrf_token},
    )
    assert response.status_code in (404, 405)

    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        binding = db.scalar(select(BotBinding).where(BotBinding.channel_id == channel_id))
        assert channel.provider_node_id == ids["active_node"]
        assert channel.provider_account_id == ids["account_a"]
        assert binding.status == "active"
