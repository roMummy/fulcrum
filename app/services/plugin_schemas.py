import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.services.plugin_runtime.registry import PluginConfigSchema, get_plugin_schema
from app.services.plugin_auto_revoke import MAX_DELAY_SECONDS, auto_revoke_policy, merge_auto_revoke_form


_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class PluginConfigMergeResult:
    """结构化配置保存结果，确保普通配置和敏感配置不会再次混入同一 JSON。"""

    config_json: str | None
    secret_patch: Mapping[str, Any] = field(default_factory=dict)
    clear_secret_fields: tuple[str, ...] = ()


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


def build_plugin_config_view(
    plugin_code: str | None,
    raw_config: str | None,
    *,
    secret_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    schema = get_plugin_config_schema(plugin_code)
    normalized_raw = normalize_config_json(raw_config) or ""
    # 平台发送策略独立于插件 schema，每个插件都可配置，不要求插件重复声明能力。
    delivery = {**auto_revoke_policy(parse_config_json(raw_config)), "maximum": MAX_DELAY_SECONDS}
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
            "delivery": delivery,
        }

    current = parse_config_json(raw_config)
    configured_secrets = dict(secret_config or {})
    # 历史普通 JSON 即使仍残留敏感字段，也不能进入页面预览或表单值。
    for item in schema.fields:
        if item.sensitive:
            current.pop(item.name, None)
    fields: list[dict[str, Any]] = []
    for item in schema.fields:
        value = "" if item.sensitive else current.get(item.name, item.default)
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
                "group": item.group,
                "minimum": item.minimum,
                "maximum": item.maximum,
                "required": item.required,
                "depends_on": item.depends_on,
                "secret_configured": item.name in configured_secrets and configured_secrets.get(item.name) not in (None, ""),
            }
        )

    return {
        "mode": "schema",
        "plugin_code": schema.plugin_code,
        "schema_version": schema.schema_version,
        "title": schema.title,
        "description": schema.description,
        "fields": fields,
        "preview_json": dump_config_json(current) or "{}",
        "raw_config_json": dump_config_json(current) or "",
        "delivery": delivery,
    }


def merge_schema_config(
    plugin_code: str | None,
    submitted_form: Mapping[str, Any],
    *,
    existing_config_json: str | None = None,
    fallback_config_json: str | None = None,
    existing_proxy_password: str = "",
) -> PluginConfigMergeResult:
    schema = get_plugin_config_schema(plugin_code)
    if not schema:
        config = parse_config_json(fallback_config_json if fallback_config_json is not None else existing_config_json)
        result = PluginConfigMergeResult(
            config_json=dump_config_json(merge_auto_revoke_form(config, submitted_form))
        )
        return _merge_proxy_fields(result, submitted_form, existing_proxy_password)

    existing = parse_config_json(existing_config_json)
    sensitive_names = {item.name for item in schema.fields if item.sensitive}
    # 普通配置读取阶段直接删除所有显式敏感字段，避免旧明文继续被保存或预览。
    for field_name in sensitive_names:
        existing.pop(field_name, None)

    has_schema_fields = any(
        f"schema__{item.name}" in submitted_form or f"schema_clear__{item.name}" in submitted_form
        for item in schema.fields
    )
    if not has_schema_fields:
        fallback = parse_config_json(fallback_config_json)
        for field_name in sensitive_names:
            fallback.pop(field_name, None)
        selected = fallback if fallback_config_json else existing
        result = PluginConfigMergeResult(
            config_json=dump_config_json(merge_auto_revoke_form(selected, submitted_form))
        )
        return _merge_proxy_fields(result, submitted_form, existing_proxy_password)

    merged = dict(existing)
    secret_patch: dict[str, Any] = {}
    clear_secret_fields: list[str] = []
    for item in schema.fields:
        key = f"schema__{item.name}"
        raw_value = submitted_form.get(key)
        if item.sensitive:
            # 敏感字段没有默认值语义：空输入保留现有密文，显式清除才删除，
            # 非空输入只进入 secret_patch，永远不进入普通 config_json。
            if _parse_boolean(submitted_form.get(f"schema_clear__{item.name}"), False):
                clear_secret_fields.append(item.name)
                continue
            normalized = "" if raw_value is None else str(raw_value).strip()
            if normalized:
                secret_patch[item.name] = normalized
            continue
        if item.field_type == "boolean":
            merged[item.name] = _parse_boolean(raw_value, item.default)
            continue

        normalized = "" if raw_value is None else str(raw_value).strip()
        if item.field_type == "select" and normalized not in {option.value for option in item.options}:
            # 下拉只能保存已声明的值；搜索文字或伪造的表单值不能变成配置。
            normalized = str(item.default)
        parsed_value = _parse_field_value(item.field_type, normalized, item.default, item.minimum, item.maximum)
        if parsed_value in (None, "", []):
            merged.pop(item.name, None)
        else:
            merged[item.name] = parsed_value

    result = PluginConfigMergeResult(
        config_json=dump_config_json(merge_auto_revoke_form(merged, submitted_form)),
        secret_patch=secret_patch,
        clear_secret_fields=tuple(clear_secret_fields),
    )
    return _merge_proxy_fields(result, submitted_form, existing_proxy_password)


