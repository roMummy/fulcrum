from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 通过显式依赖使用 tomli。
    import tomli as tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PROJECT_ROOT / "plugins"
# 私有插件目录(逗号分隔的目录名):只在内网部署里使用、不随公开仓库发布的插件。
# 这些目录存在时不计入目录总数,也不触发"未纳入确认范围"错误。
PRIVATE_PLUGIN_CODES = {item.strip() for item in os.environ.get("FULCRUM_PRIVATE_PLUGINS", "").split(",") if item.strip()}

# 42 个目标代码来自已经确认的 A-BOT 迁移台账。交付校验固定使用完整集合，
# 不允许因为某个插件暂时未配置第三方凭证就把目录从正式交付范围中排除。
TARGET_PLUGIN_CODES = {
    "ai_auto_response",
    "ai_gen_image",
    "codex_quota_allocator",
    "daily_news",
    "daily_ranking",
    "dify",
    "dota2_stats",
    "douyin_parser",
    "douyu",
    "duanju_search",
    "epic_free",
    "fun_command_play",
    "game_task",
    "github_opengraph",
    "global_news",
    "group_auto_invite",
    "group_virtual",
    "gscore_adapter",
    "guess_song",
    "jd_sign_token",
    "lucky_pot",
    "maibot_adapter",
    "member_context",
    "member_roast",
    "message_push_task",
    "message_sign",
    "message_summary",
    "music",
    "newapi_coupon",
    "point_trade",
    "robot_menu",
    "value_rank",
    "weather",
    "xiuxian",
}

# 这两个目录是 SaaS 自有业务，不属于 A-BOT 复刻范围，但仍必须满足同一目录契约。
SAAS_OWN_PLUGIN_CODES = {"customer_operations", "supplier_order_flow", "idiom_guess"}

# 排除项和平台原生能力不能重新以目录插件形式出现。平台管理、统计、运维和群级
# 作用域已经有唯一 SaaS 页面和服务入口，不保留 A-BOT 单机管理兼容目录。
FORBIDDEN_PLUGIN_CODES = {
    "plugin_manager",
    "stats_collector",
    "system_updater",
    "trendradar_permission",
}

FORBIDDEN_IMPORT_ROOTS = {"base", "db", "plugins", "utils"}
REQUIRED_ENTRYPOINTS = {"execute_action", "execute_message_action"}
ALLOWED_PLUGIN_TYPES = {"ai", "command", "message", "observer", "rule", "scheduled", "system", "timer", "cron"}


def _read_manifest(config_path: Path, plugin_code: str, errors: list[str]) -> dict[str, Any] | None:
    """解析目录 TOML，并把语法错误收敛为不包含原配置值的稳定错误。"""

    try:
        with config_path.open("rb") as source:
            parsed = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError):
        errors.append(f"{plugin_code}: config.toml 无法解析")
        return None
    if not isinstance(parsed, dict):
        errors.append(f"{plugin_code}: config.toml 顶层结构无效")
        return None
    return parsed


def _validate_manifest(plugin_code: str, parsed: dict[str, Any], errors: list[str]) -> None:
    """检查运行时真正依赖的目录声明，不允许目录名和 manifest 形成双重身份。"""

    plugin_section = parsed.get("plugin")
    if not isinstance(plugin_section, dict):
        errors.append(f"{plugin_code}: 缺少 plugin 声明")
        return
    if plugin_section.get("code") != plugin_code:
        errors.append(f"{plugin_code}: plugin.code 与目录名不一致")
    if not str(plugin_section.get("display_name") or "").strip():
        errors.append(f"{plugin_code}: 缺少 display_name")
    plugin_type = str(plugin_section.get("plugin_type") or "").strip().lower()
    if plugin_type not in ALLOWED_PLUGIN_TYPES:
        errors.append(f"{plugin_code}: plugin_type 无效")

    capability = parsed.get("capability")
    if not isinstance(capability, dict):
        errors.append(f"{plugin_code}: 缺少 capability 声明")
        return
    for field_name in ("channel_types", "chat_types", "message_types"):
        values = capability.get(field_name)
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item.strip() for item in values):
            errors.append(f"{plugin_code}: capability.{field_name} 无效")


def _validate_secret_defaults(plugin_code: str, parsed: dict[str, Any], errors: list[str]) -> None:
    """敏感字段必须显式声明且在运行时默认配置中保持为空。

    校验只报告插件代码和字段名，绝不把检测到的配置值拼入输出，避免交付脚本
    在 CI、终端历史或部署日志中二次泄露租户凭证。
    """

    secrets_section = parsed.get("secrets")
    if secrets_section is None:
        return
    if not isinstance(secrets_section, dict):
        errors.append(f"{plugin_code}: secrets 声明无效")
        return
    fields = secrets_section.get("fields")
    if not isinstance(fields, list) or not fields or not all(isinstance(item, str) and item.strip() for item in fields):
        errors.append(f"{plugin_code}: secrets.fields 无效")
        return

    normalized_fields = [str(item).strip() for item in fields]
    if len(normalized_fields) != len(set(normalized_fields)):
        errors.append(f"{plugin_code}: secrets.fields 存在重复字段")

    runtime = parsed.get("runtime")
    runtime_config = runtime if isinstance(runtime, dict) else {}
    for field_name in normalized_fields:
        if field_name not in runtime_config:
            errors.append(f"{plugin_code}: 敏感字段 {field_name} 缺少空默认声明")
            continue
        if runtime_config[field_name] not in (None, ""):
            errors.append(f"{plugin_code}: 敏感字段 {field_name} 默认值非空")


