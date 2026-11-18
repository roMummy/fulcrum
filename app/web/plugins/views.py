"""插件页面的配置卡片、筛选项与运行摘要，复用公共运行时服务。"""

from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Plugin
from app.services.plugin_runtime import get_plugin_runtime_meta, get_plugin_runtime_meta_map
from app.services.plugin_schemas import build_plugin_config_view, dump_config_json, parse_config_json
from app.services.plugin_secrets import decrypt_plugin_secret_config
from app.services.runtime import build_binding_dispatch_view, build_binding_runtime_view


def build_binding_schema_cards(bindings: list[BotPlugin]) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for binding in bindings:
        plugin_code = binding.plugin.code if binding.plugin else None
        # 页面只需要知道哪些敏感字段已经配置。服务端在可信绑定范围内解密后仅传
        # 布尔状态给模板，密钥原文不会进入 HTML、JSON 预览或浏览器表单值。
        secret_config = decrypt_plugin_secret_config(
            tenant_id=int(binding.tenant_id),
            binding_id=int(binding.id),
            encrypted=binding.secret_config_encrypted,
        )
        config_data = parse_config_json(binding.config_json)
        proxy_bypass = str(config_data.pop("proxy_bypass", "") or "").strip()
        proxy_url = str(config_data.pop("proxy_url", "") or "").strip()
        proxy_username = str(config_data.pop("proxy_username", "") or "").strip()
        # 代理配置只由独立区域展示；原始 JSON 预览和应用配置不再重复暴露。
        config_view = build_plugin_config_view(
            plugin_code,
            dump_config_json(config_data),
            secret_config=secret_config,
        )
        cards.append(
            {
                "binding": binding,
                "config_view": config_view,
                "proxy_view": _proxy_view(
                    proxy_url=proxy_url,
                    proxy_username=proxy_username,
                    proxy_bypass=proxy_bypass,
                    secret_config=secret_config,
                ),
                "has_schema": config_view["mode"] == "schema",
                "runtime_info": get_plugin_runtime_meta(plugin_code),
                "runtime_view": build_binding_runtime_view(binding),
            }
        )
    return cards


def _proxy_view(
    *,
    proxy_url: str,
    proxy_username: str,
    proxy_bypass: str,
    secret_config: Mapping[str, Any],
) -> dict[str, object]:
    """向页面提供可编辑的代理地址、用户名和是否已保存密码。"""

    view: dict[str, object] = {
        "mode": "proxy" if proxy_url else "direct",
        "has_proxy": bool(proxy_url),
        "url": proxy_url,
        "username": proxy_username,
        "has_password": bool(secret_config.get("proxy_password")),
        "bypass": proxy_bypass,
    }
    if proxy_url:
        return view
    # 兼容旧的单 URL 密文格式：展示脱敏地址，保存时会迁移为拆分字段。
    legacy_url = str(secret_config.get("proxy_url") or "").strip()
    if not legacy_url:
        return view
    from app.services.plugin_runtime.proxy import PluginProxyConfigError, parse_proxy_url

    try:
        endpoint = parse_proxy_url(legacy_url, bypass=proxy_bypass)
    except PluginProxyConfigError:
        return {**view, "mode": "proxy", "has_proxy": True, "url": ""}
    if endpoint is None:
        return view
    return {
        **view,
        "mode": "proxy",
        "has_proxy": True,
        "url": endpoint.safe_label,
        "username": endpoint.username,
        "has_password": bool(endpoint.password),
    }


def build_binding_schema_cards_with_dispatch(db: Session, bindings: list[BotPlugin]) -> list[dict[str, object]]:
    cards = build_binding_schema_cards(bindings)
    for card, binding in zip(cards, bindings):
        card["dispatch_view"] = build_binding_dispatch_view(db, binding)
    return cards


