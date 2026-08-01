from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel
from app.schemas.forms import ChannelCreate, ChannelUpdate
from app.services.open_api_apps import OpenApiAppInput, upsert_open_api_app
from test_provider_node_pages import _login, provider_page_context


@pytest.mark.parametrize(
    "channel_type",
    ["wechat_ipad", "wechat_winauto", "telegram", "openclaw", "simulator"],
)
def test_tenant_cannot_create_non_workwechat_channel(provider_page_context, channel_type: str) -> None:
    """租户表单即使被手工篡改，也不能创建首期范围外的渠道。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    response = client.post(
        "/channels",
        data={
            "bot_id": ids["bot"],
            "name": f"非法渠道-{channel_type}",
            "channel_type": channel_type,
            "csrf_token": csrf_token,
            "provider_node_id": 0,
            "provider_account_id": 0,
        },
    )

    assert response.status_code == 400
    with ids["factory"]() as db:
        count = db.scalar(select(func.count(Channel.id)).where(Channel.channel_type == channel_type))
        assert int(count or 0) == 0


@pytest.mark.parametrize("schema_type", [ChannelCreate, ChannelUpdate])
def test_channel_schema_rejects_non_workwechat_type(schema_type) -> None:
    """服务层输入模型作为第二道边界，阻止后台脚本绕过页面写入旧渠道类型。"""

    with pytest.raises(ValidationError, match="channel_type_not_enabled"):
        schema_type(bot_id=1, name="非法渠道", channel_type="telegram")


def test_channel_pages_only_render_workwechat_controls(provider_page_context) -> None:
    """租户页面只展示企业微信类型和 Provider 绑定字段，不再暴露旧渠道配置入口。"""

    client, ids = provider_page_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        legacy_channel = Channel(
            bot_id=ids["bot"],
            name="页面不可见的历史旧渠道",
            channel_type="telegram",
            is_enabled=True,
            status="online",
        )
        db.add(legacy_channel)
        db.commit()
        legacy_channel_id = int(legacy_channel.id)
    csrf_token = _login(client, "admin")
    created = client.post(
        "/channels",
        data={
            "bot_id": ids["bot"],
            "name": "唯一企微渠道",
            "channel_type": "workwechat",
            "provider_node_id": ids["active_node"],
            "provider_account_id": ids["account_a"],
            "csrf_token": csrf_token,
            "is_enabled": "true",
        },
    )
    assert created.status_code == 303

    list_page = client.get("/channels")
    assert list_page.status_code == 200
    assert 'name="channel_type" value="workwechat"' in list_page.text
    for disabled_type in ("wechat_ipad", "wechat_winauto", "telegram", "openclaw", "simulator"):
        assert f'value="{disabled_type}"' not in list_page.text
    assert "页面不可见的历史旧渠道" not in list_page.text
    assert "Wechat iPad 可选代理" not in list_page.text
    assert client.get(f"/channels/{legacy_channel_id}").status_code == 404

    with ids["factory"]() as db:
        channel = db.scalar(select(Channel).where(Channel.name == "唯一企微渠道"))
        channel_id = int(channel.id)
    detail_page = client.get(f"/channels/{channel_id}")
    assert detail_page.status_code == 200
    assert 'name="channel_type" value="workwechat"' in detail_page.text
    assert "Wechat iPad 代理" not in detail_page.text
    assert "刷新群列表" not in detail_page.text


def test_open_api_app_rejects_non_workwechat_channel(provider_page_context) -> None:
    """开放接口应用不能把历史旧渠道重新暴露给外部业务系统。"""

    _client, ids = provider_page_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        legacy_channel = Channel(
            bot_id=ids["bot"],
            name="历史 Telegram 渠道",
            channel_type="telegram",
            is_enabled=True,
            status="online",
        )
        db.add(legacy_channel)
        db.commit()

        with pytest.raises(ValueError, match="channel_type_not_enabled"):
            upsert_open_api_app(
                db,
                0,
                OpenApiAppInput(
                    name="非法开放应用",
                    app_key="legacy-channel-app",
                    app_secret="test-secret",
                    default_channel_id=int(legacy_channel.id),
                    authorized_channel_ids=(int(legacy_channel.id),),
                ),
            )
