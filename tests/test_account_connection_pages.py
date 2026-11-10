from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.models.entities import Bot, Channel
from app.models.saas import BotBinding, LoginProxySession
from test_provider_node_pages import _login, provider_page_context


def test_account_connection_platform_and_wechat_method_pages(provider_page_context) -> None:
    """接入入口先按平台选择，个人微信再进入明确的两种连接方式。"""

    client, _ids = provider_page_context
    _login(client, "admin")

    platform_page = client.get("/channels/connect")
    assert platform_page.status_code == 200
    for label in ("个人微信", "企业微信", "Telegram"):
        assert label in platform_page.text
    assert "bot_account_id" not in platform_page.text
    assert "device_fingerprint" not in platform_page.text

    method_page = client.get("/channels/connect?platform=wechat")
    assert method_page.status_code == 200
    assert "iPad 协议" in method_page.text
    assert "Windows 客户端" in method_page.text


def test_connection_forms_only_show_required_business_fields(provider_page_context) -> None:
    """不同渠道只显示完成连接所需字段，不暴露通用协议配置。"""

    client, _ids = provider_page_context
    _login(client, "admin")

    telegram_page = client.get("/channels/connect?channel_type=telegram")
    assert telegram_page.status_code == 200
    assert 'name="bot_token"' in telegram_page.text
    assert 'name="api_base"' not in telegram_page.text

    workwechat_page = client.get("/channels/connect?channel_type=workwechat")
    assert workwechat_page.status_code == 200
    assert 'name="provider_node_id"' in workwechat_page.text
    assert 'name="provider_account_id"' in workwechat_page.text
    assert "企业微信客户端" in workwechat_page.text

    ipad_page = client.get("/channels/connect?channel_type=wechat_ipad")
    assert ipad_page.status_code == 200
    for label in ("智能选择", "自有代理", "直接登录"):
        assert label in ipad_page.text
    for field_name in (
        "proxy_mode",
        "proxy_province",
        "proxy_city",
        "custom_proxy_host",
        "custom_proxy_port",
        "custom_proxy_username",
        "custom_proxy_password",
    ):
        assert f'name="{field_name}"' in ipad_page.text
    assert 'name="api_base"' not in ipad_page.text
    assert "协议服务地址" not in ipad_page.text
    assert "http://127.0.0.1:9064" not in ipad_page.text
    assert 'name="bot_token"' not in ipad_page.text
    for removed_text in ("Webhook", "私聊策略", "允许名单", "适配配置 JSON"):
        assert removed_text not in ipad_page.text


def test_wechat_ipad_connection_creates_one_temporary_network_session(provider_page_context) -> None:
    """首次接入只创建本次扫码使用的临时网络会话，不把代理写入渠道长期配置。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    started = client.post(
        "/channels/connect/start",
        data={
            "csrf_token": csrf_token,
            "channel_type": "wechat_ipad",
            "display_name": "微信售后账号",
            "proxy_mode": "direct",
        },
    )

    assert started.status_code == 303
    query = parse_qs(urlparse(started.headers["location"]).query)
    channel_id = int(query["channel_id"][0])
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        login_session = db.scalar(
            select(LoginProxySession).where(LoginProxySession.channel_id == channel_id)
        )
        assert channel.adapter_config_json in (None, "")
        assert login_session is not None
        assert login_session.mode == "direct"
        assert login_session.state == "preparing"


def test_workwechat_connection_uses_verified_identity_and_atomic_activation(provider_page_context) -> None:
    """最终启用只使用客户端验证身份，伪造账号和设备字段不能影响商业绑定。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    started = client.post(
        "/channels/connect/start",
        data={
            "csrf_token": csrf_token,
            "channel_type": "workwechat",
            "display_name": "售后企微",
            "provider_node_id": ids["active_node"],
            "provider_account_id": ids["account_a"],
            "bot_account_id": "forged-account",
            "device_fingerprint": "forged-device",
        },
    )

    assert started.status_code == 303
    query = parse_qs(urlparse(started.headers["location"]).query)
    channel_id = int(query["channel_id"][0])
    confirm_page = client.get(started.headers["location"])
    assert confirm_page.status_code == 200
    assert "确认启用账号" in confirm_page.text
    assert "企微甲" in confirm_page.text

    finalized = client.post(
        f"/channels/{channel_id}/connect/finalize",
        data={
            "csrf_token": csrf_token,
            "display_name": "售后企微",
            "bot_account_id": "forged-account",
            "device_fingerprint": "forged-device",
        },
    )
    assert finalized.status_code == 303

    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        bot = db.get(Bot, channel.bot_id)
        binding = db.scalar(select(BotBinding).where(BotBinding.channel_id == channel_id))
        assert channel.is_enabled is True
        assert bot.is_enabled is True
        assert binding.bot_id == bot.id
        assert binding.bot_account_id == "workwechat-account-a"
        assert binding.bot_account_id != "forged-account"
        assert binding.device_fingerprint_hash != "forged-device"


def test_non_admin_cannot_start_account_connection(provider_page_context) -> None:
    """运营和只读角色可以查看接入页，但不能创建连接草稿。"""

    client, _ids = provider_page_context
    csrf_token = _login(client, "operator")
    response = client.post(
        "/channels/connect/start",
        data={
            "csrf_token": csrf_token,
            "channel_type": "telegram",
            "display_name": "越权账号",
            "bot_token": "not-used",
        },
    )
    assert response.status_code == 403


def test_tenant_navigation_uses_business_modules_only(provider_page_context) -> None:
    """企业顶部菜单只保留六个业务模块，不展示内部机器人和执行端概念。"""

    client, _ids = provider_page_context
    _login(client, "admin")
    page = client.get("/channels")
    assert page.status_code == 200
    navigation = page.text.split('<nav class="app-primary-nav"', 1)[1].split("</nav>", 1)[0]
    for label in ("总览", "账号接入", "插件", "消息", "套餐", "企业设置"):
        assert label in navigation
    for removed_label in ("机器人", "Provider", "租户设置", "更多"):
        assert removed_label not in navigation


def test_provider_pages_use_enterprise_wechat_client_copy(provider_page_context) -> None:
    """租户侧执行端页面统一使用企业微信客户端文案。"""

    client, _ids = provider_page_context
    _login(client, "admin")
    page = client.get("/provider-nodes")
    assert page.status_code == 200
    assert "企业微信客户端" in page.text
    assert "Provider 节点" not in page.text


def test_dashboard_hides_operations_diagnostics_from_tenant_users(provider_page_context) -> None:
    """告警摘要、待处理风险、最近日志异常、最近任务状态是运维信息,租户用户处理不了,首页不再展示(平台后台才看这些)。"""

    client, _ids = provider_page_context
    _login(client, "admin")
    page = client.get("/dashboard")
    assert page.status_code == 200
    for hidden in ("告警摘要", "待处理风险", "最近日志异常", "最近任务状态"):
        assert hidden not in page.text
