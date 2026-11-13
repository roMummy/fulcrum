"""插件目录页复用同次扫描结果，同时保留下一次读取的热更新能力。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_RUNTIME = Path(tempfile.mkdtemp(prefix="plugin-catalog-performance-"))
os.environ.setdefault("LOG_DIR", str(_TEST_RUNTIME / "logs"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_RUNTIME / 'app.db'}")

import pytest
from sqlalchemy import event, select

import app.main as main_module
from app.core.settings import settings
from app.models.entities import BotPlugin, Plugin
from app.services import plugin_directory
from app.services.plugin_runtime import registry
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture()
def plugin_catalog(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(settings, "plugins_dir", str(root))
    plugin_directory._SCAN_CACHE.clear()
    yield root
    plugin_directory._SCAN_CACHE.clear()


def _write_plugin(root: Path, code: str, source: str = "def execute_action(request):\n    return None\n") -> Path:
    folder = root / code
    folder.mkdir()
    folder.joinpath("config.toml").write_text(
        f'''[plugin]
code = "{code}"
display_name = "{code}"
[capability]
channel_types = ["simulator"]
chat_types = ["group"]
message_types = ["text"]
[matcher]
commands = ["原命令"]
''',
        encoding="utf-8",
    )
    folder.joinpath("plugin.py").write_text(source, encoding="utf-8")
    return folder


def _rewrite(path: Path, source: str) -> None:
    # 显式推进时间戳，避免 Windows 文件时间精度与 Python 字节码缓存影响热更新验证。
    changed_at = path.stat().st_mtime_ns + 2_000_000_000
    path.write_text(source, encoding="utf-8")
    os.utime(path, ns=(changed_at, changed_at))


def test_batch_metadata_matches_single_lookups_with_one_directory_scan(plugin_catalog, monkeypatch) -> None:
    _write_plugin(plugin_catalog, "good")
    broken = _write_plugin(plugin_catalog, "broken")
    broken.joinpath("config.toml").write_text("[plugin\ncode =", encoding="utf-8")
    _write_plugin(plugin_catalog, "empty", "NOT_A_PLUGIN = True\n")
    _write_plugin(plugin_catalog, "import_error", "raise RuntimeError('测试插件无法载入')\n")
    codes = ["good", "broken", "empty", "import_error", "missing"]
    expected = {code: registry.get_plugin_runtime_meta(code) for code in codes}
    scans = []
    real_signature = plugin_directory._build_scan_signature

    def signature(root):
        scans.append(root)
        return real_signature(root)

    monkeypatch.setattr(plugin_directory, "_build_scan_signature", signature)
    actual = registry.get_plugin_runtime_meta_map(iter(codes))

    assert actual == expected
    assert len(scans) == 1
    assert actual["good"]["supports_execution"] is True
    assert actual["broken"]["manifest_valid"] is False
    assert actual["empty"]["supports_execution"] is False
    assert actual["import_error"]["supports_execution"] is False
    assert actual["missing"]["migration_state"] == "undiscovered"


def test_next_batch_sees_manifest_entrypoint_addition_and_removal(plugin_catalog) -> None:
    folder = _write_plugin(plugin_catalog, "demo")
    assert registry.get_plugin_runtime_meta_map(["demo"])["demo"]["supports_execution"] is True

    manifest = folder / "config.toml"
    _rewrite(manifest, manifest.read_text(encoding="utf-8").replace("原命令", "新命令"))
    assert registry.get_plugin_runtime_meta_map(["demo"])["demo"]["commands"] == ["新命令"]

    entrypoint = folder / "plugin.py"
    _rewrite(entrypoint, "NOT_A_PLUGIN = True\n")
    assert registry.get_plugin_runtime_meta_map(["demo"])["demo"]["supports_execution"] is False

    _rewrite(entrypoint, "def execute_action(request):\n    return {'updated': True}\n")
    _write_plugin(plugin_catalog, "added")
    result = registry.get_plugin_runtime_meta_map(["demo", "added"])
    assert all(item["supports_execution"] for item in result.values())

    entrypoint.unlink()
    result = registry.get_plugin_runtime_meta_map(["demo", "added"])
    assert result["demo"]["migration_state"] == "undiscovered"
    assert result["added"]["supports_execution"] is True


def test_loading_discovered_plugin_skips_rediscovery_and_still_supports_force(plugin_catalog, monkeypatch) -> None:
    _write_plugin(plugin_catalog, "demo")
    (discovered,) = plugin_directory.discover_plugins()

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("已有目录记录时不应重新发现插件")

    monkeypatch.setattr(plugin_directory, "get_discovered_plugin", unexpected_lookup)
    loaded = plugin_directory.load_discovered_plugin(discovered)
    assert loaded is not None and loaded.instance is not None
    assert plugin_directory.load_discovered_plugin(discovered) is loaded
    assert plugin_directory.load_discovered_plugin(discovered, force=True) is not loaded


@pytest.mark.parametrize("available", [False, True])
def test_summary_reuses_supplied_metadata_even_when_empty(monkeypatch, available) -> None:
    def unexpected_lookup(*args, **kwargs):
        pytest.fail("已提供运行信息时不应重新扫描插件目录")

    monkeypatch.setattr(main_module, "get_plugin_runtime_meta", unexpected_lookup)
    monkeypatch.setattr(main_module, "get_plugin_runtime_meta_map", unexpected_lookup)
    runtime_map = {
        "demo": {"plugin_category": "functional", "migration_state": "migrated", "supports_execution": True}
    } if available else {}
    summary = main_module.build_plugin_runtime_summary(
        [Plugin(id=1, code="demo", display_name="示例")],
        [BotPlugin(status="enabled")],
        runtime_map=runtime_map,
    )
    assert summary["functional_total"] == int(available)
    assert summary["functional_executable"] == int(available)
    assert summary["bindings_total"] == summary["bindings_active"] == 1


def test_catalog_with_binding_does_not_prepare_detail_data_or_repeat_scans(
    provider_page_context, plugin_catalog, monkeypatch,
) -> None:
    client, ids = provider_page_context
    _write_plugin(plugin_catalog, "demo")
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db)
        plugin = db.scalar(select(Plugin).where(Plugin.code == "demo"))
        db.add(BotPlugin(
            tenant_id=ids["alpha"], bot_id=ids["bot"], plugin_id=plugin.id,
            status="enabled", runtime_status="running", secret_config_encrypted="unused-test-secret",
        ))
        db.commit()
    _login(client, "admin")

    def unexpected_detail_work(*args, **kwargs):
        pytest.fail("列表页不应解密绑定配置或整理详细调度记录")

    monkeypatch.setattr(main_module, "decrypt_plugin_secret_config", unexpected_detail_work)
    monkeypatch.setattr(main_module, "build_binding_dispatch_view", unexpected_detail_work)
    scans = []
    real_signature = plugin_directory._build_scan_signature

    def signature(root):
        scans.append(root)
        return real_signature(root)

    monkeypatch.setattr(plugin_directory, "_build_scan_signature", signature)
    queries = []
    engine = ids["factory"].kw["bind"]

    def record_query(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record_query)
    try:
        response = client.get("/plugins")
    finally:
        event.remove(engine, "before_cursor_execute", record_query)

    assert response.status_code == 200
    assert "当前启用 1 / 1" in response.text
    assert 'data-plugin-binding="bound"' in response.text
    assert 0 < len(scans) <= 2
    assert not any("raw_webhook_events" in query.lower() for query in queries)
