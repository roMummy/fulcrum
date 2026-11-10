"""群详情页「功能」标签的数据与表单解析。"""

from __future__ import annotations

from typing import Any, Mapping

from app.models.entities import Chatroom
from app.services.group_features.registry import all_features, get_feature
from app.services.group_features.store import get_state, has_saved_state, save_state, set_flag

# 表单里配置项的字段名前缀,避免和 enabled 等控制字段重名。
FORM_PREFIX = "cfg_"


def build_feature_cards(chatroom: Chatroom, channel_type: str = "", defaults: Mapping[str, bool] | None = None) -> list[dict[str, Any]]:
    """生成页面渲染用的能力列表:每项含开关状态和按小节分好组、带当前值的配置项。

    限定了渠道类型的能力(如自动下载附件)只在对应渠道的群里显示。
    defaults:能力的默认开关(如 AI 对话跟随租户的聊天开关),只在这个群没单独保存过设置时生效。
    """

    cards: list[dict[str, Any]] = []
    for feature in all_features():
        if feature.channel_types and channel_type not in feature.channel_types:
            continue
        enabled, config = get_state(chatroom, feature)
        if not has_saved_state(chatroom, feature) and defaults and feature.key in defaults:
            enabled = bool(defaults[feature.key])
        sections: list[dict[str, Any]] = []
        for item in feature.fields:
            if not sections or sections[-1]["title"] != item.group:
                sections.append({"title": item.group, "switches": [], "inputs": []})
            field = {
                "key": item.key,
                "name": FORM_PREFIX + item.key,
                "label": item.label,
                "kind": item.kind,
                "hint": item.hint,
                # 密钥不回显,只告诉页面是否已配置。
                "value": "" if item.kind == "secret" else config[item.key],
                "configured": bool(config[item.key]) if item.kind == "secret" else False,
                "minimum": item.minimum,
                "maximum": item.maximum,
                "depends_on": FORM_PREFIX + item.depends_on if item.depends_on else "",
            }
            # 开关项和输入项分开摆:开关排成一列,输入框排成整齐的两列。
            sections[-1]["switches" if item.kind == "bool" else "inputs"].append(field)
        cards.append(
            {
                "key": feature.key,
                "name": feature.name,
                "description": feature.description,
                "enabled": enabled,
                "sections": sections,
                # 有文本/数字/密钥输入的设置才需要"保存"按钮;只有开关的设置点一下就已保存。
                "has_inputs": any(section["inputs"] for section in sections),
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


def toggle_flag(chatroom: Chatroom, feature_key: str, field: str, value: bool) -> str:
    """点开关即保存:只改这一个开关,返回给用户看的提示。"""

    feature = get_feature(feature_key)
    if feature is None:
        raise ValueError(f"未知群功能: {feature_key}")
    item = set_flag(chatroom, feature_key, field, value)
    state = "开启" if value else "关闭"
    return f"已{state}「{feature.name}」" if item is None else f"已{state}「{item.label}」"
