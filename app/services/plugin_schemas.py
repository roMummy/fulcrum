import json
from typing import Any, Mapping

from app.services.plugin_runtime.registry import PluginConfigSchema, get_plugin_schema
from app.services.security import mask_secret


def get_plugin_config_schema(plugin_code: str | None) -> PluginConfigSchema | None:
    return get_plugin_schema(plugin_code)


def parse_config_json(raw_config: str | None) -> dict[str, Any]:
    normalized = (raw_config or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_config_json(config: Mapping[str, Any]) -> str | None:
    compact = {
        key: value
        for key, value in config.items()
        if value not in (None, "", [])
    }
    if not compact:
        return None
    return json.dumps(compact, ensure_ascii=False, sort_keys=True)


def deep_merge_config(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = deep_merge_config(current, value)
        else:
            merged[key] = value
    return merged


def diff_config_override(base: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key, value in target.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            nested = diff_config_override(current, value)
            if nested:
                diff[key] = nested
        elif key not in base or current != value:
            diff[key] = value
    return diff


def build_plugin_config_view(plugin_code: str | None, raw_config: str | None) -> dict[str, Any]:
    schema = get_plugin_config_schema(plugin_code)
    normalized_raw = normalize_config_json(raw_config) or ""
    if not schema:
        return {
            "mode": "json",
            "plugin_code": (plugin_code or "").strip(),
            "title": "配置 JSON",
            "description": "当前插件未注册 schema，继续使用原始 JSON 编辑。",
            "fields": [],
            "preview_json": normalized_raw,
            "raw_config_json": normalized_raw,
            "masked_preview_json": normalized_raw,
        }

    current = parse_config_json(raw_config)
    fields: list[dict[str, Any]] = []
    masked_preview = dict(current)
    for item in schema.fields:
        value = current.get(item.name, item.default)
        masked_value = ""
        render_value: str = ""
        if item.field_type == "list":
            value = value if isinstance(value, list) else (item.default or [])
            render_value = ",".join(str(part) for part in value)
        elif item.field_type == "boolean":
            value = bool(value) if value is not None else bool(item.default)
            render_value = "true" if value else "false"
        elif item.field_type == "number":
            render_value = "" if value is None else str(value)
        else:
            render_value = "" if value is None else str(value)

        if item.sensitive:
            masked_value = mask_secret(render_value) if render_value else ""
            masked_preview[item.name] = masked_value if masked_value else None
            render_value = ""

        fields.append(
            {
                "name": item.name,
                "field_type": item.field_type,
                "label": item.label,
                "description": item.description,
                "default": item.default,
                "value": value,
                "render_value": render_value,
                "sensitive": item.sensitive,
                "placeholder": item.placeholder,
                "options": item.options,
                "masked_value": masked_value,
            }
        )

    return {
        "mode": "schema",
        "plugin_code": schema.plugin_code,
        "schema_version": schema.schema_version,
        "title": schema.title,
        "description": schema.description,
        "fields": fields,
        "preview_json": dump_config_json(masked_preview) or "{}",
        "raw_config_json": normalized_raw,
        "masked_preview_json": dump_config_json(masked_preview) or "{}",
    }


def merge_schema_config(
    plugin_code: str | None,
    submitted_form: Mapping[str, Any],
    *,
    existing_config_json: str | None = None,
    fallback_config_json: str | None = None,
) -> str | None:
    schema = get_plugin_config_schema(plugin_code)
    if not schema:
        return normalize_config_json(fallback_config_json)

    existing = parse_config_json(existing_config_json)
    has_schema_fields = any(f"schema__{item.name}" in submitted_form for item in schema.fields)
    if not has_schema_fields:
        return normalize_config_json(fallback_config_json) or dump_config_json(existing)

    merged = dict(existing)
    for item in schema.fields:
        key = f"schema__{item.name}"
        raw_value = submitted_form.get(key)
        if item.field_type == "boolean":
            merged[item.name] = _parse_boolean(raw_value, item.default)
            continue

        normalized = "" if raw_value is None else str(raw_value).strip()
        if item.sensitive:
            if normalized:
                merged[item.name] = normalized
            elif merged.get(item.name):
                continue
            elif item.default not in (None, ""):
                merged[item.name] = item.default
            else:
                merged.pop(item.name, None)
            continue

        parsed_value = _parse_field_value(item.field_type, normalized, item.default)
        if parsed_value in (None, "", []):
            merged.pop(item.name, None)
        else:
            merged[item.name] = parsed_value

    return dump_config_json(merged)


def build_schema_view(plugin_code: str | None, raw_config: str | None) -> dict[str, Any] | None:
    view = build_plugin_config_view(plugin_code, raw_config)
    if view["mode"] != "schema":
        return None
    return view


def normalize_config_json(raw_config: str | None) -> str | None:
    normalized = (raw_config or "").strip()
    if not normalized:
        return None
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return normalized
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def _parse_field_value(field_type: str, raw_value: str, default: Any) -> Any:
    if field_type == "number":
        if not raw_value:
            return default
        try:
            return int(raw_value)
        except ValueError:
            return default
    if field_type == "list":
        source = raw_value or ",".join(str(part) for part in (default or []))
        return [part.strip() for part in source.replace("，", ",").split(",") if part.strip()]
    if field_type == "select":
        return raw_value or default
    return raw_value or default


def _parse_boolean(raw_value: Any, default: Any) -> bool:
    if raw_value is None:
        return bool(default)
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}
