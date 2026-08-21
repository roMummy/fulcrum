from __future__ import annotations

from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.models.entities import Channel
from test_provider_node_pages import _login, provider_page_context


def _channel_form(ids: dict, csrf_token: str, **overrides) -> dict[str, object]:
    """构造渠道页面的最小提交数据，测试只覆盖统一渠道表单的公共契约。"""

    payload: dict[str, object] = {
        "bot_id": ids["bot"],
        "name": "测试渠道",
        "channel_type": "wechat_ipad",
        "api_base": "",
        "bot_token": "",
        "webhook_path": "",
        "dm_policy": "allowlist",
        "allow_from": "",
        "adapter_config_json": "",
        "proxy_host": "",
        "proxy_port": "",
        "proxy_username": "",
        "proxy_password": "",
        "provider_node_id": 0,
        "provider_account_id": 0,
        "csrf_token": csrf_token,
        "is_enabled": "false",
    }
    payload.update(overrides)
    return payload


def test_channel_page_lists_all_formal_channel_types(provider_page_context) -> None:
    """租户创建入口必须直接展示统一目录中的四种正式渠道。"""

    client, _ids = provider_page_context
    _login(client, "admin")

    response = client.get("/channels")

    assert response.status_code == 200
    assert 'select name="channel_type"' in response.text
    for channel_type, label in (
        ("workwechat", "企业微信 Hook"),
        ("wechat_ipad", "微信 iPad 协议"),
        ("wechat_winauto", "微信 Windows"),
        ("telegram", "Telegram"),
    ):
        assert f'value="{channel_type}"' in response.text
        assert label in response.text


@pytest.mark.parametrize("channel_type", ("wechat_ipad", "wechat_winauto", "telegram"))
def test_direct_channel_does_not_persist_submitted_executor_binding(provider_page_context, channel_type: str) -> None:
    """非远端执行渠道即使收到伪造节点主键，也必须丢弃这些不属于该渠道的字段。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    response = client.post(
        "/channels",
        data=_channel_form(
            ids,
            csrf_token,
            name=f"{channel_type}-草稿",
            channel_type=channel_type,
            provider_node_id=ids["active_node"],
            provider_account_id=ids["account_a"],
        ),
    )

    assert response.status_code == 303
    with ids["factory"]() as db:
        channel = db.scalar(select(Channel).where(Channel.name == f"{channel_type}-草稿"))
        assert channel is not None
        assert channel.provider_node_id is None
        assert channel.provider_account_id is None


@pytest.mark.parametrize(
    ("channel_type", "missing_label"),
    (
        ("wechat_ipad", "API Base"),
        ("wechat_winauto", "API Base"),
        ("telegram", "Bot Token"),
    ),
)
def test_enabled_direct_channel_requires_catalog_configuration(
    provider_page_context,
    channel_type: str,
    missing_label: str,
) -> None:
    """正式渠道启用前必须满足目录声明的必填配置，停用草稿不受此限制。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    response = client.post(
        "/channels",
        data=_channel_form(
            ids,
            csrf_token,
            name=f"{channel_type}-缺配置",
            channel_type=channel_type,
            is_enabled="true",
        ),
    )

    assert response.status_code == 303
    assert missing_label in unquote(response.headers["location"])
    with ids["factory"]() as db:
        assert db.scalar(select(Channel).where(Channel.name == f"{channel_type}-缺配置")) is None


@pytest.mark.parametrize(
    ("channel_type", "config"),
    (
        ("wechat_ipad", {"api_base": "http://127.0.0.1:8848"}),
        ("wechat_winauto", {"api_base": "http://127.0.0.1:8182"}),
        ("telegram", {"bot_token": "telegram-test-token"}),
    ),
)
def test_tenant_admin_can_create_enabled_direct_channel(
    provider_page_context,
    channel_type: str,
    config: dict[str, str],
) -> None:
    """提供目录要求的最小配置后，租户管理员可以启用不依赖执行端的渠道。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    response = client.post(
        "/channels",
        data=_channel_form(
            ids,
            csrf_token,
            name=f"{channel_type}-启用",
            channel_type=channel_type,
            is_enabled="true",
            **config,
        ),
    )

    assert response.status_code == 303
    with ids["factory"]() as db:
        channel = db.scalar(select(Channel).where(Channel.name == f"{channel_type}-启用"))
        assert channel is not None
        assert channel.channel_type == channel_type
        assert channel.is_enabled is True


def test_channel_type_is_immutable_after_creation(provider_page_context) -> None:
    """编辑请求篡改渠道类型时必须拒绝，原记录不能被转换成另一种协议。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    created = client.post(
        "/channels",
        data=_channel_form(ids, csrf_token, name="不可换型渠道", channel_type="wechat_ipad"),
    )
    assert created.status_code == 303
    with ids["factory"]() as db:
        channel_id = int(db.scalar(select(Channel.id).where(Channel.name == "不可换型渠道")))

    edited = client.post(
        f"/channels/{channel_id}/edit",
        data=_channel_form(ids, csrf_token, name="尝试换型", channel_type="telegram"),
    )

    assert edited.status_code == 303
    assert "渠道类型不可修改" in unquote(edited.headers["location"])
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.channel_type == "wechat_ipad"
        assert channel.name == "不可换型渠道"


def test_blank_token_keeps_existing_encrypted_secret_on_edit(provider_page_context) -> None:
    """敏感字段留空表示不轮换密钥，启用校验必须识别数据库中已有的密文。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    created = client.post(
        "/channels",
        data=_channel_form(
            ids,
            csrf_token,
            name="Telegram 主渠道",
            channel_type="telegram",
            bot_token="telegram-existing-token",
            is_enabled="true",
        ),
    )
    assert created.status_code == 303
    with ids["factory"]() as db:
        channel = db.scalar(select(Channel).where(Channel.name == "Telegram 主渠道"))
        channel_id = int(channel.id)
        encrypted_before = channel.bot_token_encrypted

    detail = client.get(f"/channels/{channel_id}")
    assert detail.status_code == 200
    assert 'name="channel_type" value="telegram"' in detail.text
    assert 'name="provider_node_id"' not in detail.text

    edited = client.post(
        f"/channels/{channel_id}/edit",
        data=_channel_form(
            ids,
            csrf_token,
            name="Telegram 业务渠道",
            channel_type="telegram",
            bot_token="",
            is_enabled="true",
        ),
    )

    assert edited.status_code == 303
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.name == "Telegram 业务渠道"
        assert channel.bot_token_encrypted == encrypted_before


def test_incomplete_draft_cannot_be_enabled_by_toggle(provider_page_context) -> None:
    """停用草稿允许缺配置，但独立启用动作同样必须执行目录校验。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    created = client.post(
        "/channels",
        data=_channel_form(ids, csrf_token, name="待配置微信", channel_type="wechat_ipad"),
    )
    assert created.status_code == 303
    with ids["factory"]() as db:
        channel_id = int(db.scalar(select(Channel.id).where(Channel.name == "待配置微信")))

    toggled = client.post(f"/channels/{channel_id}/toggle")

    assert toggled.status_code == 303
    assert "API Base" in unquote(toggled.headers["location"])
    with ids["factory"]() as db:
        assert db.get(Channel, channel_id).is_enabled is False