def _merge_proxy_fields(
    result: PluginConfigMergeResult,
    submitted_form: Mapping[str, Any],
    existing_proxy_password: str,
) -> PluginConfigMergeResult:
    """把绑定页的代理字段并入普通配置和加密配置；表单没带代理字段时不改动。"""

    if not {"proxy_mode", "proxy_url", "proxy_username", "proxy_password", "proxy_bypass"} & set(submitted_form):
        return result
    from app.services.plugin_runtime.proxy import (
        PluginProxyConfigError,
        normalize_bypass,
        parse_proxy_config,
    )

    config = parse_config_json(result.config_json)
    # 代理配置改由独立字段保存；旧版单 URL 密文在保存时一并清理。
    config.pop("proxy_url", None)
    config.pop("proxy_username", None)
    proxy_mode = str(submitted_form.get("proxy_mode") or "").strip().lower()
    if proxy_mode and proxy_mode not in {"direct", "proxy"}:
        raise ValueError("代理模式不正确")
    proxy_url = str(submitted_form.get("proxy_url") or "").strip()
    proxy_username = str(submitted_form.get("proxy_username") or "").strip()
    proxy_password = str(submitted_form.get("proxy_password") or "")
    clear_proxy = _parse_boolean(submitted_form.get("clear_proxy_url"), False)
    # 未提交模式时保持旧表单语义：空地址或“清除”都按直连处理。
    direct_mode = proxy_mode == "direct" or (not proxy_mode and (clear_proxy or not proxy_url))
    if proxy_mode == "proxy" and not proxy_url:
        raise ValueError("选择“代理”后必须填写代理地址")
    if direct_mode:
        proxy_url = ""
    try:
        bypass = normalize_bypass(submitted_form.get("proxy_bypass"))
    except PluginProxyConfigError as exc:
        raise ValueError("代理绕过列表格式不正确") from exc

    secret_patch = dict(result.secret_patch)
    secret_patch.pop("proxy_url", None)
    clear_secret_fields = list(result.clear_secret_fields)
    if "proxy_url" not in clear_secret_fields:
        clear_secret_fields.append("proxy_url")

    if not proxy_url:
        config.pop("proxy_bypass", None)
        secret_patch.pop("proxy_password", None)
        if "proxy_password" not in clear_secret_fields:
            clear_secret_fields.append("proxy_password")
        return PluginConfigMergeResult(
            config_json=dump_config_json(config),
            secret_patch=secret_patch,
            clear_secret_fields=tuple(clear_secret_fields),
        )

    effective_password = proxy_password or (existing_proxy_password if proxy_username else "")
    try:
        parse_proxy_config(
            proxy_url,
            username=proxy_username,
            password=effective_password,
            bypass=bypass,
        )
    except PluginProxyConfigError as exc:
        raise ValueError(_proxy_error_message(str(exc))) from exc

    config["proxy_url"] = proxy_url
    if proxy_username:
        config["proxy_username"] = proxy_username
    if bypass:
        config["proxy_bypass"] = ",".join(bypass)
    else:
        config.pop("proxy_bypass", None)

    if proxy_username:
        if proxy_password:
            secret_patch["proxy_password"] = proxy_password
        elif existing_proxy_password:
            # 旧密文里的密码迁移到新的 proxy_password，避免清除旧 URL 后丢失。
            secret_patch["proxy_password"] = existing_proxy_password
        if "proxy_password" in clear_secret_fields:
            clear_secret_fields.remove("proxy_password")
    else:
        secret_patch.pop("proxy_password", None)
        if "proxy_password" not in clear_secret_fields:
            clear_secret_fields.append("proxy_password")
    return PluginConfigMergeResult(
        config_json=dump_config_json(config),
        secret_patch=secret_patch,
        clear_secret_fields=tuple(clear_secret_fields),
    )


def _proxy_error_message(code: str) -> str:
    messages = {
        "proxy_url_scheme_required": "代理地址需要以 http://、socks5:// 或 socks5h:// 开头",
        "proxy_url_scheme_invalid": "代理地址只支持 http、socks5、socks5h",
        "proxy_url_host_required": "代理地址缺少主机",
        "proxy_url_port_invalid": "代理端口不合法，只允许 1-65535",
        "proxy_url_credentials_incomplete": "代理用户名和密码必须同时填写",
        "proxy_url_credentials_inline": "代理账号密码请填写在下方用户名和密码输入框",
        "proxy_url_invalid": "代理地址不能包含路径、查询或片段",
    }
    return messages.get(code, "代理地址格式不正确")


def build_schema_view(
    plugin_code: str | None,
    raw_config: str | None,
    *,
    secret_config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    view = build_plugin_config_view(plugin_code, raw_config, secret_config=secret_config)
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


def _parse_field_value(field_type: str, raw_value: str, default: Any, minimum: Any = None, maximum: Any = None) -> Any:
    if field_type == "number":
        if not raw_value:
            return default
        try:
            # 带小数点的按小数保存,否则按整数;超出声明的范围时钳制到边界。
            value: int | float = float(raw_value) if any(mark in raw_value for mark in ".eE") else int(raw_value)
        except ValueError:
            return default
        if minimum is not None and value < minimum:
            value = minimum
        if maximum is not None and value > maximum:
            value = maximum
        return value
    if field_type == "time":
        # HH:MM;格式不对时保留默认,不把乱写的内容存进配置
        return raw_value if _TIME_PATTERN.match(raw_value or "") else default
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
