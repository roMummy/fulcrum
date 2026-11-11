"""插件清单 v2:标准 TOML 解析、[[config.fields]] 声明、由声明生成的配置表单与保存。

背景:配置表单以前是靠 [runtime] 的默认值"猜"类型,标签是字段名机械转换("Max Items"),说明是英文模板。
现在插件在清单里声明字段(中文标签、说明、范围、分组、下拉选项),页面和校验都从声明派生;没有声明的插件仍按原来的方式推断。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="plugin-manifest-logs-"))

import pytest
from sqlalchemy import select
from starlette.datastructures import FormData

from app.core.settings import settings
from app.models.entities import BotPlugin, Plugin
from app.services import plugin_directory
from app.services.plugin_manifest import ManifestParseError, parse_manifest, read_config_fields
from app.services.plugin_runtime import registry
from app.services import plugin_schemas
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

BASE = """
[plugin]
code = "demo"
display_name = "示例"
[capability]
channel_types = ["simulator"]
chat_types = ["group"]
message_types = ["text"]
[matcher]
commands = ["示例"]
[runtime]
ratio = 0.5
"""


def _fields(body: str):
    return read_config_fields(parse_manifest(BASE + body))


# ---------- 解析 ----------

def test_the_standard_toml_parser_handles_floats_and_arrays_of_tables() -> None:
    parsed = parse_manifest(BASE + '\n[[config.fields]]\nkey = "a"\nlabel = "甲"\n\n[[config.fields]]\nkey = "b"\nlabel = "乙"\n')
    assert parsed["runtime"]["ratio"] == 0.5 and isinstance(parsed["runtime"]["ratio"], float)  # 以前小数会被当成字符串
    assert [item["key"] for item in parsed["config"]["fields"]] == ["a", "b"]
    with pytest.raises(ManifestParseError):
        parse_manifest("[plugin\ncode = ")


def test_a_manifest_without_declarations_has_no_fields_and_no_errors() -> None:
    assert _fields("") == ((), [])


def test_declared_fields_carry_label_hint_range_group_options_and_defaults() -> None:
    fields, errors = _fields(
        """
[[config.fields]]
key = "timeout"
label = "超时(秒)"
label_en = "Timeout"
kind = "number"
default = 15
min = 5
max = 60
hint = "最长等待"
group = "接口"
required = true

[[config.fields]]
key = "mode"
label = "模式"
kind = "select"
default = "fast"
options = ["fast", { value = "slow", label = "慢速" }]

[[config.fields]]
key = "words"
label = "词语"
kind = "tags"
default = ["甲", "乙"]
depends_on = "enabled_extra"

[[config.fields]]
key = "enabled_extra"
label = "启用额外"
kind = "bool"
default = true

