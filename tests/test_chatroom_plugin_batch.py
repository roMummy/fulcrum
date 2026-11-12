"""群插件批量开关通过真实认证、租户隔离和表单路由验证。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="chatroom-plugin-batch-logs-"))

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, GroupPluginScope, Plugin
from app.services.directory_pages import build_chatroom_plugin_directory
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture()
def batch_context(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        plugin = Plugin(code="batch-example", display_name="批量示例", platform_status="active", visibility="all")
        other_plugin = Plugin(code="batch-other", display_name="其他插件", platform_status="active", visibility="all")
        second_bot = Bot(tenant_id=ids["alpha"], name="第二个机器人")
        unbound_bot = Bot(tenant_id=ids["alpha"], name="未绑定机器人")
        beta_bot = Bot(tenant_id=ids["beta"], name="乙机器人")
        db.add_all([plugin, other_plugin, second_bot, unbound_bot, beta_bot])
        db.flush()
        channels = [
            Channel(tenant_id=ids["alpha"], bot_id=second_bot.id, name="第二账号", channel_type="wechat_webridge"),
            Channel(tenant_id=ids["alpha"], bot_id=unbound_bot.id, name="未绑定账号", channel_type="wechat_winauto"),
            Channel(tenant_id=ids["beta"], bot_id=beta_bot.id, name="乙账号", channel_type="workwechat"),
        ]
        db.add_all(channels)
        db.flush()
        bindings = [
            BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=plugin.id, status="enabled"),
            BotPlugin(tenant_id=ids["alpha"], bot_id=second_bot.id, plugin_id=plugin.id, status="disabled"),
            BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=other_plugin.id, status="enabled"),
        ]
        rooms = [
            Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="batch-a@chatroom", name="批量甲群", management_status="enabled", is_active=True),
            Chatroom(tenant_id=ids["alpha"], bot_id=second_bot.id, channel_id=channels[0].id, external_id="batch-b@chatroom", name="批量乙群", management_status="disabled", is_active=False),
            Chatroom(tenant_id=ids["alpha"], bot_id=unbound_bot.id, channel_id=channels[1].id, external_id="batch-c@chatroom", name="未绑定群"),
            Chatroom(tenant_id=ids["beta"], bot_id=beta_bot.id, channel_id=channels[2].id, external_id="batch-d@chatroom", name="其他企业的群"),
            Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="batch-left@chatroom", name="退出群", status="left"),
        ]
        db.add_all([*bindings, *rooms])
        db.flush()
        scope = GroupPluginScope(tenant_id=ids["alpha"], channel_id=rooms[0].channel_id, chatroom_id=rooms[0].id, bot_plugin_id=bindings[0].id, enabled=False, scope_status="overridden", config_override_json='{"keep": true}', policy_effect="allow")
        other_scope = GroupPluginScope(tenant_id=ids["alpha"], channel_id=rooms[0].channel_id, chatroom_id=rooms[0].id, bot_plugin_id=bindings[2].id, enabled=False, scope_status="overridden")
        db.add_all([scope, other_scope])
        db.commit()
        ids = {
            **ids,
            "plugin": int(plugin.id),
            "bindings": [int(item.id) for item in bindings],
            "rooms": [int(item.id) for item in rooms],
            "scope": int(scope.id),
            "other_scope": int(other_scope.id),
        }
    return client, ids


def _post(client, ids, csrf, room_ids, mode="enabled", **extra):
    return client.post(
        "/chatrooms/plugins/batch",
        data={"csrf_token": csrf, "plugin_id": ids["plugin"], "chatroom_ids": room_ids, "mode": mode, **extra},
        headers={"X-Requested-With": "fetch"},
    )


def test_batch_enables_multiple_bots_and_preserves_other_settings(batch_context):
    client, ids = batch_context
    csrf = _login(client, "operator")
    response = _post(client, ids, csrf, ids["rooms"][:2])
    assert response.status_code == 200
    assert response.json()["count"] == 2
    with ids["factory"]() as db:
        scope = db.get(GroupPluginScope, ids["scope"])
        assert scope.enabled is True and scope.scope_status == "overridden"
        assert scope.config_override_json == '{"keep": true}' and scope.policy_effect == "allow"
        assert scope.last_operator_id is not None
        assert db.get(GroupPluginScope, ids["other_scope"]).enabled is False
        second_scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids["rooms"][1], GroupPluginScope.bot_plugin_id == ids["bindings"][1]))
        assert second_scope.enabled is True
        assert db.get(BotPlugin, ids["bindings"][1]).status == "disabled"
        assert db.get(Chatroom, ids["rooms"][1]).management_status == "disabled"
        assert db.get(Chatroom, ids["rooms"][1]).is_active is False


def test_batch_disables_only_selected_plugin(batch_context):
    client, ids = batch_context
    csrf = _login(client, "admin")
    assert _post(client, ids, csrf, ids["rooms"][:2]).status_code == 200
    assert _post(client, ids, csrf, ids["rooms"][:2], "disabled").status_code == 200
    with ids["factory"]() as db:
        scopes = db.scalars(select(GroupPluginScope).where(GroupPluginScope.bot_plugin_id.in_(ids["bindings"][:2]))).all()
        assert len(scopes) == 2 and all(not scope.enabled for scope in scopes)
        assert db.get(BotPlugin, ids["bindings"][0]).status == "enabled"
        assert db.get(GroupPluginScope, ids["scope"]).config_override_json == '{"keep": true}'


@pytest.mark.parametrize("bad_index, expected", [(2, 400), (3, 404), (4, 400)])
def test_invalid_room_rejects_entire_batch_without_partial_changes(batch_context, bad_index, expected):
    client, ids = batch_context
    csrf = _login(client, "admin")
    assert _post(client, ids, csrf, [ids["rooms"][0], ids["rooms"][bad_index]]).status_code == expected
    with ids["factory"]() as db:
        assert db.get(GroupPluginScope, ids["scope"]).enabled is False
        assert db.scalar(select(BotPlugin.id).where(BotPlugin.bot_id == db.get(Chatroom, ids["rooms"][2]).bot_id)) is None


def test_csrf_readonly_and_unbound_binding_are_rejected(batch_context):
    client, ids = batch_context
    csrf = _login(client, "admin")
    assert _post(client, ids, "forged", ids["rooms"][:1]).status_code == 403
    readonly_csrf = _login(client, "readonly")
    assert _post(client, ids, readonly_csrf, ids["rooms"][:1]).status_code == 403
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        db.get(BotPlugin, ids["bindings"][0]).status = "unbound"
        db.commit()
    assert _post(client, ids, csrf, ids["rooms"][:1]).status_code == 400
    with ids["factory"]() as db:
        assert db.get(GroupPluginScope, ids["scope"]).enabled is False


def test_directory_renders_batch_controls_only_for_operators(batch_context):
    client, ids = batch_context
    _login(client, "operator")
    directory = client.get("/chatrooms")
    assert directory.status_code == 200 and 'href="/chatrooms/plugin-scopes"' in directory.text
    assert 'id="chatroom-plugin-batch"' not in directory.text
    page = client.get(f"/chatrooms/plugin-scopes?plugin_id={ids['plugin']}")
    assert page.status_code == 200
    assert 'id="chatroom-plugin-batch"' in page.text and "批量启用" in page.text
    assert f'name="chatroom_ids" value="{ids["rooms"][0]}"' in page.text
    assert f'name="chatroom_ids" value="{ids["rooms"][3]}"' not in page.text
    _login(client, "readonly")
    readonly_page = client.get(f"/chatrooms/plugin-scopes?plugin_id={ids['plugin']}")
    assert readonly_page.status_code == 200 and 'id="chatroom-plugin-batch"' not in readonly_page.text


def test_invalid_mode_and_redirect_target_are_handled(batch_context):
    client, ids = batch_context
    csrf = _login(client, "admin")
    assert _post(client, ids, csrf, ids["rooms"][:1], mode="all").status_code == 400
    response = client.post("/chatrooms/plugins/batch", data={"csrf_token": csrf, "plugin_id": ids["plugin"], "chatroom_ids": ids["rooms"][:1], "mode": "enabled", "next": "//example.com/"})
    assert response.status_code == 303 and response.headers["location"].startswith(f"/chatrooms/plugin-scopes?plugin_id={ids['plugin']}&selection_cleared=1&notice=")


def test_saved_scope_and_inherited_default_are_separate_from_global_switch(batch_context):
    _client, ids = batch_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        directory = build_chatroom_plugin_directory(db, tenant_id=ids["alpha"], plugin_id=ids["plugin"])
        rows = {item["id"]: item for item in directory["rows"]}
        assert rows[ids["rooms"][0]]["enabled"] is False
        assert rows[ids["rooms"][0]]["source"] == "overridden"
        # 第二个绑定总开关关闭，但它保存的默认群选择仍是启用。
        second = rows[ids["rooms"][1]]
        assert second["enabled"] is True and second["source"] == "inherited"
        assert second["binding_enabled"] is False and second["group_running"] is False
        enabled = build_chatroom_plugin_directory(db, tenant_id=ids["alpha"], plugin_id=ids["plugin"], scope_status="enabled", scope_source="inherited")
        assert ids["rooms"][1] in [row["id"] for row in enabled["rows"]]
        assert ids["rooms"][0] not in [row["id"] for row in enabled["rows"]]
        db.get(BotPlugin, ids["bindings"][1]).group_default_enabled = False
        db.commit()
        disabled = build_chatroom_plugin_directory(db, tenant_id=ids["alpha"], plugin_id=ids["plugin"], scope_status="disabled")
        assert ids["rooms"][1] in [row["id"] for row in disabled["rows"]]


def test_separate_page_supports_search_account_filters_and_pagination(batch_context):
    client, ids = batch_context
    _login(client, "operator")
    with ids["factory"]() as db:
        db.add_all([
            Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id=f"paged-{number}@chatroom", name=f"分页测试{number:02}")
            for number in range(35)
        ])
        db.commit()
    base = f"/chatrooms/plugin-scopes?plugin_id={ids['plugin']}&q=分页测试&channel_id={ids['channel_id']}"
    first = client.get(base)
    second = client.get(base + "&page=2")
    assert first.status_code == second.status_code == 200
    assert first.text.count('data-scope-room aria-label=') == 30
    assert second.text.count('data-scope-room aria-label=') == 5
    assert "分页测试00" in first.text and "分页测试34" not in first.text
    assert "分页测试34" in second.text and "分页测试00" not in second.text
    assert f'data-selection-key="fulcrum:group-plugin:{ids["alpha"]}:{ids["plugin"]}"' in first.text
    assert 'src="/static/js/chatroom-plugin-scopes.js"' in first.text
    assert "支持跨页、跨筛选保留选择" in first.text


def test_batch_accepts_more_than_one_page_and_clears_selection_on_success(batch_context):
    client, ids = batch_context
    csrf = _login(client, "operator")
    with ids["factory"]() as db:
        rooms = [Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id=f"many-{number}@chatroom", name=f"跨页{number}") for number in range(101)]
        db.add_all(rooms)
        db.commit()
        room_ids = [room.id for room in rooms]
    result = _post(client, ids, csrf, room_ids, next=f"/chatrooms/plugin-scopes?plugin_id={ids['plugin']}&page=2&scope_status=disabled")
    assert result.status_code == 200 and result.json()["count"] == 101
    assert "page=2&scope_status=disabled&selection_cleared=1" in result.json()["redirect_url"]
    returned = client.get(result.json()["redirect_url"])
    assert returned.status_code == 200 and 'data-clear-selection="true"' in returned.text
    assert _post(client, ids, csrf, list(range(1, 1002))).status_code == 400


def test_select_matching_includes_all_pages_and_respects_filters(batch_context):
    _client, ids = batch_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        rooms = [Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id=f"select-all-{i}@chatroom", name=f"全选测试{i:03d}") for i in range(353)]
        db.add_all(rooms)
        db.add(Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="select-all-left@chatroom", name="全选测试退出", status="left"))
        db.get(Chatroom, ids["rooms"][2]).name = "全选测试未绑定"
        db.flush()
        room_ids = [room.id for room in rooms]
        db.add(GroupPluginScope(tenant_id=ids["alpha"], channel_id=ids["channel_id"], chatroom_id=room_ids[-1], bot_plugin_id=ids["bindings"][0], enabled=False, scope_status="overridden"))
        db.commit()
        arguments = dict(tenant_id=ids["alpha"], plugin_id=ids["plugin"], keyword="全选测试", channel_id=ids["channel_id"])
        first = build_chatroom_plugin_directory(db, **arguments)
        last = build_chatroom_plugin_directory(db, **arguments, page=12)
        assert len(first["rows"]) == 30
        assert first["matching_room_ids"] == last["matching_room_ids"] == room_ids
        disabled = build_chatroom_plugin_directory(db, **arguments, scope_status="disabled", scope_source="overridden")
        assert disabled["matching_room_ids"] == room_ids[-1:]
        enabled = build_chatroom_plugin_directory(db, **arguments, scope_status="enabled", scope_source="inherited")
        assert enabled["matching_room_ids"] == room_ids[:-1]
        assert build_chatroom_plugin_directory(db, **{**arguments, "channel_id": -1})["matching_room_ids"] == []
        assert build_chatroom_plugin_directory(db, **{**arguments, "keyword": "无匹配"})["matching_room_ids"] == []


def test_select_matching_is_bounded_at_batch_limit(batch_context):
    _client, ids = batch_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        db.add_all([Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id=f"limit-{i}@chatroom", name="上限测试") for i in range(1002)])
        db.commit()
        directory = build_chatroom_plugin_directory(db, tenant_id=ids["alpha"], plugin_id=ids["plugin"], keyword="上限测试")
        assert directory["paging"]["total"] == 1002
        assert len(directory["matching_room_ids"]) == 1001
