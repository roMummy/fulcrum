"""微桥接入的真实 HTTP 流程及渠道凭据隔离验证。"""

from __future__ import annotations

import json
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

import app.api.wechat_webridge as bridge_api
from app.core.settings import settings
from app.core.logging import ContextFormatter
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel, TaskJob
from app.models.saas import BotBinding
from app.services.bot_entitlements import get_bot_quota
from app.services.wechat_webridge import service
from app.services.wechat_webridge.access import connection_url, initialize_connection
from test_provider_node_pages import _login, provider_page_context


IDENTITY = {
    "device_id": "test-bridge-device",
    "session_id": "test-bridge-session",
    "account_id": "wxid_bridge_test",
    "runtime_version": "8.0.80-6",
    "state": {"is_logged_in": True},
}
PUBLIC_BASE = "http://192.0.2.10:8280"


@pytest.fixture(autouse=True)
def isolate_bridge_runtime(monkeypatch):
    # 临时端口避免与正在运行的开发服务竞争；各用例独立维护轮询节流状态。
    monkeypatch.setattr(settings, "local_proxy_relay_port", 0)
    monkeypatch.setattr(settings, "public_base_url", PUBLIC_BASE)
    monkeypatch.setattr(service, "_last_poll_maintenance", {})
    monkeypatch.setattr(bridge_api, "POLL_HOLD_SECONDS", 0)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


