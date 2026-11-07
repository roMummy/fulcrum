from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-newapi-coupon"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import (
    Bot,
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Message,
    Plugin,
)
from app.models.plugin_business import CouponAccountBinding
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


@pytest.fixture()
def coupon_fixture(tmp_path: Path):
    """建立真实群、活跃成员和 NewAPI 领券插件绑定。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'newapi-coupon.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="newapi-coupon", name="领券租户", status="active")
        db.add(tenant)
        db.flush()
        plugin = Plugin(code="newapi_coupon", display_name="NewAPI 领券", plugin_type="command")
        db.add(plugin)
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="福利机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="福利 Telegram",
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
            external_id="coupon-group-100",
            title="用户福利群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id=conversation.external_id,
            name=conversation.title,
            status="active",
            management_status="managed",
            is_registered=True,
            is_active=True,
        )
        db.add(chatroom)
        db.flush()
        db.add_all(
            [
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="member-100",
                    nickname="已有账号用户",
                    status="active",
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="member-200",
                    nickname="自动开户用户",
                    status="active",
                ),
            ]
        )
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
            "chatroom_id": int(chatroom.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _append_message(
    factory,
    identity: dict[str, int],
    content: str,
    *,
    sender_id: str,
) -> int:
    """追加一条可信入站群消息。"""

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        message = Message(
            tenant_id=identity["tenant_id"],
            conversation_id=identity["conversation_id"],
            direction="in",
            sender_id=sender_id,
            sender_name=sender_id,
            content=content,
            message_type="text",
            external_message_id=f"coupon-message-{time.time_ns()}",
            status="received",
        )
        db.add(message)
        db.commit()
        return int(message.id)


def _config(**overrides) -> dict:
    """返回使用租户自备 NewAPI 和 SMTP 的完整配置。"""

    config = {
        "enabled": True,
        "base_url": "https://newapi.example.test",
        "admin_access_token": "tenant-admin-token",
        "admin_user_id": 1,
        "subscription_plan_id": 7,
        "auto_create_user": False,
        "username_prefix": "mbs",
        "request_timeout_seconds": 20,
        "claim_rate_limit": 3,
        "claim_rate_window_seconds": 3600,
        "notify_mode": "group",
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_username": "",
        "smtp_password": "",
        "smtp_from_email": "",
        "smtp_use_ssl": True,
        "email_subject": "NewAPI 套餐已开通",
    }
    config.update(overrides)
    return config


def _request(
    identity: dict[str, int],
    message_id: int,
    *,
    sender_id: str,
    config: dict | None = None,
) -> PluginExecutionRequest:
    """构造含伪造身份和上游参数的请求，验证实现只读取可信数据库消息。"""

    return PluginExecutionRequest(
        action="execute",
        config=config or _config(),
        payload={
            "sender_id": "forged-user",
            "email": "attacker@example.com",
            "base_url": "http://127.0.0.1/private",
            "subscription_plan_id": 999,
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


def _response(url: str, payload: dict, *, status_code: int = 200, headers: dict | None = None):
    return PluginHttpResponse(
        status_code=status_code,
        headers=headers or {"content-type": "application/json"},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        url=url,
    )


class ExistingUserHttpClient:
    """模拟已有 NewAPI 用户尚未绑定目标套餐的固定接口响应。"""

    def __init__(self):
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        method = method.upper()
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        parsed = urlsplit(url)
        if method == "GET" and parsed.path == "/api/user/search":
            keyword = parse_qs(parsed.query)["keyword"][0]
            users = (
                [{"id": 88, "username": "existing88", "email": "existing@example.com"}]
                if keyword in {"existing@example.com", "mbs"}
                else []
            )
            return _response(url, {"success": True, "message": "ok", "data": {"items": users}})
        if method == "GET" and parsed.path == "/api/subscription/admin/plans":
            return _response(
                url,
                {"success": True, "message": "ok", "data": [{"id": 7, "title": "基础套餐"}]},
            )
        if method == "GET" and parsed.path == "/api/subscription/admin/users/88/subscriptions":
            return _response(url, {"success": True, "message": "ok", "data": []})
        if method == "POST" and parsed.path == "/api/subscription/admin/bind":
            return _response(url, {"success": True, "message": "bound", "data": {"id": 901}})
        raise AssertionError(f"未声明的请求: {method} {url}")


class AutoCreateHttpClient:
    """模拟不存在用户、创建账号、绑定套餐并切换订阅扣费的完整流程。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.created = False
        self.created_username = ""

    def request(self, method: str, url: str, **kwargs):
        method = method.upper()
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        parsed = urlsplit(url)
        if method == "GET" and parsed.path == "/api/user/search":
            users = []
            if self.created:
                users = [{"id": 99, "username": self.created_username, "email": ""}]
            return _response(url, {"success": True, "message": "ok", "data": {"items": users}})
        if method == "POST" and parsed.path == "/api/user/":
            self.created = True
            self.created_username = str(kwargs["json_body"]["username"])
            return _response(url, {"success": True, "message": "created", "data": None})
        if method == "GET" and parsed.path == "/api/subscription/admin/plans":
            return _response(
                url,
                {"success": True, "message": "ok", "data": [{"id": 7, "title": "基础套餐"}]},
            )
        if method == "GET" and parsed.path == "/api/subscription/admin/users/99/subscriptions":
            return _response(url, {"success": True, "message": "ok", "data": []})
        if method == "POST" and parsed.path == "/api/subscription/admin/bind":
            return _response(url, {"success": True, "message": "bound", "data": {"id": 902}})
        if method == "POST" and parsed.path == "/api/user/login":
            return _response(
                url,
                {"success": True, "message": "login", "data": {"id": 99}},
                headers={"content-type": "application/json", "set-cookie": "session=secure-cookie; Path=/; HttpOnly"},
            )
        if method == "PUT" and parsed.path == "/api/subscription/self/preference":
            return _response(url, {"success": True, "message": "updated", "data": None})
        raise AssertionError(f"未声明的请求: {method} {url}")


