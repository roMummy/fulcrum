"""群功能注册表:每个能力声明名称、说明、默认配置和可配置项。

页面据此渲染开关和配置弹窗,不需要为每个能力单独写模板;
新增能力只需在对应模块里 register 一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigField:
    """一个配置项:key 即存入群配置的键名。"""

    key: str
    label: str
    # text / textarea / number / bool / secret(密钥:加密保存,页面不回显,留空表示保持不变)
    kind: str = "text"
    default: Any = ""
    hint: str = ""
    # number 类型的取值范围,超出会被钳制;其他类型忽略。
    minimum: int | None = None
    maximum: int | None = None
    # 页面上的小节标题;同一能力里 group 相同的配置项排在一起,留空归入未命名小节。
    group: str = ""
    # 依赖另一个 bool 配置项的 key:被依赖项关闭时,本项在页面上变淡(仍可编辑、仍会保存)。
    depends_on: str = ""


@dataclass(frozen=True)
class GroupFeature:
    """一个群功能。

    key 是稳定标识,同时是群配置里的键名,一旦上线不要改。
    commands 为空表示这是事件型能力(如成员变更提醒),不响应命令。
    """

    key: str
    name: str
    description: str
    fields: tuple[ConfigField, ...] = ()
    commands: tuple[str, ...] = ()
    # 事件型能力关心的消息类型(如 group_member_join);命令型留空。
    event_types: tuple[str, ...] = ()
    # 只在这些渠道类型的群里提供(页面不显示其他渠道);留空表示所有渠道。
    channel_types: tuple[str, ...] = ()

    def defaults(self) -> dict[str, Any]:
        return {item.key: item.default for item in self.fields}


_FEATURES: dict[str, GroupFeature] = {}


def register(feature: GroupFeature) -> GroupFeature:
    """注册能力;重复注册同一个 key 视为编程错误。"""

    if feature.key in _FEATURES:
        raise ValueError(f"群功能重复注册: {feature.key}")
    _FEATURES[feature.key] = feature
    return feature


def get_feature(key: str) -> GroupFeature | None:
    _ensure_loaded()
    return _FEATURES.get(key)


def all_features() -> tuple[GroupFeature, ...]:
    _ensure_loaded()
    return tuple(_FEATURES.values())


_loaded = False


def _ensure_loaded() -> None:
    """首次使用时导入各能力模块,让它们完成注册(延迟导入避免循环依赖)。

    用显式标记而不是"注册表是否为空"判断:有人先单独 import 了某个能力模块时,
    注册表已非空,但其他能力仍未加载。
    """

    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.services.group_features import ai_chat, inactive_rank, media_download, member_change, red_packet, transfer_receive  # noqa: F401