@pytest.fixture()
def bridge_context(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    monkeypatch.setattr(bridge_api, "SessionLocal", ids["factory"])
    monkeypatch.setattr(bridge_api, "TenantSessionLocal", ids["factory"])
    csrf = _login(client, "admin")
    return client, ids, csrf


def _start(context, name="测试微桥"):
    client, ids, csrf = context
    response = client.post(
        "/channels/connect/start",
        data={"csrf_token": csrf, "channel_type": "wechat_webridge", "display_name": name},
    )
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    channel_id = int(query["channel_id"][0])
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        address = connection_url(channel)
    return channel_id, urlsplit(address).path


def _register(client, address, identity=None):
    response = client.post(address + "/agent/register", json=identity or IDENTITY)
    assert response.status_code == 200
    return response


def _confirm(context, channel_id, account_id=IDENTITY["account_id"]):
    client, _ids, csrf = context
    return client.post(
        f"/channels/{channel_id}/connect/finalize",
        data={"csrf_token": csrf, "expected_account_id": account_id, "display_name": "测试微信"},
    )


def _poll(client, address):
    response = client.post(address + "/agent/poll", json=IDENTITY)
    assert response.status_code == 200
    commands = response.json()["commands"]
    # 测试关闭挂起等待后，首次轮询新入队的状态任务可能要到下一轮才满足 available_at。
    if not commands:
        response = client.post(address + "/agent/poll", json=IDENTITY)
        assert response.status_code == 200
        commands = response.json()["commands"]
    return commands


def _assert_unbound(ids, channel_id):
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.is_enabled is False
        assert db.get(Bot, channel.bot_id).is_enabled is False
        assert db.scalar(select(BotBinding).where(BotBinding.channel_id == channel_id)) is None


def test_generates_stable_private_address_and_copy_control(bridge_context):
    """用户只输入账号名称，专属地址由服务端生成并在刷新后保持不变。"""

    client, ids, csrf = bridge_context
    page = client.get("/channels/connect?channel_type=wechat_webridge")
    assert page.status_code == 200
    assert 'name="api_base"' not in page.text
    assert 'name="adapter_config_json"' not in page.text
    assert "生成长轮询地址" in page.text
    channel_id, address = _start(bridge_context)
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        token = address.rsplit("/", 1)[1]
        assert len(token) >= 32
        assert channel.api_base == f"{PUBLIC_BASE}/api/wechat-webridge/{channel_id}"
        assert token not in channel.runtime_info_json
        assert not channel.adapter_config_json
    first = client.get(f"/channels/connect?channel_id={channel_id}")
    refreshed = client.get(f"/channels/connect?channel_id={channel_id}")
    assert first.status_code == refreshed.status_code == 200
    assert bool(PUBLIC_BASE + address in first.text)
    assert bool(PUBLIC_BASE + address in refreshed.text)
    assert 'id="webridge-connection-url"' in first.text and "readonly" in first.text
    assert "data-copy-address" in first.text and "复制地址" in first.text
    detail = client.get(f"/channels/{channel_id}")
    assert detail.status_code == 200
    assert 'name="api_base"' not in detail.text and "继续接入" in detail.text
    updated = client.post(
        f"/channels/{channel_id}/edit",
        data={"csrf_token": csrf, "name": "微桥新名称", "channel_type": "wechat_webridge", "api_base": "https://attacker.example/replace"},
    )
    assert updated.status_code == 303
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.name == "微桥新名称"
        assert bool(connection_url(channel) == PUBLIC_BASE + address)
        assert channel.api_base == f"{PUBLIC_BASE}/api/wechat-webridge/{channel_id}"
    _assert_unbound(ids, channel_id)


def test_all_phone_endpoints_reject_missing_wrong_and_other_channel_tokens(bridge_context):
    """凭据必须校验在注册、心跳、事件、命令和两种上传的共同入口。"""

    client, _ids, _csrf = bridge_context
    first_id, first_address = _start(bridge_context, "微桥甲")
    second_id, second_address = _start(bridge_context, "微桥乙")
    assert first_address.rsplit("/", 1)[1] != second_address.rsplit("/", 1)[1]
    crossed = f"/api/wechat-webridge/{second_id}/" + first_address.rsplit("/", 1)[1]
    for base, status in [
        (f"/api/wechat-webridge/{first_id}", 404),
        (f"/api/wechat-webridge/{first_id}/invalid-token", 403),
        (crossed, 403),
    ]:
        for endpoint in ("register", "state", "event", "poll", "command_result", "upload_media", "moments_upload"):
            url = base + "/agent/" + endpoint
            if endpoint == "upload_media":
                response = client.post(url, params={"attachment_id": 1}, data={k: IDENTITY[k] for k in ("device_id", "session_id", "account_id")}, files={"file": ("test.txt", b"test", "text/plain")})
            elif endpoint == "moments_upload":
                response = client.post(url, params={"media_id": 1}, content=b"test")
            else:
                response = client.post(url, json=IDENTITY)
            assert response.status_code == status, endpoint


def test_registration_displays_account_and_profile_without_activating(bridge_context):
    """真实注册和 account_status 回报驱动网页资料，但最终启用留给用户确认。"""

    client, ids, _csrf = bridge_context
    channel_id, address = _start(bridge_context)
    registered = _register(client, address)
    assert registered.json()["connection_state"] == "verified"
    status = client.get(f"/channels/{channel_id}/connect/status")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["account"]["account_id"] == IDENTITY["account_id"]
    assert status.json()["account"]["runtime_version"] == IDENTITY["runtime_version"]
    assert address.rsplit("/", 1)[1] not in status.text
    commands = _poll(client, address)
    account_command = next(c for c in commands if c["command_name"] == "account_status")
    completed = client.post(
        address + "/agent/command_result",
        json={**IDENTITY, "request_id": account_command["request_id"], "ok": True, "result": {"ok": True, "result": {"account_id": IDENTITY["account_id"], "nick_name": "测试微信昵称", "avatar_url": "https://example.org/avatar.png"}}},
    )
    assert completed.status_code == 200
    status = client.get(f"/channels/{channel_id}/connect/status")
    assert status.json()["account"]["nickname"] == "测试微信昵称"
    assert status.json()["account"]["avatar_url"] == "https://example.org/avatar.png"
    page = client.get(f"/channels/connect?channel_id={channel_id}")
    assert "测试微信昵称" in page.text and "https://example.org/avatar.png" in page.text
    assert "确认信息并接入" in page.text
    _assert_unbound(ids, channel_id)


def test_pending_connection_only_receives_status_commands(bridge_context):
    """确认前不下发业务命令、不处理业务事件，确认后才释放正常任务。"""

    client, ids, _csrf = bridge_context
    channel_id, address = _start(bridge_context)
    _register(client, address)
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        job = service.enqueue_command(db, db.get(Channel, channel_id), command_name="friends", args={})
        job_id = int(job.id)
        db.commit()
    commands = _poll(client, address)
    assert commands
    assert {c["command_name"] for c in commands} <= {"account_status", "device_status"}
    with ids["factory"]() as db:
        assert db.get(TaskJob, job_id).status == "pending"
    ignored = client.post(address + "/agent/event", json={**IDENTITY, "event_name": "message"})
    assert ignored.status_code == 200 and ignored.json()["ignored"] is True
    assert _confirm(bridge_context, channel_id).status_code == 303
    commands = client.post(address + "/agent/poll", json=IDENTITY).json()["commands"]
    assert any(c["command_name"] == "friends" for c in commands)


def test_confirmation_checks_identity_and_csrf_before_binding(bridge_context):
    """确认账号必须与页面展示一致，不能绕过 CSRF 或提前占用机器人额度。"""

    client, ids, _csrf = bridge_context
    channel_id, address = _start(bridge_context)
    _register(client, address)
    bypass = client.post(f"/channels/{channel_id}/toggle", data={"csrf_token": _csrf})
    assert bypass.status_code == 303
    assert bypass.headers["location"] == f"/channels/connect?channel_id={channel_id}"
    _assert_unbound(ids, channel_id)
    rejected = client.post(
        f"/channels/{channel_id}/connect/finalize",
        data={"expected_account_id": IDENTITY["account_id"], "display_name": "测试微信"},
    )
    assert rejected.status_code == 403
    for account_id in ("", "wxid_another_account"):
        rejected = _confirm(bridge_context, channel_id, account_id)
        assert rejected.status_code == 303
        assert parse_qs(urlsplit(rejected.headers["location"]).query)["tone"] == ["danger"]
        _assert_unbound(ids, channel_id)
    confirmed = _confirm(bridge_context, channel_id)
    assert confirmed.status_code == 303
    assert urlsplit(confirmed.headers["location"]).path == f"/channels/{channel_id}"
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.is_enabled and db.get(Bot, channel.bot_id).is_enabled
        binding = db.scalar(select(BotBinding).where(BotBinding.channel_id == channel_id))
        assert binding is not None and binding.bot_account_id == IDENTITY["account_id"]


def test_connected_channel_cannot_be_replaced_by_another_account_or_device(bridge_context):
    """持有同一连接地址也不能把已有渠道覆盖成其他微信或另一台手机。"""

    client, ids, _csrf = bridge_context
    channel_id, address = _start(bridge_context)
    _register(client, address)
    _confirm(bridge_context, channel_id)
    for changed in ({"account_id": "wxid_other"}, {"device_id": "other-device"}):
        response = client.post(address + "/agent/register", json={**IDENTITY, **changed})
        assert response.status_code == 400
        event = client.post(address + "/agent/event", json={**IDENTITY, **changed, "event_name": "message"})
        assert event.status_code == 409
    bypass = client.post(f"/webhooks/{channel_id}/inbound", json={**IDENTITY, "event_name": "message"})
    assert bypass.status_code == 403
    # 微信重启可能产生新会话，同一账号和设备可以正常重新注册。
    response = _register(client, address, {**IDENTITY, "session_id": "test-session-after-restart"})
    assert response.json()["connection_state"] == "active"
    with ids["factory"]() as db:
        runtime = json.loads(db.get(Channel, channel_id).runtime_info_json)
        assert runtime["webridge"]["account_id"] == IDENTITY["account_id"]
        assert runtime["webridge"]["device_id"] == IDENTITY["device_id"]


def test_deleted_registered_draft_cannot_be_confirmed_from_old_page(bridge_context):
    """网页删除已注册草稿后，旧确认页提交不能复活账号或占用额度。"""

    client, ids, csrf = bridge_context
    channel_id, address = _start(bridge_context)
    assert _register(client, address).json()["connection_state"] == "verified"
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        used_before = get_bot_quota(db).used
    deleted = client.post(f"/channels/{channel_id}/delete", data={"csrf_token": csrf})
    assert deleted.status_code == 303
    assert urlsplit(deleted.headers["location"]).path == "/channels"
    rejected = _confirm(bridge_context, channel_id)
    assert rejected.status_code == 303
    assert parse_qs(urlsplit(rejected.headers["location"]).query)["tone"] == ["danger"]
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        channel = db.get(Channel, channel_id)
        bot = db.get(Bot, channel.bot_id)
        assert channel.deleted_at is not None and bot.deleted_at is not None
        assert channel.is_enabled is False and bot.is_enabled is False
        assert get_bot_quota(db).used == used_before
        assert db.scalar(select(BotBinding).where(BotBinding.channel_id == channel_id)) is None


def test_connection_progress_is_private_to_owning_tenant_admin(bridge_context):
    """跨企业进度不可见，普通运营也不能读取管理员专属的连接信息。"""

    client, ids, _csrf = bridge_context
    own_id, address = _start(bridge_context)
    with ids["factory"]() as db:
        bot = Bot(tenant_id=ids["beta"], name="乙机器人", is_enabled=False)
        db.add(bot)
        db.flush()
        channel = Channel(tenant_id=ids["beta"], bot_id=bot.id, name="乙微桥", channel_type="wechat_webridge", is_enabled=False)
        db.add(channel)
        db.flush()
        initialize_connection(channel, PUBLIC_BASE)
        db.commit()
        other_id = channel.id
    assert client.get(f"/channels/{other_id}/connect/status").status_code == 404
    assert client.get(f"/channels/connect?channel_id={other_id}").status_code == 404
    _login(client, "operator")
    assert client.get(f"/channels/{own_id}/connect/status").status_code == 403
    assert client.get(f"/channels/connect?channel_id={own_id}").status_code == 403
    detail = client.get(f"/channels/{own_id}")
    assert detail.status_code == 200
    assert "继续接入" not in detail.text and "查看连接地址" not in detail.text
    assert address.rsplit("/", 1)[1] not in detail.text


def test_logs_hide_connection_token_with_and_without_agent_suffix():
    """手机请求和误打开基础地址的日志都脱敏，既有无 Token 路径仍可排错。"""

    token = secrets.token_urlsafe(32)
    formatter = ContextFormatter("%(message)s")
    prefix = "/api/wechat-webridge/42/"
    for suffix in ("/agent/register", "", "?debug=1"):
        record = logging.LogRecord("test.webridge", logging.INFO, __file__, 1, "request %s", (PUBLIC_BASE + prefix + token + suffix,), None)
        record.path = prefix + token + suffix
        rendered = formatter.format(record)
        assert token not in rendered
        assert "[已隐藏]" in rendered
    for legacy_path in (prefix + "agent/register", prefix + "commands"):
        record = logging.LogRecord("test.webridge", logging.INFO, __file__, 1, "request %s", (legacy_path,), None)
        rendered = formatter.format(record)
        assert legacy_path in rendered
        assert "[已隐藏]" not in rendered
