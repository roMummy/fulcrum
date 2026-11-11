"""文档中心的插件开发规范页与标准示例插件:示例必须始终能通过真实的安装校验并按文档所述工作。"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from test_developer_docs import _load_route, _principal, _request

_load_route()  # 先准备好独立的测试运行环境,再导入应用模块

from app.core.settings import settings  # noqa: E402
from app.services import plugin_directory, plugin_packages  # noqa: E402
from app.services.plugin_runtime.contracts import PluginExecutionRequest  # noqa: E402
from app.web import developer_docs  # noqa: E402


def _call(handler, path: str, logged_in: bool = True):
    request = _request(path)
    request.state.current_user = _principal() if logged_in else None
    return asyncio.run(handler(request))


def test_the_plugin_spec_page_requires_login_and_shows_the_spec_and_the_download() -> None:
    assert _call(developer_docs.plugin_docs_page, "/developer/docs/plugins", logged_in=False).status_code == 303
    response = _call(developer_docs.plugin_docs_page, "/developer/docs/plugins")
    html = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Fulcrum 插件开发规范" in html and "打包与交付" in html
    assert 'href="/developer/docs/plugins/hello_plugin.zip"' in html
    # 两个页签,当前页高亮
    assert 'class="section-tab active" href="/developer/docs/plugins"' in html and 'href="/developer/docs"' in html


def test_the_bridge_docs_page_gets_the_same_tabs() -> None:
    html = _call(developer_docs.developer_docs_page, "/developer/docs").body.decode("utf-8")
    assert 'class="section-tab active" href="/developer/docs"' in html and "插件开发规范" in html


def test_the_template_download_requires_login_and_is_a_zip_of_the_template_directory() -> None:
    assert _call(developer_docs.plugin_template_download, "/developer/docs/plugins/hello_plugin.zip", logged_in=False).status_code == 303
    response = _call(developer_docs.plugin_template_download, "/developer/docs/plugins/hello_plugin.zip")
    assert response.status_code == 200 and response.media_type == "application/zip"
    assert "hello_plugin.zip" in response.headers["content-disposition"]
    names = sorted(zipfile.ZipFile(io.BytesIO(response.body)).namelist())
    assert names == ["hello_plugin/README.md", "hello_plugin/config.toml", "hello_plugin/plugin.py"]


@pytest.fixture()
def installed_template(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins_dir", str(tmp_path))
    plugin_directory._SCAN_CACHE.clear()
    result = plugin_packages.install_or_upgrade(developer_docs.build_plugin_template_zip())  # 走真实的上传校验
    yield result
    plugin_directory._SCAN_CACHE.clear()


def test_the_template_passes_the_real_install_checks_and_its_declared_defaults_match(installed_template) -> None:
    assert (installed_template.action, installed_template.code) == ("install", "hello_plugin")
    (found,) = plugin_directory.discover_plugins(force=True)
    assert found.is_valid and not found.validation_errors
    declared = {field.key: field for field in found.config_fields}
    assert list(declared["commands"].default) == list(found.matcher.commands)
    assert bool(declared["require_mention"].default) == bool(found.matcher.require_mention)
    # 每个声明了的配置项在 [runtime] 里都有同样的默认值(触发方式字段除外)
    for key, field in declared.items():
        if key not in ("commands", "keywords", "require_mention"):
            assert found.runtime_config[key] == field.default, key


def _run(config: dict, command_args: str = "", action: str = "execute"):
    loaded = plugin_directory.load_directory_plugin("hello_plugin")
    return loaded.instance.execute_action(PluginExecutionRequest(action=action, config=config, payload={"command_args": command_args}))


def test_the_template_replies_according_to_its_config_and_survives_bad_values(installed_template) -> None:
    assert _run({}, action="reload").ok
    assert not _run({}, action="explode").ok
    result = _run({"greeting": "早", "times": 2, "loud": True, "signature": ["甲", "乙"]}, "小明")
    assert result.output["reply_text"] == "小明,早! 早!\n—— 甲、乙"
    # 配置被改坏时(类型错误、越界)不抛异常,回到安全值
    bad = _run({"greeting": "", "times": "abc", "style": "???", "signature": "不是列表"})
    assert bad.ok and bad.output["reply_text"] == "你好~"
    assert _run({"times": 999}).output["reply_text"].count("你好") == 5
