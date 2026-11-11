from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-native-boundaries"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app import main as main_module
from app.core.settings import settings
from app.models.entities import Bot, Plugin
from app.services.plugin_directory import discover_plugins
from app.services.plugin_runtime.registry import get_plugin_runtime_meta, list_plugin_runtime_definitions


PLATFORM_NATIVE_CODES = {
    "plugin_manager",
    "stats_collector",
    "system_updater",
    "trendradar_permission",
}


def test_runtime_has_no_placeholder_or_migration_intermediate_states() -> None:
    """目录运行时只允许真实 migrated 或 invalid 结果，不再提供占位成功响应。"""

    executor_source = (PROJECT_ROOT / "app/services/plugin_runtime/executors.py").read_text(encoding="utf-8")
    definitions = list_plugin_runtime_definitions()

    assert "PlaceholderExecutor" not in executor_source
    assert "scaffolded" not in executor_source
    assert 'status="stub"' not in executor_source
    assert definitions
    assert {item.migration_state for item in definitions} <= {"migrated", "invalid"}
    assert all(item.migration_state == "migrated" for item in definitions)
    assert all(item.supports_execution for item in definitions)


def test_manifest_without_real_entry_is_invalid_and_not_bindable(tmp_path: Path, monkeypatch) -> None:
    """结构合法但没有执行函数的目录不能生成配置表单或出现在机器人开关中。"""

    plugin_root = tmp_path / "plugins"
    plugin_dir = plugin_root / "empty_runtime"
    plugin_dir.mkdir(parents=True)
    plugin_dir.joinpath("config.toml").write_text(
        """
[plugin]
code = "empty_runtime"
display_name = "空运行插件"
plugin_type = "command"

[capability]
channel_types = ["telegram"]
chat_types = ["group"]
message_types = ["text"]
consume_policy = "continue"

[matcher]
commands = ["空命令"]

[runtime]
enabled = true
""".strip(),
        encoding="utf-8",
    )
    plugin_dir.joinpath("plugin.py").write_text("PLUGIN_NAME = 'empty_runtime'\n", encoding="utf-8")
    monkeypatch.setattr(settings, "plugins_dir", str(plugin_root))
    discover_plugins(force=True)

    runtime_info = get_plugin_runtime_meta("empty_runtime")
    matrix = main_module.build_plugin_toggle_matrix(
        [Bot(id=1, tenant_id=1, name="测试机器人")],
        [Plugin(id=1, code="empty_runtime", display_name="空运行插件", plugin_type="command")],
        [],
        {"empty_runtime": dict(runtime_info)},
    )

    assert runtime_info["manifest_valid"] is True
    assert runtime_info["migration_state"] == "invalid"
    assert runtime_info["supports_execution"] is False
    assert runtime_info["schema_version"] is None
    assert matrix == [
        {
            "bot_id": 1,
            "bot_name": "测试机器人",
            "enabled_count": 0,
            "total_count": 0,
            "items": [],
        }
    ]


def test_platform_native_capabilities_do_not_reappear_as_plugins() -> None:
    """平台管理、统计、运维和群作用域继续由现有 SaaS 页面承担。"""

    plugin_codes = {item.manifest.code for item in discover_plugins(force=True)}
    route_paths: set[str] = set()
    for route in main_module.app.routes:
        path = getattr(route, "path", None)
        if path is not None:
            route_paths.add(str(path))
        # 当前应用通过带作用域上下文的 IncludedRouter 保存 APIRouter，平台页面
        # 位于 original_router.routes；测试需要读取真实嵌套路由而不是假设全部扁平。
        original_router = getattr(route, "original_router", None)
        for nested in getattr(original_router, "routes", ()):
            nested_path = getattr(nested, "path", None)
            if nested_path is not None:
                route_paths.add(str(nested_path))

    assert not plugin_codes & PLATFORM_NATIVE_CODES
    assert {
        "/plugins",
        "/plugins/{plugin_id}",
        "/platform/dashboard",
        "/platform/operations",
        "/platform/logs",
    } <= route_paths
    assert any("/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/" in path for path in route_paths)


def test_plugin_pages_do_not_offer_invalid_binding_or_configuration_only_copy() -> None:
    """损坏插件页面显示需要处理，且不会继续提供可误导的绑定入口。"""

    list_template = (PROJECT_ROOT / "app/templates/plugins.html").read_text(encoding="utf-8")
    detail_template = (PROJECT_ROOT / "app/templates/plugin_detail.html").read_text(encoding="utf-8")

    # 插件目录页采用紧凑列表:只在有问题时显示"暂不可用/有提示"徽标(不再给每个插件挂"可执行"),
    # 损坏的插件(manifest 有错误)明确提示不可用;详情页只有能执行的插件才提供"绑定到机器人"入口。
    assert "runtime_info.manifest_errors" in list_template and "暂不可用" in list_template
    assert "{% if plugin_runtime_info.supports_execution %}" in detail_template
    assert "仅配置型" not in detail_template and "仅配置型" not in list_template