def test_newapi_coupon_declares_single_command_tenant_secrets_and_business_model() -> None:
    """插件只保留一个领券入口，管理员令牌和 SMTP 密码进入租户密文。"""

    discovered = get_discovered_plugin("newapi_coupon", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.matcher.commands == ("领券",)
    assert discovered.secret_fields == ("admin_access_token", "smtp_password")
    assert "admin_access_token" not in discovered.runtime_config
    assert "smtp_password" not in discovered.runtime_config
    assert "telegram" in discovered.capability.channel_types

    constraints = {item.name for item in CouponAccountBinding.__table__.constraints if item.name}
    assert "uq_coupon_binding_tenant_channel_member" in constraints
    assert "uq_coupon_binding_tenant_email" in constraints
    assert "uq_coupon_binding_tenant_newapi_user" in constraints

    source = (PROJECT_ROOT / "plugins/newapi_coupon/plugin.py").read_text(encoding="utf-8")
    assert "WechatAPIClient" not in source
    assert "GroupBotManager" not in source
    assert "redis" not in source.lower()
    assert "legacy" not in source.lower()


def test_existing_user_claim_binds_once_and_repeated_claim_does_not_call_newapi(
    coupon_fixture,
    monkeypatch,
) -> None:
    """已有账号首次绑定套餐并落库，后续同用户同邮箱只能读取幂等结果。"""

    factory, identity = coupon_fixture
    message_id = _append_message(
        factory,
        identity,
        "领券 existing@example.com",
        sender_id="member-100",
    )
    loaded = load_directory_plugin("newapi_coupon", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_http = ExistingUserHttpClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_http)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = module.execute_message_action(
            db,
            _request(identity, message_id, sender_id="member-100"),
        )
        db.commit()

    assert first.ok is True and first.output["provider_status"] == "issued"
    assert "existing88" in first.output["replies"][0]["content"]
    assert "existing@example.com" not in first.output["replies"][0]["content"]
    binds = [call for call in fake_http.calls if urlsplit(call["url"]).path == "/api/subscription/admin/bind"]
    assert len(binds) == 1
    assert binds[0]["kwargs"]["json_body"] == {"user_id": 88, "plan_id": 7}

    repeated_message_id = _append_message(
        factory,
        identity,
        "领券 existing@example.com",
        sender_id="member-100",
    )

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("已完成绑定的重复领取不应访问 NewAPI")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        repeated = module.execute_message_action(
            db,
            _request(identity, repeated_message_id, sender_id="member-100"),
        )
        binding = db.scalar(select(CouponAccountBinding))

    assert repeated.ok is True and repeated.output["provider_status"] == "already_issued"
    assert binding is not None and binding.status == "active"
    assert binding.newapi_user_id == 88 and binding.email_normalized == "existing@example.com"


def test_auto_create_delivers_password_only_by_tenant_smtp(
    coupon_fixture,
    monkeypatch,
) -> None:
    """自动开户的初始密码只进入租户邮件发送器，不进入群回复、结果或数据库。"""

    factory, identity = coupon_fixture
    message_id = _append_message(
        factory,
        identity,
        "领券 created@example.com",
        sender_id="member-200",
    )
    loaded = load_directory_plugin("newapi_coupon", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_http = AutoCreateHttpClient()
    delivered: dict[str, str] = {}
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_http)

    def fake_email_sender(config, *, recipient, username, password, plan_title):
        delivered.update(
            {
                "recipient": recipient,
                "username": username,
                "password": password,
                "plan_title": plan_title,
                "smtp_password": config.smtp_password,
            }
        )

    monkeypatch.setattr(module, "_send_account_email", fake_email_sender)
    config = _config(
        auto_create_user=True,
        notify_mode="email",
        smtp_host="smtp.example.test",
        smtp_username="mailer@example.test",
        smtp_password="tenant-smtp-secret",
        smtp_from_email="mailer@example.test",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(
            db,
            _request(identity, message_id, sender_id="member-200", config=config),
        )
        db.commit()
        binding = db.scalar(select(CouponAccountBinding))

    assert result.ok is True and result.output["provider_status"] == "issued"
    assert delivered["recipient"] == "created@example.com"
    assert delivered["username"].startswith("mbs")
    assert len(delivered["password"]) >= 12
    assert delivered["smtp_password"] == "tenant-smtp-secret"
    serialized_result = json.dumps(result.output, ensure_ascii=False)
    assert delivered["password"] not in serialized_result
    assert "tenant-smtp-secret" not in serialized_result
    assert binding is not None and binding.status == "active"
    assert binding.notification_status == "sent"
    assert delivered["password"] not in " ".join(str(value or "") for value in binding.__dict__.values())

    create_call = next(call for call in fake_http.calls if urlsplit(call["url"]).path == "/api/user/")
    assert create_call["kwargs"]["json_body"]["password"] == delivered["password"]
    preference_call = next(
        call for call in fake_http.calls if urlsplit(call["url"]).path == "/api/subscription/self/preference"
    )
    assert preference_call["kwargs"]["headers"]["Cookie"] == "session=secure-cookie"


def test_member_and_email_binding_are_immutable_without_external_calls(
    coupon_fixture,
    monkeypatch,
) -> None:
    """同一渠道用户不能换邮箱，同一邮箱也不能被另一个渠道用户重复领取。"""

    factory, identity = coupon_fixture
    loaded = load_directory_plugin("newapi_coupon", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        db.add(
            CouponAccountBinding(
                tenant_id=identity["tenant_id"],
                bot_plugin_id=identity["binding_id"],
                channel_id=identity["channel_id"],
                conversation_id=identity["conversation_id"],
                member_external_id="member-100",
                email_normalized="owner@example.com",
                newapi_user_id=501,
                newapi_username="owner501",
                plan_id=7,
                plan_title="基础套餐",
                status="active",
                notification_status="not_required",
            )
        )
        db.commit()

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("绑定冲突必须在创建 HTTP 客户端之前返回")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    changed_email_message = _append_message(
        factory,
        identity,
        "领券 changed@example.com",
        sender_id="member-100",
    )
    occupied_email_message = _append_message(
        factory,
        identity,
        "领券 owner@example.com",
        sender_id="member-200",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        changed = module.execute_message_action(
            db,
            _request(identity, changed_email_message, sender_id="member-100"),
        )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        occupied = module.execute_message_action(
            db,
            _request(identity, occupied_email_message, sender_id="member-200"),
        )

    assert changed.output["provider_status"] == "member_already_bound"
    assert occupied.output["provider_status"] == "email_already_bound"


def test_untrusted_member_or_incomplete_auto_create_config_stops_before_http(
    coupon_fixture,
    monkeypatch,
) -> None:
    """非活跃群成员和缺少租户 SMTP 的自动开户都不能访问 NewAPI。"""

    factory, identity = coupon_fixture
    loaded = load_directory_plugin("newapi_coupon", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("可信成员或租户配置校验失败时不应创建 HTTP 客户端")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    outsider_message = _append_message(
        factory,
        identity,
        "领券 outsider@example.com",
        sender_id="outsider-999",
    )
    member_message = _append_message(
        factory,
        identity,
        "领券 created@example.com",
        sender_id="member-200",
    )
    invalid_email_message = _append_message(
        factory,
        identity,
        "领券 invalid-email",
        sender_id="member-100",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        outsider = module.execute_message_action(
            db,
            _request(identity, outsider_message, sender_id="outsider-999"),
        )
        missing_smtp = module.execute_message_action(
            db,
            _request(
                identity,
                member_message,
                sender_id="member-200",
                config=_config(auto_create_user=True, notify_mode="email"),
            ),
        )
        invalid_email = module.execute_message_action(
            db,
            _request(identity, invalid_email_message, sender_id="member-100"),
        )

    assert outsider.output["provider_status"] == "group_member_required"
    assert missing_smtp.output["provider_status"] == "not_configured"
    assert invalid_email.output["provider_status"] == "email_invalid"
