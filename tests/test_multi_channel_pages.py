from __future__ import annotations

import json
from urllib.parse import unquote

from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.models.entities import Bot, Channel
from app.models.saas import BotBinding
from app.schemas.forms import ChannelCreate
from app.services.crud import add_channel
from test_provider_node_pages import _login, provider_page_context


def _channel_edit_form(ids: dict, csrf_token: str, **overrides) -> dict[str, object]:
    """构造账号高级设置提交数据，并允许测试夹带已经废弃的敏感字段。"""

    payload: dict[str, object] = {
        "bot_id": ids["bot"],
        "name": "测试账号",
        "channel_type": "wechat_ipad",
        "api_base": "",
        "bot_token": "",
        "webhook_path": "",
        "dm_policy": "allowlist",
        "allow_from": "",
        "adapter_config_json": "",
        "provider_node_id": 0,
        "provider_account_id": 0,
        "csrf_token": csrf_token,
        "is_enabled": "false",
    }
    payload.update(overrides)
    return payload


def _seed_channel(
    ids: dict,
    *,
    name: str,
    channel_type: str,
    bot_id: int | None = None,
    api_base: str = "",
    bot_token: str = "",
    webhook_path: str = "",
    adapter_config_json: str = "",
    provider_node_id: int | None = None,
    provider_account_id: int | None = None,
    is_enabled: bool = False,
    with_binding: bool = False,
    bot_account_id: str = "",
) -> int:
    """直接准备页面测试夹具，避免重新引入已经下线的手工渠道创建入口。"""

    with tenant_scope(ids["alpha"]), ids["factory"]() as db:
        channel = add_channel(
            db,
            ChannelCreate(
                bot_id=int(bot_id or ids["bot"]),
                name=name,
                channel_type=channel_type,
                api_base=api_base,
                bot_token=bot_token,
                webhook_path=webhook_path,
                dm_policy="allowlist",
                allow_from="",
                adapter_config_json=adapter_config_json,
                provider_node_id=provider_node_id,
                provider_account_id=provider_account_id,
                is_enabled=is_enabled,
            ),
            status="online" if is_enabled else "draft",
        )
        if with_binding:
            # 正式账号必须同时关联机器人和渠道；测试摘要使用固定十六进制内容，
            # 不模拟或保存任何真实设备指纹。
            db.add(
                BotBinding(
                    tenant_id=ids["alpha"],
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    adapter_type=channel_type,
                    bot_account_id=bot_account_id or f"test-account-{channel.id}",
                    device_fingerprint_hash=f"{channel.id:064x}",
                    status="active",
                )
            )
        db.commit()
        return int(channel.id)


def test_account_page_uses_compact_business_list(provider_page_context) -> None:
    """账号接入首页只展示业务清单，不再平铺协议和执行端字段。"""

    client, _ids = provider_page_context
    _login(client, "admin")

    response = client.get("/channels")

    assert response.status_code == 200
    assert "账号接入" in response.text
    assert "账号总数" in response.text
    assert "剩余额度" in response.text
    assert 'href="/channels/connect"' in response.text
    for removed_text in ("API Base", "Webhook", "Adapter Config", "新增渠道"):
        assert removed_text not in response.text


def test_legacy_manual_channel_create_route_is_removed(provider_page_context) -> None:
    """旧手工入口不能绕过可信身份验证和机器人额度绑定直接创建渠道。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    response = client.post(
        "/channels",
        data=_channel_edit_form(
            ids,
            csrf_token,
            name="绕过接入创建",
            channel_type="telegram",
            bot_token="forged-token",
            is_enabled="true",
        ),
    )

    assert response.status_code == 405
    with ids["factory"]() as db:
        assert db.scalar(select(Channel.id).where(Channel.name == "绕过接入创建")) is None


def test_channel_type_is_immutable_after_creation(provider_page_context) -> None:
    """编辑请求篡改渠道类型时必须拒绝，原记录不能被转换成另一种协议。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    channel_id = _seed_channel(
        ids,
        name="不可换型账号",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
    )

    edited = client.post(
        f"/channels/{channel_id}/edit",
        data=_channel_edit_form(ids, csrf_token, name="尝试换型", channel_type="telegram"),
    )

    assert edited.status_code == 303
    assert "渠道类型不可修改" in unquote(edited.headers["location"])
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.channel_type == "wechat_ipad"
        assert channel.name == "不可换型账号"


