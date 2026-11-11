"""插件页重做:配置是主体,没人看的信息收起来;同时补上修改类接口缺失的 CSRF 校验。

背景:
- 详情页以前是一张"技术信息卡"(编码、版本、作者、适用范围、能力标签),真正要用的配置藏在"修改配置"弹窗里,要点两步才能配置。
  现在每个绑定的机器人是一张卡片(和群功能同一套交互),技术信息收进最下面折叠的区域。
- 实测发现 /plugins/{id}/edit、/reload、/bind、/execute 不带 CSRF 令牌也能成功(edit 甚至能把绑定停用),
  而 /toggle 是有校验的。这些修改类接口现在都要求令牌。
"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="plugin-detail-logs-"))

import pytest
from sqlalchemy import select

import app.main as main_module
from app.models.entities import BotPlugin, Plugin
from app.services import plugin_directory
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)


@pytest.fixture()
def pages(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db, force=True)
        weather = db.scalar(select(Plugin).where(Plugin.code == "weather"))
        other = db.scalar(select(Plugin).where(Plugin.code == "dota2_stats"))
        # 天气:停用状态的绑定,用来验证"保存配置不会改变启停";另一个插件只用于列表页
        binding = BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=weather.id, status="disabled", runtime_status="disabled", config_json="{}")
        db.add(binding)
        db.add(BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=other.id, status="enabled", runtime_status="running", config_json="{}"))
        db.commit()
        return client, ids, int(weather.id), int(binding.id)


def _binding_state(ids, binding_id: int) -> tuple[str, dict]:
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, binding_id)
        return binding.status, json.loads(binding.config_json or "{}")


# ---------- 页面内容 ----------

def test_the_detail_page_leads_with_how_to_use_and_hides_the_technical_card(pages) -> None:
    client, _ids, weather_id, _binding_id = pages
    _login(client, "admin")
    html = client.get(f"/plugins/{weather_id}").text
    head, _, tech = html.partition("技术信息")
    assert tech  # 技术信息还在,只是收到了最下面
    # 没人看的信息不再出现在主体里
    for noisy in ("插件编码", "默认状态", "能力标签", "适用范围", "wechat_ipad", "schema-form", "command-match"):
        assert noisy not in head, noisy
        assert noisy in tech or noisy in ("schema-form", "command-match", "wechat_ipad"), noisy
    assert "插件编码" in tech and "适用范围" in tech and "能力标签" in tech
    # 用户想知道的:怎么用
    assert "使用方法" in head and "<code>天气</code>" in head and "<code>订阅天气</code>" in head
    assert "消息里包含关键词" in head and "插件详情" not in head


def test_each_binding_is_a_card_with_the_group_feature_interaction(pages) -> None:
    client, ids, weather_id, binding_id = pages
    csrf = _login(client, "admin")
    html = client.get(f"/plugins/{weather_id}").text
    assert f'data-gf-key="plugin-binding-{binding_id}"' in html and "data-gf-card" in html
    assert f'data-autosave-url="/plugins/{binding_id}/toggle"' in html and 'data-autosave-key="enabled"' in html  # 开关点击即保存
    assert f'data-gf-save-url="/plugins/{binding_id}/edit"' in html  # 保存用 fetch,弹出提示
    assert "/static/js/group-features.js" in html
    assert 'class="gf-toggle"' in html and "展开或收起设置" in html  # 右侧小箭头
    assert "已停用" in html  # 绑定是停用状态
    # 配置字段(中文标签、分组)在卡片里;不再是弹窗
    assert "天气接口地址" in html and "<h4>触发方式</h4>" in html and "edit-plugin-binding" not in html and "修改配置" not in html
    # 修改类表单都带令牌;重载是另一个表单,卡片里的按钮用 form 属性提交它
    assert html.count(f'name="csrf_token" value="{csrf}"') >= 3
    assert f'form="reload-binding-{binding_id}"' in html and f'id="reload-binding-{binding_id}"' in html


def test_the_plugin_list_shows_problems_only_and_a_plain_config_button(pages, monkeypatch) -> None:
    client, _ids, weather_id, _binding_id = pages
    real = main_module.get_plugin_runtime_meta
    monkeypatch.setattr(main_module, "get_plugin_runtime_meta", lambda code: {**real(code), "manifest_errors": ["缺少 plugin.code。"]} if code == "weather" else real(code))
    _login(client, "admin")
    html = client.get("/plugins").text
    rows = html.split('<article class="plugin-catalog-row">')[1:]
    (row,) = [item for item in rows if f'<h3><a href="/plugins/{weather_id}">' in item]  # 按链接精确定位天气那一行
    assert "暂不可用" in row and f'href="/plugins/{weather_id}"' in row and ">配置</a>" in row
    assert all("可执行" not in item and "查看详情" not in item for item in rows)  # 列表行里不再给每个插件挂"可执行"和版本号


def test_a_dependent_field_is_marked_so_it_dims_when_its_switch_is_off() -> None:
    template = main_module.templates.env.get_template("_plugin_config_form.html")
    view = {
        "mode": "schema", "description": "",
        "fields": [
            {"name": "loud", "field_type": "boolean", "label": "高调", "description": "", "value": False, "render_value": "false", "sensitive": False, "placeholder": "", "options": (), "group": "", "minimum": None, "maximum": None, "required": False, "depends_on": ""},
            {"name": "volume", "field_type": "number", "label": "音量", "description": "", "value": 5, "render_value": "5", "sensitive": False, "placeholder": "", "options": (), "group": "", "minimum": 1, "maximum": 9, "required": False, "depends_on": "loud"},
        ],
    }
    html = template.render(config_view=view)
    assert 'data-gf-depends="schema__loud"' in html and html.count("data-gf-depends") == 1  # 只有依赖项带标记
    # 开关前面有同名隐藏域,脚本必须取复选框来判断,否则永远变淡
    script = (main_module.APP_ROOT / "app" / "static" / "js" / "group-features.js").read_text(encoding="utf-8")
    assert "input[type=\"checkbox\"][name=" in script


# ---------- 保存 ----------

def test_saving_with_fetch_returns_json_and_never_changes_whether_the_binding_is_enabled(pages) -> None:
    client, ids, _weather_id, binding_id = pages
    csrf = _login(client, "admin")
    response = client.post(
        f"/plugins/{binding_id}/edit",
        data={"schema__default_city": "上海", "schema__timeout_seconds": "999", "schema__require_mention": "false"},
        headers={"X-CSRF-Token": csrf, "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200 and response.json() == {"ok": True, "notice": "插件绑定配置已保存"}
    status, config = _binding_state(ids, binding_id)
    assert status == "disabled"  # 启停由单独的开关负责,保存配置不能把停用的绑定又打开
    assert config["default_city"] == "上海" and config["timeout_seconds"] == 60  # 声明的范围生效


def test_a_normal_form_post_redirects_back_only_to_plugin_pages(pages) -> None:
    client, _ids, weather_id, binding_id = pages
    csrf = _login(client, "admin")
    good = client.post(f"/plugins/{binding_id}/edit", data={"csrf_token": csrf, "next": f"/plugins/{weather_id}", "schema__default_city": "广州"}, follow_redirects=False)
    assert good.status_code == 303 and good.headers["location"].startswith(f"/plugins/{weather_id}?notice=")
    for evil in ("https://evil.example/x", "//evil.example", "/plugins//evil.example", "/other"):
        bad = client.post(f"/plugins/{binding_id}/edit", data={"csrf_token": csrf, "next": evil}, follow_redirects=False)
        assert bad.status_code == 303 and bad.headers["location"].startswith("/plugins/runtime?notice="), evil  # 不会被带去别的站点
    reload_ok = client.post(f"/plugins/{binding_id}/reload", data={"csrf_token": csrf, "next": f"/plugins/{weather_id}"}, follow_redirects=False)
    assert reload_ok.status_code == 303 and reload_ok.headers["location"].startswith(f"/plugins/{weather_id}?notice=")


def test_every_mutating_plugin_endpoint_refuses_requests_without_a_csrf_token(pages) -> None:
    client, ids, weather_id, binding_id = pages
    _login(client, "admin")
    before = _binding_state(ids, binding_id)
    attempts = [
        ("edit", f"/plugins/{binding_id}/edit", {"status": "enabled", "schema__default_city": "被篡改"}),
        ("reload", f"/plugins/{binding_id}/reload", {}),
        ("bind", "/plugins/bind", {"bot_id": ids["bot"], "plugin_id": weather_id, "status": "enabled"}),
    ]
    for name, path, data in attempts:
        assert client.post(path, data=data, follow_redirects=False).status_code == 403, name
    assert client.post(f"/plugins/{binding_id}/execute", json={}, follow_redirects=False).status_code == 403  # JSON 接口走请求头令牌
    assert _binding_state(ids, binding_id) == before  # 什么都没被改
    # 带上令牌的 JSON 调用不再被 CSRF 拦下(执行本身的结果不在这里断言,只要不是 403)
    csrf = _login(client, "admin")
    assert client.post(f"/plugins/{binding_id}/execute", json={}, headers={"X-CSRF-Token": csrf}).status_code != 403
