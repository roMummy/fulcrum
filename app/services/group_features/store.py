"""群功能的开关与配置读写。

数据放在 Chatroom.extra_json 的 "group_features" 键下,结构:
    {"group_features": {"<feature_key>": {"enabled": true, "config": {...}}}}
不新增表,也不碰 extra_json 里其他键(如 auto_download_media)。
"""

from __future__ import annotations

import json
from typing import Any

from app.models.entities import Chatroom
from app.services.security import encrypt_secret
from app.services.config_fields import ConfigField
from app.services.group_features.registry import GroupFeature, get_feature

EXTRA_KEY = "group_features"


def _load_extra(chatroom: Chatroom) -> dict[str, Any]:
    try:
        parsed = json.loads(chatroom.extra_json or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce(field: ConfigField, raw: Any) -> Any:
    """按配置项类型规整取值;非法输入回落到默认值,保证存进去的永远可用。"""

    if field.kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "on", "yes"}
        return bool(raw)
    if field.kind == "number":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return field.default
        if field.minimum is not None:
            value = max(field.minimum, value)
        if field.maximum is not None:
            value = min(field.maximum, value)
        return value
    return str(raw if raw is not None else field.default).strip()


def get_state(chatroom: Chatroom, feature: GroupFeature) -> tuple[bool, dict[str, Any]]:
    """返回 (是否启用, 生效配置);配置缺省项用默认值补齐。"""

    saved = (_load_extra(chatroom).get(EXTRA_KEY) or {}).get(feature.key) or {}
    saved_config = saved.get("config") if isinstance(saved.get("config"), dict) else {}
    config = {item.key: _coerce(item, saved_config.get(item.key, item.default)) for item in feature.fields}
    return bool(saved.get("enabled")), config


def has_saved_state(chatroom: Chatroom, feature: GroupFeature) -> bool:
    """这个群是否单独保存过该能力的设置(没保存过的能力可以跟随默认值,如 AI 对话跟随租户的聊天开关)。"""

    saved = (_load_extra(chatroom).get(EXTRA_KEY) or {}).get(feature.key)
    return isinstance(saved, dict) and "enabled" in saved


def is_enabled(chatroom: Chatroom | None, feature_key: str) -> bool:
    """群已启用(总开关)且该能力已打开才算启用。"""

    if chatroom is None or not bool(getattr(chatroom, "is_active", False)):
        return False
    feature = get_feature(feature_key)
    return bool(feature and get_state(chatroom, feature)[0])


def save_state(chatroom: Chatroom, feature_key: str, *, enabled: bool, config: dict[str, Any] | None = None) -> None:
    """保存开关与配置;config 为 None 时只改开关,保留原配置。"""

    feature = get_feature(feature_key)
    if feature is None:
        raise ValueError(f"未知群功能: {feature_key}")
    extra = _load_extra(chatroom)
    features = extra.get(EXTRA_KEY) if isinstance(extra.get(EXTRA_KEY), dict) else {}
    entry = features.get(feature_key) if isinstance(features.get(feature_key), dict) else {}
    entry["enabled"] = bool(enabled)
    if config is not None:
        saved = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        new_config: dict[str, Any] = {}
        for item in feature.fields:
            raw = config.get(item.key, item.default)
            if item.kind == "secret":
                # 密钥:填了就加密保存,留空保持原值(页面从不回显密钥,不能因为没填就清掉)。
                plain = str(raw or "").strip()
                new_config[item.key] = encrypt_secret(plain) if plain else str(saved.get(item.key) or "")
            else:
                new_config[item.key] = _coerce(item, raw)
        entry["config"] = new_config
    features[feature_key] = entry
    extra[EXTRA_KEY] = features
    chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)


def set_flag(chatroom: Chatroom, feature_key: str, field: str, value: bool) -> ConfigField | None:
    """只改一个开关:field 为 "enabled" 改总开关,否则改对应的 bool 配置项;其余配置原样保留。

    和 save_state 的区别:点开关即保存时,表单里别的输入框可能还有没保存的修改,也不能因此重新加密密钥,
    所以这里直接改存储里的这一个键。返回被改的配置项(总开关返回 None);field 不是该功能的 bool 配置项抛 ValueError。
    """

    feature = get_feature(feature_key)
    if feature is None:
        raise ValueError(f"未知群功能: {feature_key}")
    item = None
    if field != "enabled":
        item = next((f for f in feature.fields if f.key == field and f.kind == "bool"), None)
        if item is None:
            raise ValueError(f"{feature_key} 没有开关项: {field}")
    extra = _load_extra(chatroom)
    features = extra.get(EXTRA_KEY) if isinstance(extra.get(EXTRA_KEY), dict) else {}
    entry = features.get(feature_key) if isinstance(features.get(feature_key), dict) else {}
    if item is None:
        entry["enabled"] = bool(value)
    else:
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        config[item.key] = bool(value)
        entry["config"] = config
    features[feature_key] = entry
    extra[EXTRA_KEY] = features
    chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    return item