def test_blank_token_keeps_existing_encrypted_secret_on_edit(provider_page_context) -> None:
    """敏感字段留空表示不轮换密钥，启用校验必须识别数据库中已有的密文。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    channel_id = _seed_channel(
        ids,
        name="Telegram 主账号",
        channel_type="telegram",
        bot_token="telegram-existing-token",
        with_binding=True,
        bot_account_id="telegram-account-1",
    )
    with ids["factory"]() as db:
        encrypted_before = db.get(Channel, channel_id).bot_token_encrypted

    detail = client.get(f"/channels/{channel_id}")
    assert detail.status_code == 200
    assert 'name="channel_type" value="telegram"' in detail.text
    assert 'name="provider_node_id"' not in detail.text

    edited = client.post(
        f"/channels/{channel_id}/edit",
        data=_channel_edit_form(
            ids,
            csrf_token,
            name="Telegram 业务账号",
            channel_type="telegram",
            bot_token="",
        ),
    )

    assert edited.status_code == 303
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.name == "Telegram 业务账号"
        assert channel.bot_token_encrypted == encrypted_before


def test_wechat_ipad_edit_removes_retired_token_configuration(provider_page_context) -> None:
    """874 不使用访问凭证，租户编辑也不能覆盖平台托管的服务地址。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    channel_id = _seed_channel(
        ids,
        name="874 微信账号",
        channel_type="wechat_ipad",
        api_base="http://ipad.platform.internal:9064",
    )
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        channel.bot_token_encrypted = "retired-encrypted-token"
        channel.bot_token_masked = "retired-token"
        db.commit()

    detail = client.get(f"/channels/{channel_id}")
    assert detail.status_code == 200
    assert 'name="bot_token"' not in detail.text
    assert 'name="api_base"' not in detail.text
    assert "http://ipad.platform.internal:9064" not in detail.text

    edited = client.post(
        f"/channels/{channel_id}/edit",
        data=_channel_edit_form(
            ids,
            csrf_token,
            name="874 微信账号",
            channel_type="wechat_ipad",
            api_base="http://forged.internal:9064",
            bot_token="forged-token",
        ),
    )

    assert edited.status_code == 303
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert channel.bot_token_encrypted is None
        assert channel.bot_token_masked is None
        assert channel.api_base == "http://ipad.platform.internal:9064"


def test_wechat_ipad_detail_only_exposes_business_login_entry(provider_page_context) -> None:
    """账号详情只提供重新登录业务入口，不再向租户展示协议诊断和登录技术操作。"""

    client, ids = provider_page_context
    _login(client, "admin")
    channel_id = _seed_channel(
        ids,
        name="微信业务账号",
        channel_type="wechat_ipad",
        api_base="http://ipad.platform.internal:9064",
        with_binding=True,
        bot_account_id="wxid-business-account",
    )

    detail = client.get(f"/channels/{channel_id}")

    assert detail.status_code == 200
    assert "重新登录" in detail.text
    assert f'href="/channels/{channel_id}/wechat-ipad/login-session"' in detail.text
    for removed_text in (
        "健康检查",
        "高级连接信息",
        "登录与二维码",
        "协议地址",
        "二维码链接",
        "二维码刷新次数",
        "CheckQR",
        "Protocol Responses",
        "获取二维码登录",
        "初始化登录",
        "同步状态",
        "登录代理",
    ):
        assert removed_text not in detail.text


