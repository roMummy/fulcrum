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
    # 以下由清单 v2 的 [[config.fields]] 声明提供;没有声明的插件这些保持默认,页面按默认值推断。
    group: str = ""
    minimum: int | float | None = None
    maximum: int | float | None = None
    required: bool = False
    depends_on: str = ""
    label_en: str = ""


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
    plugin_category: str
    migration_state: str
    execution_capability: str
    schema: PluginConfigSchema | None = None
    executor: PluginExecutor | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    admin_surfaces: tuple[str, ...] = field(default_factory=tuple)
    admin_extension: Mapping[str, Any] = field(default_factory=dict)

    @property
    def schema_version(self) -> str | None:
        return self.schema.schema_version if self.schema else None

    @property
    def supports_execution(self) -> bool:
        return self.executor is not None


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
            "keywords": [],
            "require_mention": False,
            "priority": None,
            "consume_policy": None,
            "timeout_ms": None,
            "config_path": None,
            "entrypoint_path": None,
            "manifest_valid": False,
            "manifest_errors": [],
            "manifest_warnings": [],
            "secret_fields": [],
            "admin_extension": {},
        }
    return {
        "plugin_code": discovered.manifest.code,
        "architecture_source": definition.architecture_source if definition else "invalid",
        "plugin_category": definition.plugin_category if definition else _derive_plugin_category(discovered),
        "migration_state": definition.migration_state if definition else "invalid",
        "execution_capability": definition.execution_capability if definition else "invalid",
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
        "keywords": list(discovered.matcher.keywords),
        "require_mention": discovered.matcher.require_mention,
        "priority": discovered.capability.priority,
        "consume_policy": discovered.capability.consume_policy,
        "timeout_ms": discovered.capability.timeout_ms,
        "config_path": str(discovered.config_path),
        "entrypoint_path": str(discovered.entrypoint_path),
        "manifest_valid": discovered.is_valid,
        "manifest_errors": list(discovered.validation_errors),
        "manifest_warnings": list(discovered.validation_warnings),
        "secret_fields": list(discovered.secret_fields),
    }


def list_plugin_runtime_definitions() -> tuple[PluginRuntimeDefinition, ...]:
    definitions = [_build_runtime_definition(item) for item in discover_plugins()]
    return tuple(sorted(definitions, key=lambda item: item.plugin_code))


def _build_runtime_definition(discovered: DiscoveredPlugin | None) -> PluginRuntimeDefinition | None:
    if not discovered:
        return None
    supports_execution = _supports_runtime_execution(discovered)
    # 无真实入口的目录不能继续生成可保存配置的表单，否则租户会看到一个能够
    # 绑定却永远不会执行的插件。只有完成 manifest 校验并成功加载入口后才
    # 提供 schema 和执行器，迁移状态也只保留 migrated/invalid 两种产品结果。
    schema = _build_plugin_schema(discovered) if supports_execution else None
    return PluginRuntimeDefinition(
        plugin_code=discovered.manifest.code,
        architecture_source="plugin_runtime" if supports_execution else "invalid",
        plugin_category=_derive_plugin_category(discovered),
        migration_state=_derive_migration_state(discovered, supports_execution),
        schema=schema,
        executor=DirectoryRuntimeExecutor(discovered.manifest.code) if supports_execution else None,
        execution_capability="reload,execute" if supports_execution else "invalid",
        capabilities=_derive_capabilities(discovered, supports_execution),
        admin_surfaces=_derive_admin_surfaces(discovered) if supports_execution else (),
        admin_extension=_derive_admin_extension(discovered) if supports_execution else {"enabled": False},
    )


def _supports_runtime_execution(discovered: DiscoveredPlugin) -> bool:
    if not discovered.is_valid:
        return False
    try:
        loaded = load_directory_plugin(discovered.manifest.code)
    except Exception:
        # 模块导入错误属于插件本身无可执行入口，运行定义必须标记 invalid，
        # 不能让插件管理页面因为单个目录损坏而整体返回 500。
        return False
    return bool(
        loaded
        and loaded.instance
        and (
            hasattr(loaded.instance, "execute_action")
            or hasattr(loaded.instance, "execute_message_action")
        )
    )