def build_plugin_runtime_summary(
    plugins: list[Plugin],
    bindings: list[BotPlugin],
    *,
    runtime_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    if runtime_map is None:
        runtime_map = get_plugin_runtime_meta_map(plugin.code for plugin in plugins)
    functional_plugins = [plugin for plugin in plugins if runtime_map.get(plugin.code, {}).get("plugin_category") == "functional"]
    migrated_functional = [
        plugin for plugin in functional_plugins if runtime_map.get(plugin.code, {}).get("migration_state") == "migrated"
    ]
    executable_functional = [
        plugin for plugin in migrated_functional if runtime_map.get(plugin.code, {}).get("supports_execution")
    ]
    active_bindings = [binding for binding in bindings if binding.status == "enabled"]
    invalid_manifests = [plugin for plugin in plugins if runtime_map.get(plugin.code, {}).get("manifest_valid") is False]
    warning_manifests = [
        plugin
        for plugin in plugins
        if runtime_map.get(plugin.code, {}).get("manifest_valid") is not False
        and runtime_map.get(plugin.code, {}).get("manifest_warnings")
    ]
    return {
        "functional_total": len(functional_plugins),
        "functional_migrated": len(migrated_functional),
        "functional_executable": len(executable_functional),
        "bindings_total": len(bindings),
        "bindings_active": len(active_bindings),
        "manifest_invalid": len(invalid_manifests),
        "manifest_warning": len(warning_manifests),
    }


def plugin_matches_filters(
    plugin: Plugin,
    runtime_info: dict[str, object],
    *,
    keyword: str = "",
    plugin_type: str = "",
    category: str = "",
    migration_state: str = "",
    execution_state: str = "",
    manifest_state: str = "",
) -> bool:
    normalized_keyword = keyword.strip().lower()
    if normalized_keyword:
        haystack = " ".join(
            [
                str(plugin.code or ""),
                str(plugin.display_name or ""),
                str(plugin.description or ""),
                str(plugin.author or ""),
                str(plugin.plugin_type or ""),
                str(runtime_info.get("plugin_category") or ""),
                str(runtime_info.get("migration_state") or ""),
            ]
        ).lower()
        if normalized_keyword not in haystack:
            return False

    if plugin_type and (plugin.plugin_type or "") != plugin_type:
        return False
    if category and str(runtime_info.get("plugin_category") or "") != category:
        return False
    if migration_state and str(runtime_info.get("migration_state") or "") != migration_state:
        return False

    supports_execution = bool(runtime_info.get("supports_execution"))
    if execution_state == "executable" and not supports_execution:
        return False
    if execution_state == "non-executable" and supports_execution:
        return False

    manifest_valid = runtime_info.get("manifest_valid")
    has_warnings = bool(runtime_info.get("manifest_warnings"))
    if manifest_state == "valid" and manifest_valid is not True:
        return False
    if manifest_state == "invalid" and manifest_valid is not False:
        return False
    if manifest_state == "warning" and not (manifest_valid is not False and has_warnings):
        return False

    return True


def build_plugin_filter_options(
    plugins: list[Plugin], plugin_runtime_map: dict[str, dict[str, object]]
) -> dict[str, list[tuple[str, str]]]:
    plugin_types = sorted({(plugin.plugin_type or "").strip() for plugin in plugins if (plugin.plugin_type or "").strip()})
    categories = sorted(
        {
            str(plugin_runtime_map.get(plugin.code, {}).get("plugin_category") or "").strip()
            for plugin in plugins
            if str(plugin_runtime_map.get(plugin.code, {}).get("plugin_category") or "").strip()
        }
    )
    migration_states = sorted(
        {
            str(plugin_runtime_map.get(plugin.code, {}).get("migration_state") or "").strip()
            for plugin in plugins
            if str(plugin_runtime_map.get(plugin.code, {}).get("migration_state") or "").strip()
        }
    )
    return {
        "plugin_types": [("", "全部")] + [(item, item) for item in plugin_types],
        "categories": [("", "全部")] + [(item, item) for item in categories],
        "migration_states": [("", "全部")] + [(item, item) for item in migration_states],
        "execution_states": [("", "全部"), ("executable", "可执行"), ("non-executable", "不可执行")],
        "manifest_states": [("", "全部"), ("valid", "有效"), ("invalid", "无效"), ("warning", "警告")],
    }