def test_wechat_ipad_edit_removes_retired_proxy_config_and_ignores_forged_fields(provider_page_context) -> None:
    """编辑微信账号时直接清除旧长期代理配置，伪造表单字段也不能重建该路径。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    secret_proxy_password = "proxy-secret-not-for-html"
    channel_id = _seed_channel(
        ids,
        name="安全配置微信",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
        webhook_path="/private-webhook",
        adapter_config_json=json.dumps(
            {
                "proxy": {
                    "host": "127.0.0.1",
                    "port": "8899",
                    "username": "proxy-user",
                    "password": secret_proxy_password,
                    "proxy_ip": "127.0.0.1:8899",
                },
                "business": {"message_mode": "normal"},
            }
        ),
    )

    detail = client.get(f"/channels/{channel_id}")
    assert detail.status_code == 200
    assert secret_proxy_password not in detail.text
    assert "adapter_config_json" not in detail.text
    assert 'name="webhook_path"' not in detail.text

    edit_payload = _channel_edit_form(
        ids,
        csrf_token,
        name="安全配置微信",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
        proxy_host="127.0.0.1",
        proxy_port="8899",
        proxy_username="proxy-user",
        proxy_password="forged-new-password",
    )
    for hidden_field in ("adapter_config_json", "webhook_path"):
        edit_payload.pop(hidden_field, None)
    edited = client.post(f"/channels/{channel_id}/edit", data=edit_payload)
    assert edited.status_code == 303

    with ids["factory"]() as db:
        saved = db.get(Channel, channel_id)
        assert saved.webhook_path == "/private-webhook"
        saved_config = json.loads(saved.adapter_config_json or "{}")
        assert "proxy" not in saved_config
        assert saved_config["business"] == {"message_mode": "normal"}
        assert secret_proxy_password not in (saved.adapter_config_json or "")
        assert "forged-new-password" not in (saved.adapter_config_json or "")


def test_incomplete_draft_cannot_be_enabled_by_toggle(provider_page_context) -> None:
    """即使协议配置完整，没有商业额度绑定的草稿也不能绕过接入向导启用。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    channel_id = _seed_channel(
        ids,
        name="未绑定微信",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
    )

    toggled = client.post(f"/channels/{channel_id}/toggle", data={"csrf_token": csrf_token})

    assert toggled.status_code == 303
    assert "账号接入" in unquote(toggled.headers["location"])
    with ids["factory"]() as db:
        assert db.get(Channel, channel_id).is_enabled is False


def test_advanced_edit_cannot_replace_bound_bot_account_or_enable_channel(provider_page_context) -> None:
    """租户高级设置只改运行配置，不能换机器人、企微账号或绕过独立启用动作。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    with ids["factory"]() as db:
        other_bot = Bot(tenant_id=ids["alpha"], name="伪造机器人", status="stopped", is_enabled=False)
        db.add(other_bot)
        db.commit()
        other_bot_id = int(other_bot.id)
    channel_id = _seed_channel(
        ids,
        name="已绑定企微",
        channel_type="workwechat",
        provider_node_id=ids["active_node"],
        provider_account_id=ids["account_a"],
        with_binding=True,
        bot_account_id="workwechat-account-a",
    )

    detail = client.get(f"/channels/{channel_id}")
    assert detail.status_code == 200
    for removed_field in ('name="bot_id"', 'name="provider_node_id"', 'name="provider_account_id"', 'name="is_enabled"'):
        assert removed_field not in detail.text

    edited = client.post(
        f"/channels/{channel_id}/edit",
        data=_channel_edit_form(
            ids,
            csrf_token,
            bot_id=other_bot_id,
            name="仅修改名称",
            channel_type="workwechat",
            provider_node_id=ids["active_node"],
            provider_account_id=ids["account_b"],
            is_enabled="true",
        ),
    )

    assert edited.status_code == 303
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        binding = db.scalar(select(BotBinding).where(BotBinding.channel_id == channel_id))
        assert channel.name == "仅修改名称"
        assert channel.bot_id == ids["bot"] == binding.bot_id
        assert channel.provider_node_id == ids["active_node"]
        assert channel.provider_account_id == ids["account_a"]
        assert channel.is_enabled is False


def test_only_tenant_admin_can_toggle_bound_account(provider_page_context) -> None:
    """运营角色只能使用账号能力，不能停用或重新启用企业绑定账号。"""

    client, ids = provider_page_context
    channel_id = _seed_channel(
        ids,
        name="管理员控制账号",
        channel_type="telegram",
        bot_token="telegram-existing-token",
        with_binding=True,
        bot_account_id="telegram-account-2",
    )
    operator_csrf = _login(client, "operator")

    denied = client.post(f"/channels/{channel_id}/toggle", data={"csrf_token": operator_csrf})

    assert denied.status_code == 403
    with ids["factory"]() as db:
        assert db.get(Channel, channel_id).is_enabled is False


def test_tenant_admin_can_toggle_bound_account(provider_page_context) -> None:
    """完成额度绑定后，企业管理员仍可通过唯一启停入口恢复账号。"""

    client, ids = provider_page_context
    channel_id = _seed_channel(
        ids,
        name="可启用账号",
        channel_type="telegram",
        bot_token="telegram-existing-token",
        with_binding=True,
        bot_account_id="telegram-account-3",
    )
    csrf_token = _login(client, "admin")

    toggled = client.post(f"/channels/{channel_id}/toggle", data={"csrf_token": csrf_token})

    assert toggled.status_code == 303
    with ids["factory"]() as db:
        assert db.get(Channel, channel_id).is_enabled is True
