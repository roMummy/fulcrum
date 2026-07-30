from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.services.plugin_directory import (
    DiscoveredPlugin,
    discover_plugins,
    get_discovered_plugin,
    load_directory_plugin,
)
from app.services.plugin_runtime.contracts import PluginExecutor
from app.services.plugin_runtime.executors import DirectoryRuntimeExecutor


@dataclass(frozen=True)
class PluginConfigOption:
    value: str
    label: str


@dataclass(frozen=True)
class PluginConfigField:
    name: str
    field_type: str
    label: str
    description: str = ""
    default: Any = None
    sensitive: bool = False
    placeholder: str = ""
    options: tuple[PluginConfigOption, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PluginConfigSchema:
    plugin_code: str
    schema_version: str
    title: str
    description: str = ""
    fields: tuple[PluginConfigField, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PluginRuntimeDefinition:
    plugin_code: str
    architecture_source: str
    plugin_category: str = "functional"
    migration_state: str = "planned"
    schema: PluginConfigSchema | None = None
    executor: PluginExecutor | None = None
    execution_capability: str = "schema-only"
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    admin_surfaces: tuple[str, ...] = field(default_factory=tuple)
    admin_extension: Mapping[str, Any] = field(default_factory=dict)

    @property
    def schema_version(self) -> str | None:
        return self.schema.schema_version if self.schema else None

    @property
    def supports_execution(self) -> bool:
        return self.executor is not None


SENSITIVE_KEYWORDS = ("api_key", "apikey", "secret", "token", "password", "authorization", "auth_key", "access_key")
LONG_TEXT_HINTS = ("prompt", "notes", "message", "content", "template", "instruction", "description")


def get_plugin_runtime_definition(plugin_code: str | None) -> PluginRuntimeDefinition | None:
    discovered = get_discovered_plugin(plugin_code)
    if not discovered:
        return None
    return _build_runtime_definition(discovered)


def get_plugin_schema(plugin_code: str | None) -> PluginConfigSchema | None:
    definition = get_plugin_runtime_definition(plugin_code)
    return definition.schema if definition else None


def get_plugin_runtime_meta(plugin_code: str | None) -> Mapping[str, Any]:
    discovered = get_discovered_plugin(plugin_code)
    definition = _build_runtime_definition(discovered) if discovered else None
    if not discovered:
        return {
            "plugin_code": (plugin_code or "").strip(),
            "architecture_source": "undiscovered",
            "plugin_category": "unknown",
            "migration_state": "undiscovered",
            "execution_capability": "unknown",
            "schema_version": None,
            "supports_execution": False,
            "catalog_source": "undiscovered",
            "channel_types": [],
            "chat_types": [],
            "message_types": [],
            "commands": [],
            "require_mention": False,
            "priority": None,
            "consume_policy": None,
            "timeout_ms": None,
            "config_path": None,
            "entrypoint_path": None,
            "manifest_valid": False,
            "manifest_errors": [],
            "manifest_warnings": [],
            "admin_extension": {},
        }
    return {
        "plugin_code": discovered.manifest.code,
        "architecture_source": definition.architecture_source if definition else "directory",
        "plugin_category": definition.plugin_category if definition else _derive_plugin_category(discovered),
        "migration_state": definition.migration_state if definition else "discovered",
        "execution_capability": definition.execution_capability if definition else "manifest-only",
        "capabilities": definition.capabilities if definition else (),
        "admin_surfaces": definition.admin_surfaces if definition else (),
        "admin_extension": dict(definition.admin_extension) if definition else {},
        "schema_version": definition.schema_version if definition else None,
        "supports_execution": definition.supports_execution if definition else False,
        "catalog_source": "directory",
        "channel_types": list(discovered.capability.channel_types),
        "chat_types": list(discovered.capability.chat_types),
        "message_types": list(discovered.capability.message_types),
        "commands": list(discovered.matcher.commands),
        "require_mention": discovered.matcher.require_mention,
        "priority": discovered.capability.priority,
        "consume_policy": discovered.capability.consume_policy,
        "timeout_ms": discovered.capability.timeout_ms,
        "config_path": str(discovered.config_path),
        "entrypoint_path": str(discovered.entrypoint_path),
        "manifest_valid": discovered.is_valid,
        "manifest_errors": list(discovered.validation_errors),
        "manifest_warnings": list(discovered.validation_warnings),
    }


def list_plugin_runtime_definitions() -> tuple[PluginRuntimeDefinition, ...]:
    definitions = [_build_runtime_definition(item) for item in discover_plugins()]
    return tuple(sorted(definitions, key=lambda item: item.plugin_code))


def _build_runtime_definition(discovered: DiscoveredPlugin | None) -> PluginRuntimeDefinition | None:
    if not discovered:
        return None
    supports_execution = _supports_runtime_execution(discovered)
    schema = _build_plugin_schema(discovered)
    return PluginRuntimeDefinition(
        plugin_code=discovered.manifest.code,
        architecture_source="plugin_runtime" if supports_execution else "directory",
        plugin_category=_derive_plugin_category(discovered),
        migration_state=_derive_migration_state(discovered, supports_execution),
        schema=schema,
        executor=DirectoryRuntimeExecutor(discovered.manifest.code) if supports_execution else None,
        execution_capability="reload,execute" if supports_execution else "schema-only",
        capabilities=_derive_capabilities(discovered, supports_execution),
        admin_surfaces=_derive_admin_surfaces(discovered),
        admin_extension=_derive_admin_extension(discovered),
    )


def _supports_runtime_execution(discovered: DiscoveredPlugin) -> bool:
    if not discovered.is_valid:
        return False
    loaded = load_directory_plugin(discovered.manifest.code)
    return bool(loaded and loaded.instance and hasattr(loaded.instance, "execute_action"))


def _derive_plugin_category(discovered: DiscoveredPlugin) -> str:
    plugin_type = (discovered.manifest.plugin_type or "").strip().lower()
    if plugin_type in {"system", "scheduled", "timer", "cron"}:
        return "framework"
    return "functional"


def _derive_migration_state(discovered: DiscoveredPlugin, supports_execution: bool) -> str:
    if not discovered.is_valid:
        return "invalid"
    if supports_execution:
        return "migrated"
    return "discovered"


def _derive_capabilities(discovered: DiscoveredPlugin, supports_execution: bool) -> tuple[str, ...]:
    items: list[str] = []
    if supports_execution:
        items.extend(["reload", "execute", "schema-form"])
    else:
        items.append("schema-form")
    plugin_type = (discovered.manifest.plugin_type or "").strip().lower()
    if plugin_type:
        items.append(plugin_type)
    if discovered.matcher.commands:
        items.append("command-match")
    if discovered.matcher.keywords:
        items.append("keyword-match")
    if discovered.matcher.require_mention:
        items.append("mention-required")
    if any(channel == "wechat_ipad" for channel in discovered.capability.channel_types):
        items.append("wechat-ipad")
    return tuple(dict.fromkeys(items))


def _derive_admin_extension(discovered: DiscoveredPlugin) -> dict[str, Any]:
    """规范化插件后台入口声明。

    这里刻意只暴露“入口元数据”，不允许目录插件动态注册后端路由。
    已拆出的业务系统，例如供应商收发单，只能通过 MBA Open API/Webhook 对接，避免把重业务页面重新塞回 MBA。
    """
    raw = dict(getattr(discovered, "admin_config", {}) or {})
    if not bool(raw.get("enabled", False)):
        return {"enabled": False}

    title = str(raw.get("title") or discovered.manifest.display_name or discovered.manifest.code).strip()
    entry = str(raw.get("entry") or "dashboard").strip() or "dashboard"
    base_path = str(raw.get("base_path") or f"/plugin-admin/{discovered.manifest.code}").strip()
    nav_items = raw.get("nav_items")
    normalized_nav = tuple(str(item).strip() for item in nav_items if str(item).strip()) if isinstance(nav_items, list) else ()
    return {
        "enabled": True,
        "title": title,
        "entry": entry,
        "base_path": base_path,
        "nav_items": normalized_nav,
    }


def _derive_admin_surfaces(discovered: DiscoveredPlugin) -> tuple[str, ...]:
    surfaces = ["plugins", "plugin-detail", "plugin-runtime"]
    admin_extension = _derive_admin_extension(discovered)
    if admin_extension.get("enabled"):
        surfaces.append(str(admin_extension.get("base_path") or f"/plugin-admin/{discovered.manifest.code}"))
    return tuple(surfaces)


def _build_plugin_schema(discovered: DiscoveredPlugin) -> PluginConfigSchema:
    fields: list[PluginConfigField] = []
    merged_defaults = dict(discovered.runtime_config)
    if "commands" not in merged_defaults and discovered.matcher.commands:
        merged_defaults["commands"] = list(discovered.matcher.commands)
    if "keywords" not in merged_defaults and discovered.matcher.keywords:
        merged_defaults["keywords"] = list(discovered.matcher.keywords)
    if "require_mention" not in merged_defaults:
        merged_defaults["require_mention"] = discovered.matcher.require_mention

    for key, value in merged_defaults.items():
        fields.append(_build_config_field(key, value))

    if not fields:
        fields.append(
            PluginConfigField(
                name="notes",
                field_type="textarea",
                label="Notes",
                description="Optional binding-level notes for this plugin.",
                placeholder="notes",
            )
        )

    return PluginConfigSchema(
        plugin_code=discovered.manifest.code,
        schema_version="v1",
        title=f"{discovered.manifest.display_name} Runtime",
        description=discovered.manifest.description or f"Auto-generated runtime schema for {discovered.manifest.code}.",
        fields=tuple(fields),
    )


def _build_config_field(name: str, default: Any) -> PluginConfigField:
    field_type = _infer_field_type(name, default)
    return PluginConfigField(
        name=name,
        field_type=field_type,
        label=_humanize_label(name),
        description=f"Auto-generated from plugin config for `{name}`.",
        default=default,
        sensitive=_is_sensitive_field(name),
        placeholder=_build_placeholder(default),
    )


def _infer_field_type(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, list):
        return "list"
    if _is_sensitive_field(name):
        return "password"
    text = str(value or "")
    lowered_name = name.lower()
    if "\n" in text or any(hint in lowered_name for hint in LONG_TEXT_HINTS) or len(text) > 120:
        return "textarea"
    return "text"


def _is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in SENSITIVE_KEYWORDS)


def _humanize_label(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part) or name


def _build_placeholder(default: Any) -> str:
    if default in (None, "", [], {}):
        return ""
    if isinstance(default, list):
        return ",".join(str(item) for item in default)
    return str(default)
