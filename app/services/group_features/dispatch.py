"""群功能的统一入口:入站事件/命令来了,看群开关,命中就执行并把回复入队发送。

接在 plugin_directory.build_dispatch_report 之前调用,和插件调度互不影响:
群功能不依赖机器人绑定;但受群的“启用”总开关控制,未启用的群一律不执行。
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import Channel, Chatroom, Conversation
from app.services.group_features.registry import GroupFeature, all_features, get_feature
from app.services.group_features.store import get_state

logger = get_logger(__name__)


class FeatureContext:
    """交给各能力处理函数的上下文:只含已校验过的群、会话和本群配置。"""

    def __init__(
        self,
        *,
        db: Session,
        channel: Channel,
        conversation: Conversation,
        chatroom: Chatroom,
        config: dict[str, Any],
        normalized_event: Mapping[str, Any],
    ) -> None:
        self.db = db
        self.channel = channel
        self.conversation = conversation
        self.chatroom = chatroom
        self.config = config
        self.normalized_event = normalized_event

    @property
    def message(self) -> Mapping[str, Any]:
        value = self.normalized_event.get("message") if isinstance(self.normalized_event, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    @property
    def details(self) -> Mapping[str, Any]:
        value = self.normalized_event.get("details") if isinstance(self.normalized_event, Mapping) else None
        return value if isinstance(value, Mapping) else {}

    @property
    def text(self) -> str:
        return str(self.message.get("content") or "").strip()


def _find_chatroom(db: Session, channel: Channel, conversation: Conversation) -> Chatroom | None:
    return db.scalar(
        select(Chatroom).where(
            Chatroom.channel_id == channel.id,
            Chatroom.external_id == str(conversation.external_id),
        )
    )


def _command_args(text: str, commands: tuple[str, ...]) -> tuple[bool, str]:
    """命令匹配:允许带或不带 / 前缀,命令后面的内容作为参数。"""

    body = text.strip()
    if body[:1] in {"/", "#"}:
        body = body[1:]
    for command in commands:
        if body == command or body.startswith(command + " "):
            return True, body[len(command):].strip()
    return False, ""


def dispatch_group_features(
    db: Session,
    channel: Channel,
    normalized_event: Mapping[str, Any],
    *,
    conversation_id: int | None,
) -> list[str]:
    """处理一条入站事件,返回本次实际执行的能力 key 列表。

    不抛异常:群功能出错只记日志,不能拖垮后面的插件调度。
    """

    conversation_payload = normalized_event.get("conversation") if isinstance(normalized_event, Mapping) else None
    if not conversation_id or not isinstance(conversation_payload, Mapping):
        return []
    if str(conversation_payload.get("chat_type") or "") != "group":
        return []
    conversation = db.get(Conversation, int(conversation_id))
    if conversation is None:
        return []
    chatroom = _find_chatroom(db, channel, conversation)
    # 群的“启用”是总开关:群未启用时,插件和群功能都不执行。
    if chatroom is None or not bool(chatroom.is_active):
        return []

    message = normalized_event.get("message") if isinstance(normalized_event.get("message"), Mapping) else {}
    message_type = str(message.get("message_type") or "text")
    text = str(message.get("content") or "").strip()

    executed: list[str] = []
    for feature in all_features():
        enabled, config = get_state(chatroom, feature)
        if not enabled:
            continue
        handler = _HANDLERS.get(feature.key)
        if handler is None:
            continue
        args = ""
        if feature.event_types:
            if message_type not in feature.event_types:
                continue
        elif feature.commands:
            hit, args = _command_args(text, feature.commands)
            if not hit:
                continue
        else:
            continue
        ctx = FeatureContext(
            db=db, channel=channel, conversation=conversation, chatroom=chatroom,
            config=config, normalized_event=normalized_event,
        )
        try:
            replies = handler(ctx, args)
        except Exception:
            logger.exception("群功能执行失败", extra={"feature": feature.key, "chatroom_id": chatroom.id})
            continue
        # 处理函数返回 None 表示这条消息与本能力无关(如普通文本不是成员变更),不计入执行也不记日志。
        if replies is None:
            continue
        executed.append(feature.key)
        if replies:
            send_replies(
                db, channel, conversation, replies,
                feature_key=feature.key,
                normalized_event=normalized_event,
                by_command=bool(feature.commands),
            )
        logger.info(
            "群功能已执行",
            extra={"feature": feature.key, "chatroom_id": chatroom.id, "reply_count": len(replies or [])},
        )
    return executed


def send_replies(
    db: Session,
    channel: Channel,
    conversation: Conversation,
    replies: list[dict[str, Any]],
    *,
    feature_key: str,
    normalized_event: Mapping[str, Any],
    by_command: bool,
) -> None:
    """把回复(标准 replies 结构)转成出站任务入队。

    规整格式复用插件调度里的 _normalize_reply_item,微桥媒体外链、通用渠道入队也复用既有函数,
    这里不再复制一份。
    命令型能力(如潜水排行)按真实文本消息回复,沿用插件默认的"引用触发消息";
    事件型能力(成员变动)没有可引用的消息也没有触发人,message_type 用 group_member_ 前缀
    让 _apply_reply_mode 跳过引用与 @。
    """

    from app.services.plugin_directory import (  # 延迟导入避免循环依赖
        DirectoryPluginContext,
        PluginDispatchContext,
        _dispatch_auto_outbound_messages,
        _normalize_reply_item,
    )

    payloads = [item for item in (_normalize_reply_item(r, channel_type=channel.channel_type) for r in replies) if item]
    if not payloads:
        return
    # 直接构造真实的目录插件上下文,复用既有出站链路(微桥媒体外链、队列入队等)。
    message = normalized_event.get("message") if isinstance(normalized_event.get("message"), Mapping) else {}
    message_type = "text" if by_command else "group_member_feature"
    sender_id = str(message.get("sender_id") or "").strip() or None if by_command else None
    context = DirectoryPluginContext(
        plugin_code=f"group_feature:{feature_key}",
        binding_id=0,
        bot_id=int(channel.bot_id),
        channel_id=int(channel.id),
        channel_type=channel.channel_type,
        chat_type="group",
        message_type=message_type,
        command_prefix="/",
        content=str(message.get("content") or "") if by_command else "",
        sender_id=sender_id,
        conversation_external_id=str(conversation.external_id),
        mentioned_self=False,
        normalized_event=normalized_event if by_command else {},
        conversation_id=int(conversation.id),
    )
    static_context = PluginDispatchContext(
        tenant_id=int(channel.tenant_id),
        bot_id=int(channel.bot_id),
        channel_id=int(channel.id),
        channel_type=channel.channel_type,
        chat_type="group",
        message_type=message_type,
    )
    _dispatch_auto_outbound_messages(db, static_context, context, {"outbound_messages": payloads})


def consumed_command(executed: list[str]) -> bool:
    """本次是否有命令型群功能已经处理了这条消息(用于避免再交给 AI 兜底重复处理)。"""

    return any((get_feature(key) is not None and bool(get_feature(key).commands)) for key in executed)


# 能力模块在导入时把处理函数填进来:{feature_key: handler(ctx, args) -> list[reply]}
_HANDLERS: dict[str, Any] = {}


def register_handler(feature: GroupFeature, handler: Any) -> None:
    _HANDLERS[feature.key] = handler
