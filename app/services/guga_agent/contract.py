"""咕嘎助手 Agent 反向通道的协议常量与公共契约。"""

from __future__ import annotations

from typing import Any

# Agent 命令任务与普通出站任务分开:async_workers 不会领取这个类型,
# 只有手机端的 poll/WS 通道会消费。
JOB_TYPE = "wechat_gugatool_agent_outbound"

# 手机端只实现了这些命令,平台不能凭空调新的命令。
COMMAND_NAMES = frozenset(
    {
        "send_text",
        "send_quote",
        "send_image",
        "send_emoji",
        "send_file",
        "send_video",
        "send_voice",
        "send_app",
        "send_link",
        "send_mini_msg",
        "send_tail",
        "revoke_message",
        "delete_session",
        "upload_to_cdn",
        "download_media",
        "get_chatroom_member_detail",
        "get_chatroom_info",
        "get_friend_list",
        "get_friend_detail",
        "get_finder_share_url",
        "get_profile",
        "get_health",
        "get_recent_messages",
        "sync_messages",
        "get_system_logs",
        "get_webhook_config",
        "update_webhook_config",
        "get_mini_cloud_classes",
        "get_mini_cloud_hooks",
        "get_mini_cloud_recent",
        "install_mini_cloud_hook",
        "invoke_mini_cloud",
        "clear_mini_cloud_records",
    }
)

SEND_COMMANDS = frozenset(
    {
        "send_text",
        "send_quote",
        "send_image",
        "send_emoji",
        "send_file",
        "send_video",
        "send_voice",
        "send_app",
        "send_link",
        "send_mini_msg",
        "send_tail",
    }
)

# WebSocket 单帧过大容易断连;超过这个体积的命令留给 HTTP 长轮询下发。
WS_MAX_COMMAND_BYTES = 4 * 1024 * 1024
POLL_MAX_COMMAND_BYTES = 20 * 1024 * 1024

RUNTIME_KEY = "guga_agent"
ACCESS_KEY = "guga_agent_access"

ONLINE_TTL_SECONDS = 90
COMMAND_RUNNING_TIMEOUT_SECONDS = 240


class GugaAgentError(ValueError):
    """带 HTTP 状态码的协议错误,便于路由层直接映射响应。"""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        self.code = str(code or "guga_agent_error").strip() or "guga_agent_error"
        self.message = str(message or self.code).strip()
        self.status = int(status or 400)
        super().__init__(self.code)


def as_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    return str(value or "").strip()


def identity_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """统一提取设备/账号/会话三个身份字段,不接受正文里多余的租户信息。"""

    return {
        "device_id": text(payload.get("device_id")),
        "account_id": text(payload.get("account_id")),
        "session_id": text(payload.get("session_id")),
    }


# 手机发送回执的字段名随版本不同:命令结果用 LocalId/MsgSvrId,
# 事件体用 localMessageID/serverMessageID,统一在这里认。
_LOCAL_ID_KEYS = ("LocalId", "localId", "localMessageID", "localMessageId", "local_message_id", "local_id")
_SERVER_ID_KEYS = ("MsgSvrId", "msgSvrId", "serverMessageID", "serverMessageId", "server_message_id", "server_id")


def message_ids(result: Any) -> dict[str, int]:
    """从命令结果里取本地/服务端消息编号;认不出就返回空,不猜。"""

    payload = as_object(result)
    for source in (as_object(payload.get("Data")), payload):
        ids: dict[str, int] = {}
        for keys, target in ((_LOCAL_ID_KEYS, "local"), (_SERVER_ID_KEYS, "server")):
            for key in keys:
                value = source.get(key)
                if isinstance(value, bool):
                    continue
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    ids[target] = number
                    break
        if ids:
            return ids
    return {}
