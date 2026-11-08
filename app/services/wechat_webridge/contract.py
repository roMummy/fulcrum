"""WeBridge 8080 长轮询协议的稳定字段契约。

本模块只保存协议层常量和轻量字段解析，业务落库由 service.py 负责，避免路由
直接依赖插件请求的原始字段。
"""

from __future__ import annotations

from typing import Any

AGENT_PATHS = ("register", "state", "event", "poll", "command_result")
JOB_TYPE = "wechat_webridge_outbound"

# 微桥文档的命令按业务域分组，供后台、开放接口和客户端能力页复用。
# 这里不把命令硬编码成后端白名单；手机插件版本决定最终可执行范围，
# 目录只负责让调用方能够发现能力并生成正确的命令入口。
COMMAND_GROUPS: dict[str, tuple[str, ...]] = {
    "message": (
        "send_message", "send_signal", "send_pat", "send_image", "send_emoji", "send_voice",
        "send_video", "send_video_from_url", "send_livephoto", "send_link_card", "send_music_card",
        "send_file", "send_file_from_url", "send_contact_card", "send_location", "send_xml",
        "forward_message", "revoke_message", "delete_session",
    ),
    "media": (
        "download_message_media", "upload_message_media", "upload_message_image",
        "upload_file_to_wechat_cdn", "message_media_probe", "location_geocode_probe",
    ),
    "contacts": (
        "friends", "friends_all", "search_contact", "add_friend", "add_room_friend",
        "accept_friend_request", "friend_add_probe", "delete_friend", "set_friend_remark",
        "set_friend_star", "set_friend_blacklist", "friend_request_probe", "friend_pay_name",
    ),
    "groups": (
        "groups_all", "room_members", "create_group", "add_group_member", "invite_group_member",
        "kick_group_member", "kick_group_members", "quit_group", "rename_group", "set_group_name",
        "set_group_self_nickname", "get_group_notice", "get_chatroom_qrcode", "group_admin_operate",
        "set_group_msg_silence", "get_group_invitation", "accept_group_invitation", "group_invitation_probe",
    ),
    "payments": (
        "receive_red_packet", "red_packet_detail", "receive_transfer", "refund_transfer",
        "get_transfer_status", "red_packet_probe", "red_packet_detail_probe", "transfer_probe",
    ),
    "profile": (
        "account_status", "bridge_status", "device_status", "profile_update", "profile_nickname",
        "profile_gender", "profile_region", "profile_signature", "profile_pat_suffix",
        "clear_gender_region", "profile_region_codes", "profile_probe", "profile_region_probe",
        "profile_pat_suffix_probe", "profile_nickname_probe", "get_self_qrcode",
    ),
    "official_accounts": (
        "official_accounts", "official_account_search", "official_account_follow",
        "official_account_unfollow", "official_account_articles",
    ),
    "moments": (
        "moments_list", "moments_user", "moments_detail", "moments_refresh", "moments_like",
        "moments_unlike", "moments_comment", "moments_delete_comment", "moments_publish",
        "moments_delete", "moments_permissions_query", "moments_permissions_update",
        "moments_operation", "moments_probe", "moments_media_download",
    ),
    "favorites": ("favorites_list", "favorites_send", "favorites_delete"),
}

COMMANDS = tuple(command for commands in COMMAND_GROUPS.values() for command in commands)


def as_object(value: Any) -> dict[str, Any]:
    """把未知输入收敛成对象，避免恶意数组或字符串进入业务分支。"""

    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    """统一清理协议文本字段，并限制单字段长度。"""

    return str(value or "").strip()[:512]


def identity_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """提取设备、会话和微信账号三元身份；不保存原始状态大对象。"""

    return {
        "device_id": text(payload.get("device_id")),
        "session_id": text(payload.get("session_id")),
        "account_id": text(payload.get("account_id")),
        "runtime_version": text(payload.get("runtime_version")),
        "build_marker": text(payload.get("build_marker")),
    }
