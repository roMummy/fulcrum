"""群详情页「功能」标签的数据与表单解析。"""

from __future__ import annotations

from typing import Any, Mapping

from app.models.entities import Chatroom
from app.services.group_features.registry import all_features, get_feature
from app.services.group_features.store import get_state, save_state

# 表单里配置项的字段名前缀,避免和 enabled 等控制字段重名。
FORM_PREFIX = "cfg_"


def build_feature_cards(chatroom: Chatroom) -> list[dict[str, Any]]:
    """生成页面渲染用的能力列表:每项含开关状态和带当前值的配置项。"""

    cards: list[dict[str, Any]] = []
    for feature in all_features():
        enabled, config = get_state(chatroom, feature)
        cards.append(
            {
                "key": feature.key,
                "name": feature.name,
                "description": feature.description,
                "enabled": enabled,
                "fields": [
                    {
                        "name": FORM_PREFIX + item.key,
                        "label": item.label,
                        "kind": item.kind,
                        "hint": item.hint,
                        "value": config[item.key],
                        "minimum": item.minimum,
                        "maximum": item.maximum,
                    }
                    for item in feature.fields
                ],
            }
        )
    return cards


def save_from_form(chatroom: Chatroom, feature_key: str, form: Mapping[str, Any]) -> str:
    """按表单保存某个能力的开关和配置,返回给用户看的提示。

    复选框没勾选时浏览器不会提交该字段,所以 bool 项按“字段是否存在”判断。
    """

    feature = get_feature(feature_key)
    if feature is None:
        raise ValueError(f"未知群功能: {feature_key}")
    config: dict[str, Any] = {}
    for item in feature.fields:
        name = FORM_PREFIX + item.key
        config[item.key] = (name in form) if item.kind == "bool" else form.get(name, item.default)
    enabled = "enabled" in form
    save_state(chatroom, feature_key, enabled=enabled, config=config)
    return f"已{'开启' if enabled else '关闭'}「{feature.name}」"
