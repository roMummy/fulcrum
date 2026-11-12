"""标准示例插件:读懂这个文件就掌握了写插件的基本套路。

四个要点:
1. 消息触发:平台调用 execute_message_action(db, request),返回 PluginExecutionResult,回复放进 output["reply_text"];
2. 配置:从 request.config 读取,读取时一律做类型和范围校验,不信任保存的内容一定合法;
3. 显示人名:状态里存稳定的用户 ID,显示时用平台的 member_names 换成本群群昵称(消息自带的名字常常是 wxid);
4. 定时:在 config.toml 声明 [[schedule]],平台到点调用 execute_scheduled_action(db, request),自己决定发给哪些群。

只需要无状态回复时,实现 execute_action(request) 就够了(见下面的"你好"分支可以单独拆出来)。
详见《插件开发规范》。
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Chatroom, Conversation
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime import member_names
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.state_store import PluginStateStore

CODE = "hello_plugin"
STYLES = {"friendly": "~", "formal": "。"}
BOARD_COMMAND = "你好榜"
BOARD_LIMIT = 5


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把配置值转成范围内的整数;不是数字时用默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_list(value: Any) -> list[str]:
    """标签类配置保存为字符串列表;兜底处理成其他形态的情况。"""

    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _build_greeting(config: Mapping[str, Any], target: str) -> str:
    greeting = str(config.get("greeting") or "你好").strip()[:50]
    times = _bounded_int(config.get("times"), 1, 1, 5)
    ending = "!" if config.get("loud") is True else STYLES.get(str(config.get("style")), "~")
    text = " ".join([f"{greeting}{ending}"] * times)
    if target:
        text = f"{target},{text}"
    signature = _as_list(config.get("signature"))
    return text + (f"\n—— {'、'.join(signature)}" if signature else "")


def _result(message: str, text: str = "", status: str = "success") -> PluginExecutionResult:
    return PluginExecutionResult(ok=True, status=status, message=message, output={"reply_text": text} if text else {})


# ---------------------------------------------------------------- 入口 1:后台"重载"健康检查与无上下文执行

def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="示例插件运行正常。")
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    target = str(request.payload.get("command_args") or "").strip()[:30]
    return _result("已回复", _build_greeting(request.config, target))


# ---------------------------------------------------------------- 入口 2:真实聊天消息(能读写数据库)

def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action != "execute":
        return execute_action(request)
    context = request.context
    if context is None or not context.sender_id:
        return execute_action(request)  # 没有可信的发送者:退回无状态回复
    store = PluginStateStore(db, context)
    group = db.get(Conversation, int(context.conversation_id)) if context.conversation_id else None
    # 状态按会话分开存:key 带上会话 ID。示例里一个群一条记录({用户 ID: 次数},单条上限 64KB,够小群用;
    # 人很多的群请像 idiom_guess 那样每人一条记录)。
    key = f"counts:{context.conversation_id or 0}"
    counts: dict[str, int] = dict((store.get(CODE, key) or {}).get("counts") or {})

    if str(request.payload.get("command_name") or "") == BOARD_COMMAND:
        if not counts or group is None or context.channel_id is None:
            return _result("暂无数据", "还没有人打过招呼,先发一句「你好」吧。")
        top = sorted(counts.items(), key=lambda item: (-int(item[1]), item[0]))[:BOARD_LIMIT]
        # 要点:存的是用户 ID;显示时才换成本群群昵称(昵称之后改了,榜单跟着变;查不到才显示 ID)。
        names = member_names(db, channel_id=int(context.channel_id), group_external_id=str(group.external_id), user_ids=[uid for uid, _ in top])
        lines = ["【本群打招呼排行榜】", *(f"{index}. {names[uid]} - {count}次" for index, (uid, count) in enumerate(top, 1))]
        return _result("已发送排行榜", "\n".join(lines))

    counts[context.sender_id] = int(counts.get(context.sender_id, 0)) + 1
    store.put(CODE, key, {"counts": counts})
    target = str(request.payload.get("command_args") or "").strip()[:30]
    return _result("已回复", _build_greeting(request.config, target))


# ---------------------------------------------------------------- 入口 3:定时(由 config.toml 的 [[schedule]] 触发)

def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """每天定时问候:发给这个机器人下、启用了群管理的所有群。

    平台只告诉你"到点了"和哪个绑定,不带会话;发给谁由插件决定。发消息用 enqueue_outbound_job 入队,
    不要自己直接调渠道。同一天同一时刻平台只会触发一次,不需要自己再做去重。
    """

    context = request.context
    if request.action != "daily_greeting" or context is None:
        return _result("忽略", status="skipped")
    if request.config.get("scheduled_enabled") is not True:
        return _result("定时问候未开启", status="skipped")
    binding = db.get(BotPlugin, context.binding_id)
    if binding is None or binding.status != "enabled" or binding.bot is None or not binding.bot.is_enabled:
        return _result("绑定未启用", status="skipped")
    text = str(request.config.get("daily_text") or "大家早上好!").strip()[:200]
    sent = 0
    rooms = db.scalars(select(Chatroom).where(Chatroom.bot_id == binding.bot_id, Chatroom.is_active.is_(True), Chatroom.management_status == "enabled")).all()
    for room in rooms:
        conversation = db.scalar(select(Conversation).where(
            Conversation.bot_id == binding.bot_id, Conversation.channel_id == room.channel_id,
            Conversation.external_id == room.external_id, Conversation.chat_type == "group"))
        channel = conversation.channel if conversation is not None else None
        if conversation is None or channel is None or not channel.is_enabled or channel.deleted_at is not None:
            continue
        enqueue_outbound_job(
            db, conversation=conversation, channel=channel,
            payload={"external_id": conversation.external_id, "receiver_id": conversation.external_id, "chat_type": "group", "message_type": "text", "content": text},
            plugin_code=CODE, related_type="hello_plugin_binding", related_id=binding.id,
            priority=60, max_attempts=1,  # 发消息失败不自动重试,避免重复发送
        )
        sent += 1
    return _result(f"已向 {sent} 个群发送问候。", status="success" if sent else "skipped")
