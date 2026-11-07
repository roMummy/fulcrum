__all__ = [
    "auto_recover_wechat_ipad_login",
    "build_wechat_ipad_chatroom_list",
    "build_wechat_ipad_chatroom_detail",
    "build_wechat_ipad_contact_list",
    "build_wechat_ipad_group_summary",
    "build_wechat_ipad_overview",
    "heartbeat_wechat_ipad_channel",
    "pull_wechat_ipad_messages",
    "poll_wechat_ipad_login_status",
    "record_wechat_ipad_receive_event",
    "record_wechat_ipad_send_event",
    "reconnect_wechat_ipad",
    "run_wechat_ipad_action",
    "reset_wechat_ipad_chatroom_scope",
    "resolve_wechat_ipad_qrcode_image",
    "should_sync_wechat_ipad_chatroom_for_event",
    "sync_wechat_ipad_chatrooms",
    "sync_wechat_ipad_contacts",
    "start_wechat_ipad_login",
    "update_wechat_ipad_chatroom_scope_config_override",
    "update_wechat_ipad_chatroom_scope_enabled",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    from app.services.wechat_ipad import service

    return getattr(service, name)