[[config.fields]]
key = "token"
label = "密钥"
kind = "secret"
"""
    )
    assert errors == []
    by_key = {item.key: item for item in fields}
    timeout = by_key["timeout"]
    assert (timeout.label, timeout.label_en, timeout.kind, timeout.default, timeout.minimum, timeout.maximum, timeout.group, timeout.required) == ("超时(秒)", "Timeout", "number", 15, 5, 60, "接口", True)
    assert by_key["mode"].options == (("fast", "fast"), ("slow", "慢速"))
    assert by_key["words"].default == ["甲", "乙"] and by_key["words"].depends_on == "enabled_extra"
    assert by_key["token"].default == ""  # 密钥没有默认值


@pytest.mark.parametrize(
    "body, expected",
    [
        ('[[config.fields]]\nkey = "Bad-Key"\nlabel = "x"', "key 必须是小写字母开头"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\n[[config.fields]]\nkey = "a"\nlabel = "y"', "key 重复"),
        ('[[config.fields]]\nkey = "a"', "缺少 label"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "color"', "kind 非法"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "secret"\ndefault = "abc"', "密钥不能有默认值"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "number"\ndefault = 1\nmin = 9\nmax = 3', "min 不能大于 max"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "number"\ndefault = 100\nmax = 3', "default 超出"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "number"\ndefault = "abc"', "default 必须是数字"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "select"', "select 必须提供 options"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "select"\ndefault = "z"\noptions = ["x", "y"]', "default 必须是 options"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "bool"\ndefault = "yes"', "bool 的 default"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "tags"\ndefault = "a,b"', "tags 的 default"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "time"\ndefault = "8点"', "HH:MM"),
        ('[[config.fields]]\nkey = "a"\nlabel = "x"\ndepends_on = "ghost"', "depends_on 必须指向另一个 bool"),
    ],
)
def test_bad_declarations_are_reported_with_a_readable_reason(body, expected) -> None:
    _fields_found, errors = _fields("\n" + body)
    assert any(expected in message for message in errors), errors


# ---------- 发现:无效的清单不会拖垮整个扫描 ----------

@pytest.fixture()
def plugin_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins_dir", str(tmp_path))
    plugin_directory._SCAN_CACHE.clear()
    yield tmp_path
    plugin_directory._SCAN_CACHE.clear()


def _write_plugin(root: Path, code: str, manifest: str) -> None:
    folder = root / code
    folder.mkdir()
    (folder / "config.toml").write_text(manifest.replace('code = "demo"', f'code = "{code}"'), encoding="utf-8")
    (folder / "plugin.py").write_text("def execute_action(request):\n    return None\n", encoding="utf-8")


def test_a_broken_toml_marks_only_that_plugin_invalid(plugin_dir) -> None:
    _write_plugin(plugin_dir, "good", BASE + '\n[[config.fields]]\nkey = "a"\nlabel = "甲"\n')
    _write_plugin(plugin_dir, "broken", "[plugin\ncode = ")
    found = {item.manifest.code: item for item in plugin_directory.discover_plugins(force=True)}
    assert found["good"].is_valid and [item.key for item in found["good"].config_fields] == ["a"]
    assert not found["broken"].is_valid and "解析失败" in found["broken"].validation_errors[0]


def test_invalid_field_declarations_make_the_plugin_invalid(plugin_dir) -> None:
    _write_plugin(plugin_dir, "wrong", BASE + '\n[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "color"\n')
    (item,) = plugin_directory.discover_plugins(force=True)
    assert not item.is_valid and any("kind 非法" in message for message in item.validation_errors)


def test_a_secret_field_is_treated_as_a_secret_without_listing_it_twice(plugin_dir) -> None:
    _write_plugin(plugin_dir, "withsecret", BASE + '\n[secrets]\nfields = ["api_key"]\n\n[[config.fields]]\nkey = "api_key"\nlabel = "密钥"\nkind = "secret"\n\n[[config.fields]]\nkey = "token"\nlabel = "令牌"\nkind = "secret"\n')
    (item,) = plugin_directory.discover_plugins(force=True)
    assert item.secret_fields == ("api_key", "token")


# ---------- 声明 -> 配置表单 -> 保存 ----------

def _schema_of(plugin_dir_path: Path, code: str, body: str):
    _write_plugin(plugin_dir_path, code, BASE + body)
    discovered = {item.manifest.code: item for item in plugin_directory.discover_plugins(force=True)}[code]
    return registry._build_plugin_schema(discovered)


DECLARED = """
[[config.fields]]
key = "timeout"
label = "超时(秒)"
kind = "number"
default = 15
min = 5
max = 60
group = "接口"

[[config.fields]]
key = "ratio"
label = "比例"
kind = "number"
default = 0.5
min = 0.1
max = 0.9

[[config.fields]]
key = "at"
label = "推送时间"
kind = "time"
default = "08:00"

[[config.fields]]
key = "loud"
label = "高调模式"
kind = "bool"
default = false

[[config.fields]]
key = "words"
label = "词语"
kind = "tags"
default = ["甲"]