def _validate_python_entry(plugin_code: str, entrypoint_path: Path, errors: list[str]) -> None:
    """使用 AST 检查真实执行入口和 A-BOT 运行时依赖，不导入插件模块。"""

    try:
        source = entrypoint_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(entrypoint_path))
    except (OSError, SyntaxError, UnicodeError):
        errors.append(f"{plugin_code}: plugin.py 无法解析")
        return

    top_level_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if not top_level_functions & REQUIRED_ENTRYPOINTS:
        errors.append(f"{plugin_code}: 缺少真实执行入口")

    for node in ast.walk(tree):
        imported_roots: set[str] = set()
        if isinstance(node, ast.Import):
            imported_roots = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots = {node.module.split(".", 1)[0]}
        # 目录插件允许引用自身的纯业务模块和本项目共享边界工具；其他 `plugins.*`
        # 导入仍然视为跨插件耦合，避免重新形成 A-BOT 全局插件运行时。
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            allowed_internal = {
                f"plugins.{plugin_code}.service",
                f"plugins.{plugin_code}.parsers",
                f"plugins.{plugin_code}.a_bogus",
                "plugins._shared",
                "plugins._shared.media",
                "plugins._shared.resources",
            }
            if node.module in allowed_internal:
                imported_roots.discard("plugins")
        forbidden = imported_roots & FORBIDDEN_IMPORT_ROOTS
        for root_name in sorted(forbidden):
            errors.append(f"{plugin_code}: 禁止导入 A-BOT 运行时 {root_name}")


def verify_plugin_migration() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回当前目录代码和全部错误，调用方只负责输出脱敏汇总。"""

    errors: list[str] = []
    expected_codes = TARGET_PLUGIN_CODES | SAAS_OWN_PLUGIN_CODES
    # 只统计同时具备 manifest 和执行入口的真实目录插件；`plugins/_shared` 等
    # 共用代码目录不能被误计入插件目录总数，也不能因此触发未纳入范围错误。
    actual_codes = {
        path.name
        for path in PLUGINS_ROOT.iterdir()
        if path.is_dir() and path.name not in PRIVATE_PLUGIN_CODES and (path / "config.toml").is_file() and (path / "plugin.py").is_file()
    } if PLUGINS_ROOT.is_dir() else set()

    missing = expected_codes - actual_codes
    unexpected = actual_codes - expected_codes
    forbidden = actual_codes & FORBIDDEN_PLUGIN_CODES
    for plugin_code in sorted(missing):
        errors.append(f"{plugin_code}: 缺少插件目录")
    for plugin_code in sorted(unexpected):
        errors.append(f"{plugin_code}: 未纳入确认范围")
    for plugin_code in sorted(forbidden):
        errors.append(f"{plugin_code}: 排除或平台原生能力不得作为插件目录")

    if len(actual_codes) != len(expected_codes):
        errors.append(f"目录数量错误: 实际 {len(actual_codes)}，要求 {len(expected_codes)}")

    for plugin_code in sorted(actual_codes & expected_codes):
        plugin_dir = PLUGINS_ROOT / plugin_code
        config_path = plugin_dir / "config.toml"
        entrypoint_path = plugin_dir / "plugin.py"
        if not config_path.is_file():
            errors.append(f"{plugin_code}: 缺少 config.toml")
            continue
        if not entrypoint_path.is_file():
            errors.append(f"{plugin_code}: 缺少 plugin.py")
            continue
        if (plugin_dir / "main.py").exists() or (plugin_dir / "plugin_main.py").exists():
            errors.append(f"{plugin_code}: 存在废弃的兼容执行入口")

        parsed = _read_manifest(config_path, plugin_code, errors)
        if parsed is not None:
            _validate_manifest(plugin_code, parsed, errors)
            _validate_secret_defaults(plugin_code, parsed, errors)
        _validate_python_entry(plugin_code, entrypoint_path, errors)

    return tuple(sorted(actual_codes)), tuple(errors)


def main() -> int:
    """输出数量、插件代码和错误，不输出 TOML 值、环境变量或数据库内容。"""

    plugin_codes, errors = verify_plugin_migration()
    print(f"目录插件总数: {len(plugin_codes)}")
    print(f"A-BOT 目标插件数: {len(TARGET_PLUGIN_CODES)}")
    print(f"SaaS 自有插件数: {len(SAAS_OWN_PLUGIN_CODES)}")
    print(f"插件代码: {','.join(plugin_codes)}")
    print(f"错误数量: {len(errors)}")
    for error in errors:
        print(f"错误: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
