"""插件平台管控:全平台停用、可见范围、目录缺失不再删数据、平台管理页。

背景:插件目录全平台共享,以前平台方没有任何管控入口;而且目录里找不到的插件会被直接删除,
删除会级联清掉所有租户的绑定、配置、加密密钥和状态数据(目录被误清空时全部丢失)。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="plugin-governance-logs-"))

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.web.platform_operations as platform_operations_web
import app.web.platform_plugins as platform_plugins_web
from app.core.db import Base
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Plugin, PluginGrant
from app.models.saas import PlatformAuditLog, PlatformUser, SaasPlan, Tenant
from app.services import plugin_directory
from app.services import plugin_governance as governance
from app.services.saas_auth import create_platform_session, hash_password, load_platform_principal
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)


# ---------- 判定规则(纯函数) ----------

@pytest.mark.parametrize(
    "status, visibility, grants, plan_id, expected",
    [
        ("active", "all", [], None, True),
        ("disabled", "all", [], None, False),  # 停用优先于一切
        ("active", "platform_only", [("tenant", 7)], 3, False),  # 仅平台可见:连名单里的租户也不行
        ("active", "plans", [("plan", 3)], 3, True),
        ("active", "plans", [("plan", 3)], 4, False),
        ("active", "plans", [("plan", 3)], None, False),  # 没有有效订阅
        ("active", "tenants", [("tenant", 7)], None, True),
        ("active", "tenants", [("tenant", 8)], None, False),
        ("active", "tenants", [("plan", 7)], None, False),  # 套餐 7 不等于租户 7
        ("active", "whatever", [], None, False),  # 不认识的可见范围:宁可拦
        ("paused", "all", [], None, False),  # 不认识的状态:宁可拦
    ],
)
def test_availability_rule_defaults_to_blocking(status, visibility, grants, plan_id, expected) -> None:
    assert governance.is_available(platform_status=status, visibility=visibility, grants=grants, tenant_id=7, plan_id=plan_id) is expected


# ---------- 目录缺失不再删数据 ----------

@pytest.fixture()
def pages(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db, force=True)
        weather = db.scalar(select(Plugin).where(Plugin.code == "weather"))
        binding = BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=weather.id, status="enabled", runtime_status="running", config_json="{}")
        db.add(binding)
        db.commit()
        ids = {**ids, "weather": int(weather.id), "binding": int(binding.id)}
    return client, ids


def test_a_plugin_missing_from_the_directory_is_marked_not_deleted(pages, tmp_path, monkeypatch) -> None:
    _client, ids = pages
    monkeypatch.setattr(settings, "plugins_dir", str(tmp_path))  # 空目录:模拟部署漏拷贝
    plugin_directory._SCAN_CACHE.clear()
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db, force=True)
        weather = db.get(Plugin, ids["weather"])
        assert weather is not None and weather.missing_at is not None
        assert db.get(BotPlugin, ids["binding"]) is not None  # 绑定没有被级联删除
    monkeypatch.undo()
    plugin_directory._SCAN_CACHE.clear()
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db, force=True)
        assert db.get(Plugin, ids["weather"]).missing_at is None  # 目录恢复后标记自动清除


# ---------- 谁被拦 ----------

def _set(factory, plugin_id: int, **values) -> None:
    with factory() as db:
        plugin = db.get(Plugin, plugin_id)
        for key, value in values.items():
            setattr(plugin, key, value)
        db.commit()


def test_blocked_codes_follow_status_visibility_and_grants(pages) -> None:
    _client, ids = pages
    factory = ids["factory"]
    with factory() as db:
        plan_id = db.scalar(select(SaasPlan.id))
    assert _blocked(factory, ids["alpha"]) == set()  # 默认谁都不拦
    _set(factory, ids["weather"], platform_status="disabled")
    assert "weather" in _blocked(factory, ids["alpha"])
    _set(factory, ids["weather"], platform_status="active", visibility="platform_only")
    assert "weather" in _blocked(factory, ids["alpha"])
    with factory() as db:
        governance.set_visibility(db, db.get(Plugin, ids["weather"]), visibility="plans", plan_ids=[plan_id])
        db.commit()
    assert "weather" not in _blocked(factory, ids["alpha"])  # 甲租户在这个套餐里
    assert "weather" in _blocked(factory, ids["beta"])  # 乙租户没有订阅
    with factory() as db:
        governance.set_visibility(db, db.get(Plugin, ids["weather"]), visibility="tenants", tenant_ids=[ids["beta"]])
        db.commit()
        assert db.scalars(select(PluginGrant.scope_type)).all() == ["tenant"]  # 换范围时旧名单被清掉
    assert "weather" in _blocked(factory, ids["alpha"]) and "weather" not in _blocked(factory, ids["beta"])


def _blocked(factory, tenant_id: int) -> set[str]:
    with factory() as db:
        return set(governance.blocked_plugin_codes(db, tenant_id))


def test_invalid_visibility_settings_are_rejected(pages) -> None:
    _client, ids = pages
    with ids["factory"]() as db:
        weather = db.get(Plugin, ids["weather"])
        for kwargs in (
            {"visibility": "plans"},  # 没选套餐
            {"visibility": "plans", "plan_ids": [9999]},  # 套餐不存在
            {"visibility": "tenants", "tenant_ids": [9999]},
            {"visibility": "nobody"},
        ):
            with pytest.raises(governance.PluginGovernanceError):
                governance.set_visibility(db, weather, **kwargs)
        with pytest.raises(governance.PluginGovernanceError):
            governance.set_platform_status(db, weather, status="paused")


# ---------- 拦在哪些入口 ----------

def test_disabled_plugin_is_skipped_in_dispatch_but_the_binding_survives(pages) -> None:
    _client, ids = pages
    event = {
        "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
        "message": {"content": "天气 北京", "sender_id": "wxid_a", "message_type": "text"},
        "details": {"mentioned_self": True},
    }

    def plan():
        with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
            channel = db.get(Channel, ids["channel_id"])
            return plugin_directory.build_static_dispatch_plan(db, channel, event)

    assert ("weather", "platform_blocked") not in plan().skipped
    _set(ids["factory"], ids["weather"], platform_status="disabled")
    assert ("weather", "platform_blocked") in plan().skipped and "weather" not in plan().candidate_plugin_codes
    with ids["factory"]() as db:
        assert db.get(BotPlugin, ids["binding"]).status == "enabled"  # 平台停用不改动租户的绑定
    _set(ids["factory"], ids["weather"], platform_status="active")
    assert ("weather", "platform_blocked") not in plan().skipped  # 恢复后原样继续


def test_tenant_pages_hide_blocked_plugins_and_refuse_to_bind_them(pages) -> None:
    client, ids = pages
    csrf = _login(client, "admin")
    assert client.get(f"/plugins/{ids['weather']}").status_code == 200
    _set(ids["factory"], ids["weather"], visibility="platform_only")
    assert client.get(f"/plugins/{ids['weather']}").status_code == 404
    assert f'href="/plugins/{ids["weather"]}"' not in client.get("/plugins").text
    refused = client.post("/plugins/bind", data={"csrf_token": csrf, "bot_id": ids["bot"], "plugin_id": ids["weather"], "status": "enabled"})
    assert refused.status_code == 404
    # 已有绑定不能再被打开或手动执行,但可以关掉
    off = client.post("/plugins/toggle-direct", data={"csrf_token": csrf, "bot_id": ids["bot"], "plugin_id": ids["weather"], "enabled": "false"})
    assert off.status_code == 200 and off.json()["is_enabled"] is False
    on = client.post("/plugins/toggle-direct", data={"csrf_token": csrf, "bot_id": ids["bot"], "plugin_id": ids["weather"], "enabled": "true"})
    assert on.status_code == 403
    assert client.post(f"/plugins/{ids['binding']}/toggle", data={"csrf_token": csrf, "enabled": "true"}).status_code == 403
    assert client.post(f"/plugins/{ids['binding']}/execute", json={}, headers={"X-CSRF-Token": csrf}).status_code == 403


# ---------- 平台管理页 ----------

def test_platform_page_requires_platform_auth_csrf_and_writes_audit(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'platform-plugins.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        admin = PlatformUser(username="plugin-admin", display_name="插件管理员", password_hash=hash_password("Plugin-admin-123"), role="platform_admin", is_enabled=True, auth_version=1, must_change_password=False)
        tenant = Tenant(code="gamma", name="丙租户", status="active")
        db.add_all([admin, tenant])
        db.commit()
        db.refresh(admin)
        session = create_platform_session(db, admin)
        tenant_id = int(tenant.id)

    monkeypatch.setattr(platform_plugins_web, "SessionLocal", factory)
    monkeypatch.setattr(platform_operations_web, "SessionLocal", factory)
    test_app = FastAPI()

    @test_app.middleware("http")
    async def restore_platform_principal(request: Request, call_next):
        with factory() as db:
            request.state.platform_principal = load_platform_principal(db, request.cookies.get(PLATFORM_SESSION_COOKIE))
        return await call_next(request)

    test_app.include_router(platform_plugins_web.router)
    with TestClient(test_app, follow_redirects=False) as client:
        assert client.get("/platform/plugins").status_code == 401
        client.cookies.set(PLATFORM_SESSION_COOKIE, session.raw_token)
        client.cookies.set(PLATFORM_CSRF_COOKIE, session.raw_csrf_token)

        page = client.get("/platform/plugins")
        assert page.status_code == 200 and "天气" in page.text and "/platform/plugins/weather/status" in page.text
        primary = page.text.split('<nav class="app-primary-nav"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/platform/plugins" class="nav-link active"' in primary

        forged = client.post("/platform/plugins/weather/status", data={"csrf_token": "forged", "status": "disabled"})
        assert forged.status_code == 403
        with factory() as db:
            assert db.scalar(select(Plugin.platform_status).where(Plugin.code == "weather")) == "active"  # 伪造令牌什么也没改

        ok = client.post("/platform/plugins/weather/status", data={"csrf_token": session.raw_csrf_token, "status": "disabled", "note": "接口异常"})
        assert ok.status_code == 303
        bad = client.post("/platform/plugins/weather/visibility", data={"csrf_token": session.raw_csrf_token, "visibility": "tenants"})
        assert bad.status_code == 303 and "tone=danger" in bad.headers["location"]  # 没选租户:被拒绝并提示
        good = client.post("/platform/plugins/weather/visibility", data={"csrf_token": session.raw_csrf_token, "visibility": "tenants", "tenant_ids": str(tenant_id)})
        assert good.status_code == 303 and "tone=danger" not in good.headers["location"]
        assert client.post("/platform/plugins/ghost/status", data={"csrf_token": session.raw_csrf_token, "status": "active"}).status_code == 404

        with factory() as db:
            weather = db.scalar(select(Plugin).where(Plugin.code == "weather"))
            assert (weather.platform_status, weather.status_note, weather.visibility) == ("disabled", "接口异常", "tenants")
            assert db.scalars(select(PluginGrant.scope_id).where(PluginGrant.plugin_id == weather.id)).all() == [tenant_id]
            actions = db.scalars(select(PlatformAuditLog.action).order_by(PlatformAuditLog.id)).all()
            assert actions == ["plugin.status", "plugin.visibility"]  # 失败的设置不留审计
