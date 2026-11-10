"""开关点击即保存:群功能/运行模式/插件启停的接口行为(只改一个开关、必须带 CSRF、返回 JSON),以及页面里统一的开关与保存按钮。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="switch-autosave-logs-"))

import pytest

from app.models.entities import Bot, BotPlugin, Chatroom, Plugin
from app.services.group_features.registry import get_feature
from app.services.group_features.store import get_state, has_saved_state, save_state, set_flag
from app.services.security import decrypt_secret
from tests.test_ai_chat import _session
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture()
def webridge_group(provider_page_context):  # noqa: F811
    from app.models.entities import Channel

    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.flush()
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, external_id="1@chatroom", name="养生群", is_active=True)
        db.add(room)
        db.commit()
        return client, ids, int(channel.id), int(room.id)


def _stored(ids, room_id: int, feature_key: str):
    with ids["factory"]() as db:
        room = db.get(Chatroom, room_id)
        feature = get_feature(feature_key)
        return get_state(room, feature), has_saved_state(room, feature), json.loads(room.extra_json or "{}")


def _headers(csrf: str) -> dict:
    return {"X-CSRF-Token": csrf, "X-Requested-With": "fetch"}


# ---------- 存储层:只改一个开关 ----------

def test_set_flag_changes_one_switch_and_leaves_everything_else_alone() -> None:
    db = _session()
    room = Chatroom(id=1, tenant_id=1, bot_id=1, channel_id=1, external_id="1@chatroom", name="群")
    db.add(room)
    save_state(room, "ai_chat", enabled=True, config={"persona": "你是小牛", "api_key": "sk-secret", "memory_all": True})
    encrypted_before = json.loads(room.extra_json)["group_features"]["ai_chat"]["config"]["api_key"]
    item = set_flag(room, "ai_chat", "web_search", False)
    assert item.key == "web_search"
    enabled, config = get_state(room, get_feature("ai_chat"))
    assert enabled is True and config["web_search"] is False and config["persona"] == "你是小牛" and config["memory_all"] is True
    # 密钥原样保留:不会被重新加密(重新加密会让解密后变成密文)
    stored = json.loads(room.extra_json)["group_features"]["ai_chat"]["config"]["api_key"]
    assert stored == encrypted_before and decrypt_secret(stored) == "sk-secret"
    # 总开关
    assert set_flag(room, "ai_chat", "enabled", False) is None
    assert get_state(room, get_feature("ai_chat"))[0] is False


def test_toggling_an_inner_switch_does_not_make_an_unsaved_feature_explicit() -> None:
    """没单独保存过的 AI 对话跟随租户的聊天开关;只改里面一个开关不能把它变成"显式关闭"。"""

    room = Chatroom(id=1, tenant_id=1, bot_id=1, channel_id=1, external_id="1@chatroom", name="群")
    set_flag(room, "ai_chat", "web_search", False)
    assert has_saved_state(room, get_feature("ai_chat")) is False
    set_flag(room, "ai_chat", "enabled", True)
    assert has_saved_state(room, get_feature("ai_chat")) is True


@pytest.mark.parametrize("field", ["persona", "api_key", "delay", "", "unknown"])
def test_set_flag_rejects_non_switch_fields(field) -> None:
    room = Chatroom(id=1, tenant_id=1, bot_id=1, channel_id=1, external_id="1@chatroom", name="群")
    with pytest.raises(ValueError):
        set_flag(room, "ai_chat", field, True)
    with pytest.raises(ValueError):
        set_flag(room, "no_such_feature", "enabled", True)


# ---------- 群功能接口 ----------

def test_feature_toggle_endpoint_changes_only_that_switch_and_returns_a_notice(webridge_group) -> None:
    client, ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    url = f"/channels/{channel_id}/chatrooms/{room_id}/features"
    on = client.post(f"{url}/transfer_receive/toggle", json={"field": "enabled", "value": True}, headers=_headers(csrf))
    assert on.status_code == 200 and on.json() == {"ok": True, "notice": "已开启「自动收款」"}
    assert _stored(ids, room_id, "transfer_receive")[0][0] is True
    off = client.post(f"{url}/transfer_receive/toggle", json={"field": "enabled", "value": False}, headers=_headers(csrf))
    assert off.json()["notice"] == "已关闭「自动收款」" and _stored(ids, room_id, "transfer_receive")[0][0] is False
    # 设置区里的开关:提示里是这个开关的名称
    inner = client.post(f"{url}/ai_chat/toggle", json={"field": "web_search", "value": False}, headers=_headers(csrf))
    assert inner.json()["notice"] == "已关闭「联网搜索」"
    (_enabled, config), explicit, _extra = _stored(ids, room_id, "ai_chat")
    assert config["web_search"] is False and explicit is False  # 没动总开关


@pytest.mark.parametrize(
    "body",
    [{"field": "persona", "value": True}, {"field": "enabled", "value": "true"}, {"field": "enabled"}, {"value": True}, {"field": "nope", "value": True}],
)
def test_feature_toggle_endpoint_rejects_bad_requests(webridge_group, body) -> None:
    client, _ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    response = client.post(f"/channels/{channel_id}/chatrooms/{room_id}/features/ai_chat/toggle", json=body, headers=_headers(csrf))
    assert response.status_code == 400 and response.json()["ok"] is False


def test_feature_endpoints_require_csrf_and_the_operator_role(webridge_group) -> None:
    client, ids, channel_id, room_id = webridge_group
    base = f"/channels/{channel_id}/chatrooms/{room_id}/features/transfer_receive"
    csrf = _login(client, "admin")
    assert client.post(f"{base}/toggle", json={"field": "enabled", "value": True}).status_code == 403  # 没有令牌
    assert client.post(f"{base}/toggle", json={"field": "enabled", "value": True}, headers=_headers("bad")).status_code == 403
    assert client.post(base, data={"enabled": "true"}).status_code == 403
    assert _stored(ids, room_id, "transfer_receive")[0][0] is False
    csrf = _login(client, "readonly")
    assert client.post(f"{base}/toggle", json={"field": "enabled", "value": True}, headers=_headers(csrf)).status_code == 403


def test_feature_save_returns_json_and_saves_all_fields(webridge_group) -> None:
    client, ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    response = client.post(
        f"/channels/{channel_id}/chatrooms/{room_id}/features/red_packet",
        data={"enabled": "true", "cfg_delay_ms": "2500"}, headers=_headers(csrf),
    )
    # 不再跳转:返回 JSON,页面脚本弹出提示
    assert response.status_code == 200 and response.json() == {"ok": True, "notice": "已保存「自动抢红包」的设置"}
    (enabled, config), explicit, _ = _stored(ids, room_id, "red_packet")
    assert enabled is True and config["delay_ms"] == 2500 and explicit is True


# ---------- 运行模式 ----------

def test_management_mode_via_script_returns_json_and_via_form_still_redirects(webridge_group) -> None:
    client, ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    url = f"/channels/{channel_id}/chatrooms/{room_id}/management"
    script = client.post(url, data={"management_status": "disabled"}, headers=_headers(csrf))
    assert script.status_code == 200 and script.json()["ok"] is True and script.json()["management_status"] == "disabled"
    # 群列表页的行内表单是整页提交(带 csrf_token 字段),仍然跳回列表
    form = client.post(url, data={"management_status": "enabled", "next": "/chatrooms", "csrf_token": csrf})
    assert form.status_code == 303 and form.headers["location"].startswith("/chatrooms")
    with ids["factory"]() as db:
        assert db.get(Chatroom, room_id).management_status == "enabled"
    assert client.post(url, data={"management_status": "disabled"}).status_code == 403  # 没有令牌


# ---------- 插件启停 ----------

@pytest.fixture()
def plugin_env(webridge_group):
    client, ids, channel_id, room_id = webridge_group
    with ids["factory"]() as db:
        plugin = Plugin(code="demo_plugin", display_name="演示插件", plugin_type="command")
        db.add(plugin)
        db.commit()
        return client, ids, channel_id, room_id, int(plugin.id)


def _binding(ids, plugin_id):
    with ids["factory"]() as db:
        return db.query(BotPlugin).filter(BotPlugin.plugin_id == plugin_id, BotPlugin.bot_id == ids["bot"]).first()


def test_plugin_switch_sets_the_target_state_idempotently(plugin_env) -> None:
    client, ids, _channel_id, _room_id, plugin_id = plugin_env
    csrf = _login(client, "admin")
    body = {"bot_id": str(ids["bot"]), "plugin_id": str(plugin_id)}
    # 还没绑定过:关闭什么也不用做;打开自动创建绑定并启用
    off = client.post("/plugins/toggle-direct", data={**body, "enabled": "false"}, headers=_headers(csrf)).json()
    assert off["ok"] and off["is_bound"] is False and off["is_enabled"] is False and _binding(ids, plugin_id) is None
    on = client.post("/plugins/toggle-direct", data={**body, "enabled": "true"}, headers=_headers(csrf)).json()
    assert on["is_bound"] and on["is_enabled"] and on["status_label"] == "已启用" and "已启用" in on["notice"] and on["enabled_count"] == 1
    # 提交目标状态:重复提交同一个状态不会反转
    again = client.post("/plugins/toggle-direct", data={**body, "enabled": "true"}, headers=_headers(csrf)).json()
    assert again["is_enabled"] is True and _binding(ids, plugin_id).status == "enabled"
    stopped = client.post("/plugins/toggle-direct", data={**body, "enabled": "false"}, headers=_headers(csrf)).json()
    assert stopped["is_enabled"] is False and stopped["status_label"] == "已停用" and stopped["enabled_count"] == 0
    # 没有令牌不行
    assert client.post("/plugins/toggle-direct", data={**body, "enabled": "true"}).status_code == 403


def test_binding_switch_on_the_runtime_page_sets_the_target_state(plugin_env) -> None:
    client, ids, _channel_id, _room_id, plugin_id = plugin_env
    csrf = _login(client, "admin")
    client.post("/plugins/toggle-direct", data={"bot_id": str(ids["bot"]), "plugin_id": str(plugin_id), "enabled": "true"}, headers=_headers(csrf))
    binding_id = _binding(ids, plugin_id).id
    off = client.post(f"/plugins/{binding_id}/toggle", data={"enabled": "false"}, headers=_headers(csrf))
    assert off.status_code == 200 and off.json() == {"ok": True, "notice": "插件绑定已停用", "is_enabled": False}
    assert client.post(f"/plugins/{binding_id}/toggle", data={"enabled": "false"}, headers=_headers(csrf)).json()["is_enabled"] is False  # 幂等
    assert client.post(f"/plugins/{binding_id}/toggle", data={"enabled": "true"}).status_code == 403


# ---------- 页面:统一的开关和保存按钮 ----------

def _group_html(webridge_group) -> str:
    client, _ids, channel_id, room_id = webridge_group
    _login(client, "admin")
    return client.get(f"/channels/{channel_id}/chatrooms/{room_id}").text


def _card(html: str, key: str) -> str:
    """群功能页里某个卡片(一个 form)的 HTML:从它的 data-gf-key 到它自己的 </form>。"""

    start = html.index(f'data-gf-key="{key}"')
    return html[start: html.index("</form>", start)]


def test_group_page_has_one_switch_style_and_save_buttons_only_where_there_are_inputs(webridge_group) -> None:
    html = _group_html(webridge_group)
    # 运行模式:没有保存按钮,单选框选中即生效
    mode = _card(html, "mode")
    assert "保存" not in mode and mode.count('data-autosave-url="') == 3 and 'data-autosave-key="management_status"' in mode
    # 只有开关、没有输入项的功能:既没有头部保存,也没有底部保存
    for key in ("transfer_receive", "media_download"):
        card = _card(html, key)
        assert "保存" not in card and 'data-autosave-json=\'{"field":"enabled"}\'' in card, key
    # 有文本/数字/密钥输入的功能:只有底部一个保存按钮
    for key in ("ai_chat", "red_packet", "inactive_rank", "member_change"):
        card = _card(html, key)
        assert card.count(">保存</button>") == 1 and "gf-foot" in card and f'data-gf-save-url' in html, key
    # 设置区里的开关也是点击即保存
    ai = _card(html, "ai_chat")
    assert 'data-autosave-json=\'{"field":"web_search"}\'' in ai and 'data-autosave-json=\'{"field":"memory_enabled"}\'' in ai


def test_every_switch_in_the_platform_uses_the_one_component() -> None:
    """全平台只有一种开关外观(.ui-switch):不再有 gf-switch、plugin-switch-toggle 这类各写各的。"""

    offenders = []
    for path in sorted((APP / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"gf-switch(?!-list)|plugin-switch-toggle|plugin-switch-track|plugin-switch-thumb", text):
            offenders.append(f"{path.name}: 旧的开关类名")
        for match in re.finditer(r'<input[^>]*role="switch"[^>]*>', text):
            # 每个开关 input 前面(同一个 label 里)必须有 .ui-switch 外壳
            if 'class="ui-switch' not in text[max(0, match.start() - 160): match.start()] and "ui-switch" not in text[max(0, match.start() - 320): match.start()]:
                offenders.append(f"{path.name}: 开关没有 .ui-switch 外壳 {match.group(0)[:70]}")
    assert offenders == [], offenders
    css = (APP / "static" / "css" / "style.css").read_text(encoding="utf-8")
    assert not re.search(r"\.gf-switch(?!-list)|\.plugin-switch-(toggle|track|thumb)", css)


def test_pages_load_the_toast_and_autosave_scripts_and_carry_a_csrf_token(webridge_group) -> None:
    html = _group_html(webridge_group)
    assert '<meta name="csrf-token" content="' in html
    assert "/static/js/toast.js" in html and "/static/js/switches.js" in html and "/static/js/group-features.js" in html


# ---------- 群内插件开关(群详情「插件」标签) ----------

def test_group_plugin_switch_saves_via_script_and_reports_whether_the_group_customised_it(plugin_env) -> None:
    client, ids, channel_id, room_id, plugin_id = plugin_env
    csrf = _login(client, "admin")
    client.post("/plugins/toggle-direct", data={"bot_id": str(ids["bot"]), "plugin_id": str(plugin_id), "enabled": "true"}, headers=_headers(csrf))
    binding_id = _binding(ids, plugin_id).id
    url = f"/channels/{channel_id}/chatrooms/{room_id}/scopes/{binding_id}/mode"
    off = client.post(url, data={"mode": "disabled"}, headers=_headers(csrf))
    assert off.status_code == 200 and off.json() == {"ok": True, "notice": "群插件已切换为群内停用", "mode": "disabled", "custom": True}
    on = client.post(url, data={"mode": "enabled"}, headers=_headers(csrf)).json()
    assert on["custom"] is True and on["mode"] == "enabled"
    # 恢复默认是整页表单提交(带 csrf_token 字段):回到插件标签;脚本提交时则返回 custom=false
    reset = client.post(url, data={"mode": "inherited", "csrf_token": csrf})
    assert reset.status_code == 303 and "tab=plugins" in reset.headers["location"]
    assert client.post(url, data={"mode": "inherited"}, headers=_headers(csrf)).json()["custom"] is False
    assert client.post(url, data={"mode": "disabled"}).status_code == 403  # 没有令牌
    # 非法模式:脚本提交得到 JSON 错误,不是 500
    assert client.post(url, data={"mode": "weird"}, headers=_headers(csrf)).status_code == 400


def test_group_plugin_tab_renders_the_switch_and_the_custom_markers(plugin_env) -> None:
    client, ids, channel_id, room_id, plugin_id = plugin_env
    csrf = _login(client, "admin")
    client.post("/plugins/toggle-direct", data={"bot_id": str(ids["bot"]), "plugin_id": str(plugin_id), "enabled": "true"}, headers=_headers(csrf))
    binding_id = _binding(ids, plugin_id).id
    html = client.get(f"/channels/{channel_id}/chatrooms/{room_id}?tab=plugins").text
    row = html[html.index("data-scope-row"): html.index("data-scope-row") + 2600]
    assert f'data-autosave-url="/channels/{channel_id}/chatrooms/{room_id}/scopes/{binding_id}/mode"' in row and 'data-autosave-on="enabled"' in row
    assert "已启用，点击停用" not in html and "已停用，点击启用" not in html  # 旧的文字按钮没有了
    # 没单独设置过:"本群已自定义"和"恢复默认"都隐藏着(脚本在开关保存后再显示)
    assert "data-scope-custom-chip hidden" in row and "data-scope-reset-form hidden" in row
    client.post(f"/channels/{channel_id}/chatrooms/{room_id}/scopes/{binding_id}/mode", data={"mode": "disabled"}, headers=_headers(csrf))
    html = client.get(f"/channels/{channel_id}/chatrooms/{room_id}?tab=plugins").text
    row = html[html.index("data-scope-row"): html.index("data-scope-row") + 2600]
    assert "data-scope-custom-chip hidden" not in row and "data-scope-reset-form hidden" not in row and "checked" not in row.split("data-autosave-form")[1][:20]


def test_plugins_and_directory_pages_use_switches_instead_of_buttons(plugin_env) -> None:
    client, ids, channel_id, room_id, plugin_id = plugin_env
    _login(client, "admin")
    plugins = client.get("/plugins").text
    assert 'data-autosave-url="/plugins/toggle-direct"' in plugins and "plugin-switch-toggle" not in plugins and "展开配置插件开关" not in plugins
    directory = client.get("/chatrooms").text
    assert f'data-autosave-url="/channels/{channel_id}/chatrooms/{room_id}/management"' in directory and ">启用</button>" not in directory and ">已启用</button>" not in directory


def test_generic_submit_lock_waits_for_script_handlers_before_locking_the_button() -> None:
    """通用的"提交后锁按钮"脚本必须等同一次提交里的其他监听器处理完再判断:
    被页面脚本(fetch 提交)接管的提交不能锁按钮,否则按钮会一直停在"处理中…"(消息发送框、功能设置保存都踩过)。"""

    script = (APP / "templates" / "base.html").read_text(encoding="utf-8")
    start = script.index('addEventListener("submit"')
    lock = script[start: script.index("}, 0);", start) + 6]
    assert "setTimeout" in lock and lock.index("setTimeout") < lock.index("event.defaultPrevented")
    assert "data-plugin-switch-form" not in script  # 插件页的旧开关表单已经不存在
