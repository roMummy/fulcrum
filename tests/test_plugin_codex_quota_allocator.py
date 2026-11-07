from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-codex-quota-allocator"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


@pytest.fixture()
def quota_plugin_fixture(tmp_path: Path):
    """建立可重复追加消息的真实租户、机器人、渠道、会话和插件绑定。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'codex-quota.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="codex-quota", name="额度租户", status="active")
        db.add(tenant)
        db.flush()
        plugin = Plugin(
            code="codex_quota_allocator",
            display_name="Codex 额度管理",
            plugin_type="command",
        )
        db.add(plugin)
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="额度机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="额度 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="codex-quota-group",
            title="额度管理群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(binding.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _append_message(factory, identity: dict[str, int], content: str, *, sender_id: str = "admin-100") -> int:
    """追加一条新的可信入站消息，确保重试同一消息不能冒充二次确认。"""

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        message = Message(
            tenant_id=identity["tenant_id"],
            conversation_id=identity["conversation_id"],
            direction="in",
            sender_id=sender_id,
            sender_name="额度管理员" if sender_id == "admin-100" else "普通成员",
            content=content,
            message_type="text",
            external_message_id=f"quota-message-{time.time_ns()}",
            status="received",
        )
        db.add(message)
        db.commit()
        return int(message.id)


def _config(**overrides) -> dict:
    """返回使用已确认 CPA v0 和 Sub2API v1 契约的租户配置。"""

    config = {
        "enabled": True,
        "admin_user_ids": ["admin-100"],
        "cpa_base_url": "https://cpa.example.test",
        "cpa_management_key": "tenant-cpa-key",
        "cpa_api_version": "v0",
        "cpa_auth_name": "codex-main.json",
        "sub2api_base_url": "https://sub2.example.test",
        "sub2api_admin_api_key": "tenant-sub2-key",
        "sub2api_api_version": "v1",
        "sub2api_group_id": 4,
        "minimum_used_percent": 5,
        "safety_margin_percent": 1,
        "currency_precision": 2,
        "daily_sleep_hours": 8,
        "request_timeout_seconds": 20,
        "subscription_page_size": 100,
        "confirmation_ttl_seconds": 300,
        "query_rate_limit": 2,
        "query_rate_window_seconds": 600,
        "write_rate_limit": 4,
        "write_rate_window_seconds": 600,
        "report_timezone": "Asia/Shanghai",
    }
    config.update(overrides)
    return config


def _request(identity: dict[str, int], message_id: int, *, config: dict | None = None, sender_id: str = "admin-100"):
    """构造带伪造 payload 的请求，验证实现只采信数据库中的消息和身份。"""

    return PluginExecutionRequest(
        action="execute",
        config=config or _config(),
        payload={
            "command_name": "周额重置",
            "sender_id": "forged-admin",
            "group_id": 999,
            "cpa_base_url": "http://127.0.0.1/private",
            "sub2api_group_id": 999,
        },
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=message_id,
            sender_id=sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def _json_response(url: str, payload: dict, *, status_code: int = 200) -> PluginHttpResponse:
    return PluginHttpResponse(
        status_code=status_code,
        headers={"content-type": "application/json; charset=utf-8"},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        url=url,
    )


class FakeQuotaHttpClient:
    """按固定 CPA/Sub2API 契约返回数据，并记录所有潜在写请求。"""

    def __init__(self):
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        normalized_method = method.upper()
        self.calls.append({"method": normalized_method, "url": url, "kwargs": kwargs})
        parsed = urlsplit(url)
        path = parsed.path
        if normalized_method == "GET" and path == "/v0/management/auth-files":
            return _json_response(
                url,
                {
                    "files": [
                        {
                            "name": "codex-main.json",
                            "provider": "codex",
                            "status": "active",
                            "disabled": False,
                            "unavailable": False,
                            "auth_index": "auth-1",
                            "id_token": {
                                "chatgpt_account_id": "account-1",
                                "plan_type": "pro",
                            },
                        }
                    ]
                },
            )
        if normalized_method == "POST" and path == "/v0/management/api-call":
            return _json_response(
                url,
                {
                    "status_code": 200,
                    "body": {
                        "plan_type": "pro",
                        "rate_limit": {
                            "primary_window": {
                                "limit_window_seconds": 604800,
                                "used_percent": 25,
                                "reset_at": 2_000_000_000,
                                "reset_after_seconds": 400000,
                            }
                        },
                    },
                },
            )
        if normalized_method == "GET" and path == "/api/v1/admin/groups/4":
            return _json_response(
                url,
                {
                    "code": 0,
                    "data": {
                        "id": 4,
                        "platform": "openai",
                        "subscription_type": "subscription",
                        "daily_limit_usd": 10,
                        "weekly_limit_usd": 40,
                        "monthly_limit_usd": 100,
                    },
                },
            )
        if normalized_method == "GET" and path == "/api/v1/admin/subscriptions":
            return _json_response(
                url,
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "id": 101,
                                "user_id": 501,
                                "group_id": 4,
                                "status": "active",
                                "weekly_usage_usd": 6,
                                "weekly_window_start": "2026-08-30T00:00:00+08:00",
                                "user": {"email": "alice@example.com"},
                            },
                            {
                                "id": 102,
                                "user_id": 502,
                                "group_id": 4,
                                "status": "active",
                                "weekly_usage_usd": 4,
                                "weekly_window_start": "2026-08-30T00:00:00+08:00",
                                "user": {"email": "bob@example.com"},
                            },
                        ],
                        "page": 1,
                        "pages": 1,
                        "total": 2,
                    },
                },
            )
        if normalized_method == "PUT" and path == "/api/v1/admin/groups/4":
            return _json_response(url, {"code": 0, "data": {"id": 4}})
        if normalized_method == "POST" and path in {
            "/api/v1/admin/subscriptions/101/reset-quota",
            "/api/v1/admin/subscriptions/102/reset-quota",
        }:
            return _json_response(url, {"code": 0, "data": {"reset": True}})
        raise AssertionError(f"未声明的 HTTP 请求: {normalized_method} {url}")


def test_codex_quota_allocator_declares_strict_contract_and_tenant_secrets() -> None:
    """目录只声明三个业务命令，两类管理密钥都必须从租户密文注入。"""

    discovered = get_discovered_plugin("codex_quota_allocator", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.manifest.plugin_type == "command"
    assert discovered.matcher.commands == ("额度查询", "额度调整", "周额重置")
    assert discovered.secret_fields == ("cpa_management_key", "sub2api_admin_api_key")
    assert discovered.runtime_config["cpa_api_version"] == "v0"
    assert discovered.runtime_config["sub2api_api_version"] == "v1"
    assert "cpa_management_key" not in discovered.runtime_config
    assert "sub2api_admin_api_key" not in discovered.runtime_config
    assert "telegram" in discovered.capability.channel_types

    source = (PROJECT_ROOT / "plugins/codex_quota_allocator/plugin.py").read_text(encoding="utf-8")
    assert "GroupBotManager" not in source
    assert "WechatAPIClient" not in source
    assert "redis" not in source.lower()
    assert "legacy" not in source.lower()


def test_query_uses_fixed_routes_trusted_message_and_masks_subscriber_email(
    quota_plugin_fixture,
    monkeypatch,
) -> None:
    """查询只走 CPA v0/Sub2API v1 固定路由，结果不暴露订阅者完整邮箱。"""

    factory, identity = quota_plugin_fixture
    message_id = _append_message(factory, identity, "额度查询", sender_id="member-200")
    loaded = load_directory_plugin("codex_quota_allocator", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_http = FakeQuotaHttpClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_http)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(
            db,
            _request(identity, message_id, sender_id="member-200"),
        )

    assert result.ok is True and result.output["provider_status"] == "completed"
    reply = result.output["replies"][0]["content"]
    assert "CPA 已用：25.00%" in reply
    assert "有效订阅：2 人" in reply
    assert "a***@example.com" in reply and "b***@example.com" in reply
    assert "alice@example.com" not in reply and "bob@example.com" not in reply
    paths = [(call["method"], urlsplit(call["url"]).path) for call in fake_http.calls]
    assert paths == [
        ("GET", "/v0/management/auth-files"),
        ("POST", "/v0/management/api-call"),
        ("GET", "/api/v1/admin/groups/4"),
        ("GET", "/api/v1/admin/subscriptions"),
    ]
    assert all(call["method"] not in {"PUT", "PATCH", "DELETE"} for call in fake_http.calls)
    serialized = json.dumps(fake_http.calls, ensure_ascii=False, default=str)
    assert "forged-admin" not in serialized and "127.0.0.1" not in serialized


def test_adjust_requires_new_message_confirmation_before_exact_group_update(
    quota_plugin_fixture,
    monkeypatch,
) -> None:
    """额度调整第一条消息只保存确认，第二条新消息才按向下截断结果更新分组。"""

    factory, identity = quota_plugin_fixture
    first_message_id = _append_message(factory, identity, "额度调整")
    loaded = load_directory_plugin("codex_quota_allocator", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_http = FakeQuotaHttpClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_http)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = module.execute_message_action(db, _request(identity, first_message_id))
        # 生产消息分发任务会在整次插件执行报告完成后统一提交状态和限流记录。
        db.commit()
    assert first.ok is True and first.output["provider_status"] == "confirmation_required"
    assert not any(call["method"] == "PUT" for call in fake_http.calls)

    # 重放同一任务不能成为第二次确认，防止队列重试意外执行外部写操作。
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        replay = module.execute_message_action(db, _request(identity, first_message_id))
        db.commit()
    assert replay.output["provider_status"] == "confirmation_required"
    assert not any(call["method"] == "PUT" for call in fake_http.calls)

    second_message_id = _append_message(factory, identity, "额度调整")
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        confirmed = module.execute_message_action(db, _request(identity, second_message_id))
        db.commit()

    assert confirmed.ok is True and confirmed.output["provider_status"] == "updated"
    updates = [call for call in fake_http.calls if call["method"] == "PUT"]
    assert len(updates) == 1
    assert urlsplit(updates[0]["url"]).path == "/api/v1/admin/groups/4"
    assert updates[0]["kwargs"]["json_body"] == {
        "daily_limit_usd": 10,
        "weekly_limit_usd": 19.8,
        "monthly_limit_usd": 100,
    }

    # 外部写入后的任务重试不能再次命中原确认；同一消息只会建立一轮新的待确认状态。
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        retried = module.execute_message_action(db, _request(identity, second_message_id))
        db.commit()
    assert retried.output["provider_status"] == "confirmation_required"
    assert len([call for call in fake_http.calls if call["method"] == "PUT"]) == 1


def test_reset_requires_confirmation_and_resets_only_weekly_counters(
    quota_plugin_fixture,
    monkeypatch,
) -> None:
    """周额重置确认后逐个处理当前有效订阅，明确不触碰日/月计数器。"""

    factory, identity = quota_plugin_fixture
    loaded = load_directory_plugin("codex_quota_allocator", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_http = FakeQuotaHttpClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_http)

    first_message_id = _append_message(factory, identity, "周额重置")
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = module.execute_message_action(db, _request(identity, first_message_id))
        db.commit()
    assert first.output["provider_status"] == "confirmation_required"
    assert not any("reset-quota" in call["url"] for call in fake_http.calls)

    second_message_id = _append_message(factory, identity, "周额重置")
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        confirmed = module.execute_message_action(db, _request(identity, second_message_id))
        db.commit()

    assert confirmed.ok is True and confirmed.output["provider_status"] == "reset"
    resets = [call for call in fake_http.calls if "reset-quota" in call["url"]]
    assert [urlsplit(call["url"]).path for call in resets] == [
        "/api/v1/admin/subscriptions/101/reset-quota",
        "/api/v1/admin/subscriptions/102/reset-quota",
    ]
    assert all(
        call["kwargs"]["json_body"] == {"daily": False, "weekly": True, "monthly": False}
        for call in resets
    )


def test_write_requires_configured_admin_and_applies_user_rate_limit(
    quota_plugin_fixture,
    monkeypatch,
) -> None:
    """写命令先校验租户管理员名单，并按真实发送人建立跨进程用户限流。"""

    factory, identity = quota_plugin_fixture
    loaded = load_directory_plugin("codex_quota_allocator", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("权限或限流失败时不应创建 HTTP 客户端")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    member_message_id = _append_message(factory, identity, "额度调整", sender_id="member-200")
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        unconfigured = module.execute_message_action(
            db,
            _request(
                identity,
                member_message_id,
                sender_id="member-200",
                config=_config(admin_user_ids=[]),
            ),
        )
        forbidden = module.execute_message_action(
            db,
            _request(identity, member_message_id, sender_id="member-200"),
        )

    assert unconfigured.output["provider_status"] == "admin_not_configured"
    assert forbidden.output["provider_status"] == "forbidden"

    fake_http = FakeQuotaHttpClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_http)
    first_message_id = _append_message(factory, identity, "额度调整")
    second_message_id = _append_message(factory, identity, "额度调整")
    limited_config = _config(write_rate_limit=1)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = module.execute_message_action(db, _request(identity, first_message_id, config=limited_config))
        db.commit()
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        blocked = module.execute_message_action(db, _request(identity, second_message_id, config=limited_config))
        db.commit()

    assert first.output["provider_status"] == "confirmation_required"
    assert blocked.output["provider_status"] == "rate_limited"
    assert not any(call["method"] == "PUT" for call in fake_http.calls)


def test_unknown_contract_or_untrusted_identity_stops_before_http(
    quota_plugin_fixture,
    monkeypatch,
) -> None:
    """未知接口版本和消息发送人不一致都必须在任何外部请求之前被拒绝。"""

    factory, identity = quota_plugin_fixture
    loaded = load_directory_plugin("codex_quota_allocator", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("前置契约或身份校验失败时不应创建 HTTP 客户端")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    message_id = _append_message(factory, identity, "额度查询", sender_id="member-200")
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        unknown_cpa = module.execute_message_action(
            db,
            _request(
                identity,
                message_id,
                sender_id="member-200",
                config=_config(cpa_api_version="v1"),
            ),
        )
        unknown_sub2 = module.execute_message_action(
            db,
            _request(
                identity,
                message_id,
                sender_id="member-200",
                config=_config(sub2api_api_version="v2"),
            ),
        )
        forged = module.execute_message_action(
            db,
            _request(identity, message_id, sender_id="forged-user"),
        )

    assert unknown_cpa.output["provider_status"] == "contract_unsupported"
    assert unknown_sub2.output["provider_status"] == "contract_unsupported"
    assert forged.output["provider_status"] == "trusted_context_required"
