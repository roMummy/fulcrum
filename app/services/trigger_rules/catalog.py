"""群级「触发 → 回复」规则的目录:有哪些触发条件、有哪些回复动作。

规则本身存在 group_trigger_rules 表里,只记录用户的选择(触发类型 + 参数、动作类型 + 参数)。
触发类型和动作类型都在这里**注册一次**:页面表单、校验、执行都由注册信息派生,
所以新增一种触发(比如"有人入群")或一种结果(比如"拍回去")只需要在本文件加一个注册项,
页面、存储和分发都不用改。

模板变量:回复文字、卡片标题等可以写 {sender_name} 这样的占位符,执行时替换;不认识的占位符原样保留。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from app.services.plugin_runtime.replies import PluginReplyError, image_reply, link_reply, text_reply, video_reply
from app.services.trigger_rules import media

MAX_TEXT_LENGTH = 1000


@dataclass(frozen=True)
class RuleField:
    """触发或动作的一个可填项。kind: text / textarea / url / select / file。"""

    name: str
    label: str
    kind: str = "text"
    default: str = ""
    hint: str = ""
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()
    # kind="file" 时的素材类别:image / audio / video
    media_kind: str = ""


@dataclass(frozen=True)
class EventView:
    """交给触发器匹配的入站事件:只含已提取好的字段,触发器不碰原始数据库对象。"""

    message_type: str
    text: str
    sender_id: str
    sender_name: str
    group_name: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerType:
    key: str
    label: str
    description: str
    event_types: tuple[str, ...]
    fields: tuple[RuleField, ...]
    # 命中返回模板变量字典,不命中返回 None
    match: Callable[[Mapping[str, Any], EventView], dict[str, str] | None]
    # 页面上提示用户这个触发下能写哪些 {变量}
    variables: tuple[tuple[str, str], ...] = ()
    # 一句话描述这条规则(列表里展示),参数是已校验过的触发参数
    summarize: Callable[[Mapping[str, Any]], str] = lambda params: ""


@dataclass(frozen=True)
class BuildContext:
    tenant_id: int
    variables: Mapping[str, str]


@dataclass(frozen=True)
class ActionType:
    key: str
    label: str
    description: str
    fields: tuple[RuleField, ...]
    build: Callable[[Mapping[str, Any], BuildContext], dict[str, Any]]
    summarize: Callable[[Mapping[str, Any]], str] = lambda params: ""


_TRIGGERS: dict[str, TriggerType] = {}
_ACTIONS: dict[str, ActionType] = {}


def register_trigger(item: TriggerType) -> TriggerType:
    if item.key in _TRIGGERS:
        raise ValueError(f"触发类型重复注册: {item.key}")
    _TRIGGERS[item.key] = item
    return item


def register_action(item: ActionType) -> ActionType:
    if item.key in _ACTIONS:
        raise ValueError(f"动作类型重复注册: {item.key}")
    _ACTIONS[item.key] = item
    return item


def get_trigger(key: str) -> TriggerType | None:
    return _TRIGGERS.get(key)


def get_action(key: str) -> ActionType | None:
    return _ACTIONS.get(key)


def all_triggers() -> tuple[TriggerType, ...]:
    return tuple(_TRIGGERS.values())


def all_actions() -> tuple[ActionType, ...]:
    return tuple(_ACTIONS.values())


def trigger_event_types() -> tuple[str, ...]:
    """所有触发类型关心的消息类型之并集(群功能据此决定哪些事件要交给规则引擎)。"""

    seen: list[str] = []
    for item in _TRIGGERS.values():
        for event_type in item.event_types:
            if event_type not in seen:
                seen.append(event_type)
    return tuple(seen)


def render_text(template: str, variables: Mapping[str, str]) -> str:
    """固定占位符替换,不提供任何表达式能力;不认识的占位符原样保留。"""

    rendered = str(template or "")
    for key, value in variables.items():
        rendered = rendered.replace("{" + key + "}", str(value or ""))
    return rendered


# ---------------------------------------------------------------- 触发:拍一拍

@dataclass(frozen=True)
class PatInfo:
    from_name: str
    patted_name: str
    template: str
    # True 被拍的是机器人自己;False 是别人;None 无法判断
    patted_is_me: bool | None


_PAT_SPLIT = re.compile(r"^(?P<who>.*?)\s*拍了拍\s*(?P<target>.*)$", re.S)
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


def _clean_name(value: str) -> str:
    """去掉模板占位符和引号:`"${张三}"` → `张三`。"""

    text = _PLACEHOLDER.sub(lambda match: match.group(1), str(value or ""))
    return text.strip().strip('"“”\'‘’ ').strip()


def parse_pat(view: EventView) -> PatInfo | None:
    """从入站事件里尽可能容错地解析拍一拍:适配器放进 details.pat 的字段、sysmsg XML、"A 拍了拍 B" 文案。

    微信里被拍的人看到的是"A 拍了拍我",所以机器人被拍时文案里目标是"我"。
    无法确定被拍的是谁时 patted_is_me 为 None,「只有拍机器人时」不会触发,避免误回。
    """

    info = view.details.get("pat") if isinstance(view.details.get("pat"), Mapping) else {}
    template = str(info.get("template") or "").strip()
    from_name = str(info.get("from_name") or "").strip()
    patted_name = str(info.get("patted_name") or "").strip()
    is_me: bool | None = info.get("patted_is_me") if isinstance(info.get("patted_is_me"), bool) else None

    raw = view.text.strip()
    if not template and raw.startswith("<"):
        try:
            root = ET.fromstring(raw)
            if root.tag == "sysmsg" and str(root.attrib.get("type") or "").lower() == "pat":
                template = str(root.findtext(".//template") or "").strip()
                from_name = from_name or str(root.findtext(".//fromusername") or "").strip()
                patted_name = patted_name or str(root.findtext(".//pattedusername") or "").strip()
        except ET.ParseError:
            pass
    if not template and "拍了拍" in raw:
        template = raw
    if not template and view.message_type != "pat":
        return None

    match = _PAT_SPLIT.match(template.replace("\n", " ")) if template else None
    if match:
        from_name = from_name or _clean_name(match.group("who"))
        target = _clean_name(match.group("target"))
        if is_me is None:
            if target.startswith("我"):
                is_me = True
            elif target:
                is_me = False
        if not patted_name and target and not target.startswith("我"):
            patted_name = target
    if view.message_type != "pat" and match is None:
        return None
    return PatInfo(from_name=from_name, patted_name=patted_name, template=template, patted_is_me=is_me)


def _match_pat(params: Mapping[str, Any], view: EventView) -> dict[str, str] | None:
    if view.message_type != "pat":
        return None
    pat = parse_pat(view)
    if pat is None:
        return None
    if str(params.get("scope") or "me") != "any" and pat.patted_is_me is not True:
        return None
    return {
        "sender_name": pat.from_name or view.sender_name or view.sender_id,
        "patted_name": pat.patted_name or ("我" if pat.patted_is_me else ""),
    }


PAT_TRIGGER = register_trigger(
    TriggerType(
        key="pat",
        label="有人拍一拍",
        description="群里有人拍了拍机器人时自动回复。",
        event_types=("pat",),
        match=_match_pat,
        fields=(
            RuleField(
                "scope", "什么时候回复", kind="select", default="me",
                options=(("me", "有人拍机器人自己时"), ("any", "群里任何人被拍时")),
                hint="一般选第一个;选第二个机器人会回应群里所有拍一拍。",
            ),
        ),
        variables=(("sender_name", "拍的人"), ("patted_name", "被拍的人"), ("group", "群名称"), ("time", "当前时间")),
        summarize=lambda params: "有人拍机器人时" if str(params.get("scope") or "me") != "any" else "群里有人被拍时",
    )
)


# ---------------------------------------------------------------- 触发:关键词

_KEYWORD_MODES = (("contains", "消息里包含"), ("exact", "消息完全等于"), ("prefix", "消息以它开头"))


def _match_keyword(params: Mapping[str, Any], view: EventView) -> dict[str, str] | None:
    if view.message_type != "text":
        return None
    keyword = str(params.get("text") or "").strip()
    content = view.text.strip()
    if not keyword or not content:
        return None
    mode = str(params.get("mode") or "contains")
    if mode == "exact":
        hit, args = content == keyword, ""
    elif mode == "prefix":
        hit, args = content.startswith(keyword), content[len(keyword):].strip()
    else:
        hit, args = keyword in content, ""
    if not hit:
        return None
    return {"sender_name": view.sender_name or view.sender_id, "content": content, "args": args}


KEYWORD_TRIGGER = register_trigger(
    TriggerType(
        key="keyword",
        label="群里说了某句话",
        description="群消息命中你设置的文字时自动回复。",
        event_types=("text",),
        match=_match_keyword,
        fields=(
            RuleField("text", "关键词", required=True, hint="例如:签到、早安。"),
            RuleField("mode", "怎么算命中", kind="select", default="contains", options=_KEYWORD_MODES),
        ),
        variables=(("sender_name", "发消息的人"), ("content", "消息原文"), ("args", "「以它开头」时后面跟的内容"), ("group", "群名称"), ("time", "当前时间")),
        summarize=lambda params: f"{dict(_KEYWORD_MODES).get(str(params.get('mode') or 'contains'), '消息里包含')}「{params.get('text', '')}」",
    )
)


# ---------------------------------------------------------------- 动作

def _media_path(ctx: BuildContext, params: Mapping[str, Any], name: str, media_kind: str) -> Path:
    stored = str(params.get(name) or "").strip()
    try:
        return media.resolve(ctx.tenant_id, stored, media_kind)
    except media.MediaError as exc:
        raise PluginReplyError(str(exc)) from exc


def _build_text(params: Mapping[str, Any], ctx: BuildContext) -> dict[str, Any]:
    return text_reply(render_text(str(params.get("content") or ""), ctx.variables))


def _build_image(params: Mapping[str, Any], ctx: BuildContext) -> dict[str, Any]:
    path = _media_path(ctx, params, "file", "image")
    return image_reply(image_path=path, allowed_root=media.media_root(ctx.tenant_id))


def _build_voice(params: Mapping[str, Any], ctx: BuildContext) -> dict[str, Any]:
    path = _media_path(ctx, params, "file", "audio")
    return {"type": "voice", "base64": media.read_base64(path)}


def _build_video(params: Mapping[str, Any], ctx: BuildContext) -> dict[str, Any]:
    video = _media_path(ctx, params, "file", "video")
    cover_name = str(params.get("cover") or "").strip()
    cover_base64 = media.read_base64(_media_path(ctx, params, "cover", "image")) if cover_name else media.PLACEHOLDER_COVER_BASE64
    return video_reply(video_base64=media.read_base64(video), cover_base64=cover_base64)


def _build_card(params: Mapping[str, Any], ctx: BuildContext) -> dict[str, Any]:
    return link_reply(
        title=render_text(str(params.get("title") or ""), ctx.variables),
        description=render_text(str(params.get("description") or ""), ctx.variables),
        url=render_text(str(params.get("url") or ""), ctx.variables),
        thumb_url=str(params.get("thumb_url") or ""),
    )


def _short(value: Any, limit: int = 24) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


TEXT_ACTION = register_action(
    ActionType(
        key="text", label="文字", description="回复一段文字。",
        fields=(RuleField("content", "回复内容", kind="textarea", required=True, hint="可以用 {sender_name} 这样的变量,发送时会替换成真实内容。"),),
        build=_build_text,
        summarize=lambda params: f"文字「{_short(params.get('content'))}」",
    )
)
IMAGE_ACTION = register_action(
    ActionType(
        key="image", label="图片", description="回复一张图片。",
        fields=(RuleField("file", "图片文件", kind="file", media_kind="image", required=True, hint="支持 jpg、png、gif、webp,不超过 5MB。"),),
        build=_build_image,
        summarize=lambda params: "图片",
    )
)
VOICE_ACTION = register_action(
    ActionType(
        key="voice", label="语音", description="回复一段语音。",
        fields=(RuleField("file", "语音文件", kind="file", media_kind="audio", required=True, hint="支持 mp3、wav、amr、silk、m4a,不超过 5MB。"),),
        build=_build_voice,
        summarize=lambda params: "语音",
    )
)
VIDEO_ACTION = register_action(
    ActionType(
        key="video", label="视频", description="回复一段视频。",
        fields=(
            RuleField("file", "视频文件", kind="file", media_kind="video", required=True, hint="支持 mp4,不超过 20MB。"),
            RuleField("cover", "视频封面", kind="file", media_kind="image", hint="可不传,会用默认封面。"),
        ),
        build=_build_video,
        summarize=lambda params: "视频",
    )
)
CARD_ACTION = register_action(
    ActionType(
        key="card", label="卡片", description="回复一张可点击的链接卡片。",
        fields=(
            RuleField("title", "卡片标题", required=True, hint="可以用变量,例如 {sender_name}。"),
            RuleField("description", "卡片描述"),
            RuleField("url", "点击后打开的网址", kind="url", required=True, hint="以 http:// 或 https:// 开头。"),
            RuleField("thumb_url", "缩略图网址", kind="url", hint="可不填;以 http:// 或 https:// 开头的图片地址。"),
        ),
        build=_build_card,
        summarize=lambda params: f"卡片「{_short(params.get('title'))}」",
    )
)