def _derive_plugin_category(discovered: DiscoveredPlugin) -> str:
    plugin_type = (discovered.manifest.plugin_type or "").strip().lower()
    if plugin_type in {"system", "scheduled", "timer", "cron"}:
        return "framework"
    return "functional"


def _derive_migration_state(discovered: DiscoveredPlugin, supports_execution: bool) -> str:
    return "migrated" if discovered.is_valid and supports_execution else "invalid"


def _derive_capabilities(discovered: DiscoveredPlugin, supports_execution: bool) -> tuple[str, ...]:
    if not supports_execution:
        return ("invalid",)
    items: list[str] = []
    items.extend(["reload", "execute", "schema-form"])
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
    已拆出的业务系统，例如供应商收发单，只能通过 Fulcrum Open API/Webhook 对接，避免把重业务页面重新塞回 Fulcrum。
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


# 清单里的字段类型 -> 配置表单的字段类型
_KIND_TO_FIELD_TYPE = {"text": "text", "textarea": "textarea", "number": "number", "bool": "boolean", "select": "select", "tags": "list", "url": "url", "time": "time", "secret": "password"}


def _schema_field_from_declaration(item: Any) -> PluginConfigField:
    """清单里声明的字段 -> 配置表单字段:标签、说明、范围、分组、下拉选项都来自声明,不再靠猜。"""

    return PluginConfigField(
        name=item.key,
        field_type=_KIND_TO_FIELD_TYPE[item.kind],
        label=item.label,
        description=item.hint,
        default=item.default,
        sensitive=item.kind == "secret",
        placeholder=item.placeholder,
        options=tuple(PluginConfigOption(value=value, label=label) for value, label in item.options),
        group=item.group,
        minimum=item.minimum,
        maximum=item.maximum,
        required=item.required,
        depends_on=item.depends_on,
        label_en=item.label_en,
    )


def _build_plugin_schema(discovered: DiscoveredPlugin) -> PluginConfigSchema:
    if discovered.config_fields:
        return PluginConfigSchema(
            plugin_code=discovered.manifest.code,
            schema_version="v2",
            title=f"{discovered.manifest.display_name} 配置",
            description=discovered.manifest.description or "",
            fields=tuple(_schema_field_from_declaration(item) for item in discovered.config_fields),
        )
    fields: list[PluginConfigField] = []
    merged_defaults = dict(discovered.runtime_config)
    if "commands" not in merged_defaults and discovered.matcher.commands:
        merged_defaults["commands"] = list(discovered.matcher.commands)
    if "keywords" not in merged_defaults and discovered.matcher.keywords:
        merged_defaults["keywords"] = list(discovered.matcher.keywords)
    if "require_mention" not in merged_defaults:
        merged_defaults["require_mention"] = discovered.matcher.require_mention

    # 敏感字段只以空默认值进入 schema。目录文件中的历史共享值即使尚未物理删除，
    # 也不会进入表单默认值、运行配置或页面预览。
    for secret_name in discovered.secret_fields:
        merged_defaults.setdefault(secret_name, "")

    explicit_secret_fields = set(discovered.secret_fields)
    for key, value in merged_defaults.items():
        fields.append(_build_config_field(key, value, sensitive=key in explicit_secret_fields))

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


def _build_config_field(name: str, default: Any, *, sensitive: bool) -> PluginConfigField:
    field_type = _infer_field_type(name, default, sensitive=sensitive)
    return PluginConfigField(
        name=name,
        field_type=field_type,
        label=_humanize_label(name),
        description=f"Auto-generated from plugin config for `{name}`.",
        default=default,
        sensitive=sensitive,
        placeholder=_build_placeholder(default),
    )


def _infer_field_type(name: str, value: Any, *, sensitive: bool) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, list):
        return "list"
    if sensitive:
        return "password"
    text = str(value or "")
    lowered_name = name.lower()
    if "\n" in text or any(hint in lowered_name for hint in LONG_TEXT_HINTS) or len(text) > 120:
        return "textarea"
    return "text"


def _humanize_label(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("-", "_").split("_") if part) or name


def _build_placeholder(default: Any) -> str:
    if default in (None, "", [], {}):
        return ""
    if isinstance(default, list):
        return ",".join(str(item) for item in default)
    return str(default)
