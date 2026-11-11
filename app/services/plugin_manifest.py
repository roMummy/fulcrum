"""插件清单 v2:用标准 TOML 解析,并把 [[config.fields]] 声明转换成配置字段。

清单是插件的"唯一来源":页面上的配置表单、校验、默认值都从这里的声明派生,插件不需要写模板。
旧的手写 TOML 子集解析器不支持 [[表数组]]、小数、多行字符串等,已经由标准解析器取代。

字段声明示例:

    [[config.fields]]
    key = "timeout_seconds"
    label = "请求超时(秒)"
    kind = "number"
    default = 15
    min = 5
    max = 60
    hint = "调用天气接口的最长等待时间"
    group = "接口"
"""

from __future__ import annotations

import re
from typing import Any

try:  # Python 3.11+ 自带;3.10 使用 requirements 里的 tomli
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅 3.10 走到
    import tomli as tomllib

from app.services.config_fields import FIELD_KINDS, ConfigField

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ManifestParseError(ValueError):
    """config.toml 不是合法的 TOML。"""


def parse_manifest(raw_text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestParseError(str(exc)) from exc


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_config_fields(parsed: dict[str, Any]) -> tuple[tuple[ConfigField, ...], list[str]]:
    """读取 [[config.fields]] 声明,返回 (字段, 错误列表)。没有声明时返回空元组:页面回退到按默认值推断。"""

    block = parsed.get("config")
    if block is None:
        return (), []
    if not isinstance(block, dict):
        return (), ["[config] 配置块类型非法。"]
    raw_fields = block.get("fields")
    if raw_fields is None:
        return (), []
    if not isinstance(raw_fields, list) or any(not isinstance(item, dict) for item in raw_fields):
        return (), ["config.fields 必须是 [[config.fields]] 表数组。"]

    errors: list[str] = []
    fields: list[ConfigField] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_fields, start=1):
        key = str(raw.get("key") or "").strip()
        name = f"字段 {key or '#' + str(index)}"
        if not KEY_PATTERN.match(key):
            errors.append(f"{name}: key 必须是小写字母开头的字母、数字、下划线(最长 64 位)。")
            continue
        if key in seen:
            errors.append(f"{name}: key 重复。")
            continue
        seen.add(key)
        label = str(raw.get("label") or "").strip()
        if not label:
            errors.append(f"{name}: 缺少 label(页面上显示的名称)。")
        kind = str(raw.get("kind") or "text").strip()
        if kind not in FIELD_KINDS:
            errors.append(f"{name}: kind 非法 {kind!r},可选 {', '.join(FIELD_KINDS)}。")
            continue
        options, option_errors = _read_options(raw.get("options"), name)
        errors += option_errors
        default = raw["default"] if "default" in raw else _default_for(kind)
        minimum, maximum = raw.get("min"), raw.get("max")
        errors += _check_field(name, kind, default, minimum, maximum, options)
        fields.append(
            ConfigField(
                key=key,
                label=label or key,
                kind=kind,
                default=default,
                hint=str(raw.get("hint") or "").strip(),
                minimum=minimum if _is_number(minimum) else None,
                maximum=maximum if _is_number(maximum) else None,
                group=str(raw.get("group") or "").strip(),
                depends_on=str(raw.get("depends_on") or "").strip(),
                label_en=str(raw.get("label_en") or "").strip(),
                placeholder=str(raw.get("placeholder") or "").strip(),
                required=bool(raw.get("required", False)),
                options=options,
            )
        )
    by_key = {item.key: item for item in fields}
    for item in fields:
        if item.depends_on and (item.depends_on not in by_key or by_key[item.depends_on].kind != "bool"):
            errors.append(f"字段 {item.key}: depends_on 必须指向另一个 bool 字段。")
    return tuple(fields), errors


def _default_for(kind: str) -> Any:
    return {"bool": False, "number": 0, "tags": []}.get(kind, "")


def _read_options(raw: Any, name: str) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """下拉选项:字符串列表(值即显示名),或 {value, label} 表的列表。"""

    if raw is None:
        return (), []
    if not isinstance(raw, list):
        return (), [f"{name}: options 必须是列表。"]
    options: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            options.append((item.strip(), item.strip()))
        elif isinstance(item, dict) and str(item.get("value") or "").strip():
            value = str(item["value"]).strip()
            options.append((value, str(item.get("label") or value).strip()))
        else:
            return (), [f"{name}: options 里的每一项必须是字符串或带 value 的表。"]
    return tuple(options), []


def _check_field(name: str, kind: str, default: Any, minimum: Any, maximum: Any, options: tuple[tuple[str, str], ...]) -> list[str]:
    errors: list[str] = []
    if kind == "secret" and default not in ("", None):
        errors.append(f"{name}: 密钥不能有默认值。")
    if kind == "bool" and not isinstance(default, bool):
        errors.append(f"{name}: bool 的 default 必须是 true/false。")
    if kind == "number":
        if not _is_number(default):
            errors.append(f"{name}: number 的 default 必须是数字。")
        for label, bound in (("min", minimum), ("max", maximum)):
            if bound is not None and not _is_number(bound):
                errors.append(f"{name}: {label} 必须是数字。")
        if _is_number(minimum) and _is_number(maximum) and minimum > maximum:
            errors.append(f"{name}: min 不能大于 max。")
        elif _is_number(default) and ((_is_number(minimum) and default < minimum) or (_is_number(maximum) and default > maximum)):
            errors.append(f"{name}: default 超出 min/max 范围。")
    if kind == "select":
        if not options:
            errors.append(f"{name}: select 必须提供 options。")
        elif str(default) not in {value for value, _label in options}:
            errors.append(f"{name}: default 必须是 options 里的某个值。")
    if kind == "tags" and not (isinstance(default, list) and all(isinstance(item, str) for item in default)):
        errors.append(f"{name}: tags 的 default 必须是字符串列表。")
    if kind == "time" and default not in ("", None) and not TIME_PATTERN.match(str(default)):
        errors.append(f"{name}: time 的 default 必须是 HH:MM。")
    if kind in {"text", "textarea", "url"} and not isinstance(default, str):
        errors.append(f"{name}: {kind} 的 default 必须是字符串。")
    return errors