[[config.fields]]
key = "token"
label = "密钥"
kind = "secret"
"""


def test_the_schema_comes_from_the_declarations_not_from_guessing(plugin_dir) -> None:
    schema = _schema_of(plugin_dir, "declared", DECLARED)
    assert schema.schema_version == "v2" and schema.title == "示例 配置"
    by_name = {item.name: item for item in schema.fields}
    assert [item.name for item in schema.fields] == ["timeout", "ratio", "at", "loud", "words", "token"]  # 只显示声明的字段,顺序按声明
    assert (by_name["timeout"].label, by_name["timeout"].group, by_name["timeout"].minimum, by_name["timeout"].maximum) == ("超时(秒)", "接口", 5, 60)
    assert (by_name["loud"].field_type, by_name["words"].field_type, by_name["at"].field_type) == ("boolean", "list", "time")
    assert by_name["token"].sensitive and by_name["token"].field_type == "password"
    assert "Auto-generated" not in repr(schema)  # 没有机械生成的英文模板


def test_undeclared_plugins_still_get_the_inferred_form(plugin_dir) -> None:
    schema = _schema_of(plugin_dir, "legacy", "")
    assert schema.schema_version == "v1" and [item.name for item in schema.fields] == ["ratio", "commands", "require_mention"]


def _save(schema_plugin: str, form: list[tuple[str, str]], existing: str | None = None):
    return plugin_schemas.merge_schema_config(schema_plugin, FormData(form), existing_config_json=existing)


@pytest.fixture()
def declared(plugin_dir, monkeypatch):
    schema = _schema_of(plugin_dir, "declared", DECLARED)
    monkeypatch.setattr(plugin_schemas, "get_plugin_config_schema", lambda code: schema)
    return "declared"


def test_numbers_accept_decimals_and_are_clamped_to_the_declared_range(declared) -> None:
    import json

    saved = json.loads(_save(declared, [("schema__timeout", "999"), ("schema__ratio", "0.75")]).config_json)
    assert saved["timeout"] == 60 and saved["ratio"] == 0.75  # 超出上限被钳制,小数按小数保存
    saved = json.loads(_save(declared, [("schema__timeout", "1"), ("schema__ratio", "5")]).config_json)
    assert saved["timeout"] == 5 and saved["ratio"] == 0.9
    saved = json.loads(_save(declared, [("schema__timeout", "abc")]).config_json)
    assert saved["timeout"] == 15  # 不是数字:回到默认值,而不是存进乱写的内容


def test_a_bad_time_keeps_the_default_and_a_good_one_is_saved(declared) -> None:
    import json

    assert json.loads(_save(declared, [("schema__at", "9点半")]).config_json)["at"] == "08:00"
    assert json.loads(_save(declared, [("schema__at", "21:30")]).config_json)["at"] == "21:30"


def test_the_switch_posts_false_then_true_and_the_last_value_wins(declared) -> None:
    import json

    off = json.loads(_save(declared, [("schema__loud", "false")]).config_json)
    on = json.loads(_save(declared, [("schema__loud", "false"), ("schema__loud", "true")]).config_json)  # 勾选后:隐藏域在前、复选框在后
    assert off["loud"] is False and on["loud"] is True


def test_tags_are_split_on_commas_and_secrets_never_enter_the_plain_config(declared) -> None:
    import json

    result = _save(declared, [("schema__words", "甲， 乙,丙"), ("schema__token", "s3cret")])
    assert json.loads(result.config_json)["words"] == ["甲", "乙", "丙"]
    assert "token" not in json.loads(result.config_json) and result.secret_patch == {"token": "s3cret"}
    assert _save(declared, [("schema__token", "")]).secret_patch == {}  # 留空表示保持不变


# ---------- 真实页面 ----------

@pytest.fixture()
def pages(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db, force=True)
        weather = db.scalar(select(Plugin).where(Plugin.code == "weather"))
        legacy = db.scalar(select(Plugin).where(Plugin.code == "dota2_stats"))
        for plugin in (weather, legacy):
            db.add(BotPlugin(tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=plugin.id, status="enabled", runtime_status="running", config_json="{}"))
        db.commit()
        return client, int(weather.id), int(legacy.id)


def test_the_weather_plugin_shows_a_real_chinese_config_form(pages) -> None:
    client, weather_id, _legacy_id = pages
    _login(client, "admin")
    html = client.get(f"/plugins/{weather_id}").text
    for text in ("天气接口地址", "接口密钥", "请求超时(秒)", "默认城市", "每日订阅推送时间", "触发命令", "群里必须 @ 机器人才响应"):
        assert text in html, text
    for group in ("接口", "查询", "触发方式"):
        assert f"<h4>{group}</h4>" in html
    assert "Auto-generated" not in html and "Api Base" not in html and "Timeout Seconds" not in html  # 毛坯没有了
    assert 'min="5"' in html and 'max="60"' in html  # 声明的范围进了输入框
    assert 'type="time"' in html and 'role="switch"' in html


def test_an_undeclared_plugin_keeps_the_inferred_form(pages) -> None:
    client, _weather_id, legacy_id = pages
    _login(client, "admin")
    html = client.get(f"/plugins/{legacy_id}").text
    assert "Auto-generated from plugin config" in html  # 没声明的插件仍按默认值推断,不受影响


def test_every_bundled_manifest_is_valid_and_declared_defaults_match_the_matcher(monkeypatch) -> None:
    """随仓库发布的全部插件清单必须有效;声明了触发方式字段的,默认值必须和 [matcher] 一致,防止两处写岔。"""

    plugin_directory._SCAN_CACHE.clear()
    monkeypatch.setattr(settings, "plugins_dir", str(Path(__file__).resolve().parents[1] / "plugins"))
    found = plugin_directory.discover_plugins(force=True)
    assert found and [item.manifest.code for item in found if not item.is_valid] == []
    plugin_directory._SCAN_CACHE.clear()  # 不把这次扫描的结果留给后面的测试
    for item in found:
        declared = {field.key: field for field in item.config_fields}
        if "commands" in declared:
            assert list(declared["commands"].default) == list(item.matcher.commands), item.manifest.code
        if "keywords" in declared:
            assert list(declared["keywords"].default) == list(item.matcher.keywords), item.manifest.code
        if "require_mention" in declared:
            assert bool(declared["require_mention"].default) == bool(item.matcher.require_mention), item.manifest.code
