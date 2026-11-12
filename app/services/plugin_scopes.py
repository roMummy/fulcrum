"""机器人插件在没有单群设置时的默认开放范围。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Plugin, TaskJob


def binding_group_default_enabled(binding: BotPlugin) -> bool:
    # 尚未 flush 的旧构造方式可能还没有默认值，按旧绑定的继承语义处理。
    default_enabled = getattr(binding, "group_default_enabled", None)
    return binding.status == "enabled" and (default_enabled is None or bool(default_enabled))


def group_plugin_scope_enabled(binding: BotPlugin, scope: GroupPluginScope | None) -> bool:
    """已保存的单群选择优先，缺少记录时使用首次绑定的选择。"""

    return bool(scope.enabled) if scope is not None else binding_group_default_enabled(binding)


def plugin_group_conversation_enabled(db: Session, *, channel: Channel, conversation: Conversation, plugin_code: str) -> bool:
    """定时和异步出站也必须遵守群插件设置，不能绕过消息入口。"""

    # AI 核心和内置群功能有各自的设置入口，不属于可绑定的目录插件。
    if conversation.chat_type != "group" or not plugin_code or plugin_code == "ai_dispatch" or plugin_code.startswith("group_feature:"):
        return True
    binding = db.scalar(
        select(BotPlugin).join(Plugin, Plugin.id == BotPlugin.plugin_id).where(
            BotPlugin.bot_id == channel.bot_id,
            Plugin.code == plugin_code,
        )
    )
    if binding is None or binding.status != "enabled":
        return False
    chatroom = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id))
    scope = None
    if chatroom is not None:
        scope = db.scalar(select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
            GroupPluginScope.bot_plugin_id == binding.id,
        ))
    return group_plugin_scope_enabled(binding, scope)


def cancel_disallowed_plugin_outbound_job(db: Session, *, channel: Channel, job: TaskJob, payload: dict) -> bool:
    """设备领取前复核已入队的插件消息；手动消息和控制命令不受群插件设置影响。"""

    plugin_code = str(payload.get("plugin_code") or "").strip()
    if not plugin_code:
        return False
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    binding = db.scalar(select(BotPlugin).join(Plugin, Plugin.id == BotPlugin.plugin_id).where(
        BotPlugin.bot_id == channel.bot_id, Plugin.code == plugin_code,
    ))
    if (
        conversation is not None
        and conversation.channel_id == channel.id and conversation.bot_id == channel.bot_id
        and not (binding is not None and binding.status == "unbound")
        and plugin_group_conversation_enabled(db, channel=channel, conversation=conversation, plugin_code=plugin_code)
    ):
        return False
    # 关闭权限后旧任务不能留在 pending，避免重新启用时又发出过期消息。
    job.status = "cancelled"
    job.finished_at = now_shanghai()
    job.error_message = "插件消息已无有效会话或授权，取消待发送消息"
    return True
