from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8), name="CST")

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.services.plugin_scopes import binding_group_default_enabled, group_plugin_scope_enabled
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, GroupPluginScope, Plugin
from app.services.channel_events import ingest_inbound_event
from app.services.chatroom_registry import format_chatroom_management_status, management_tone
from app.services.plugin_runtime.registry import get_plugin_schema
from app.services.plugin_schemas import deep_merge_config, dump_config_json, parse_config_json
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.chatrooms import (
    materialize_group_plugin_scopes_for_chatroom,
    reset_group_plugin_scope_to_inherited,
    update_group_plugin_scope_config_override,
    update_group_plugin_scope_enabled,
    upsert_wechat_ipad_chatroom_profile,
)

RECENT_CALL_FRESH_SECONDS = 10 * 60
RECENT_CALL_STALE_SECONDS = 60 * 60
WECHAT_IPAD_LOGIN_PROFILE_KEYS = ("car", "ipad")
WECHAT_IPAD_LOGIN_PROFILE_LABELS = {
    "car": "Car",
    "ipad": "iPad",
}
WECHAT_IPAD_HEARTBEAT_INTERVAL_SECONDS = 60
WECHAT_IPAD_HEARTBEAT_LONG_INTERVAL_SECONDS = 120
WECHAT_IPAD_TWICE_AUTO_AUTH_COOLDOWN_SECONDS = 300
WECHAT_IPAD_AUTO_RECOVER_COOLDOWN_SECONDS = 300
logger = get_logger(__name__)


def _summarize_wechat_call_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key == "channel":
            channel = value
            summary["channel_id"] = getattr(channel, "id", None)
            summary["channel_name"] = getattr(channel, "name", None)
            continue
        if key in {"proxy"}:
            summary[key] = "configured" if value else "empty"
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            rendered = str(value or "")
            summary[key] = rendered[:160]
            continue
        summary[key] = type(value).__name__
    return summary


def _summarize_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把协议回包压缩为运行诊断所需字段，禁止把大体积业务载荷写入渠道表。"""

    # 已经压缩过的摘要会在后续状态写入中被重复使用；保持原字段清单，避免每次持久化继续丢失信息。
    if isinstance(payload.get("top_level_keys"), list) and isinstance(payload.get("data_keys"), list):
        return {
            "top_level_keys": list(payload.get("top_level_keys") or [])[:20],
            "data_keys": list(payload.get("data_keys") or [])[:20],
            "message": str(payload.get("message") or ""),
            "code": payload.get("code"),
            "http_status": payload.get("http_status"),
            "success": (
                payload.get("success")
                if isinstance(payload.get("success"), bool)
                else WechatIpadApiClient.is_success(payload)
            ),
        }
    data = WechatIpadApiClient.extract_data(payload)
    return {
        "top_level_keys": sorted(list(payload.keys()))[:20],
        "data_keys": sorted(list(data.keys()))[:20] if isinstance(data, dict) else [],
        "message": WechatIpadApiClient.extract_message(payload) or "",
        "code": WechatIpadApiClient.extract_code(payload),
        "http_status": payload.get("http_status"),
        "success": WechatIpadApiClient.is_success(payload),
    }


def _compact_wechat_ipad_runtime_info(runtime_info: dict[str, Any]) -> dict[str, Any]:
    """清除二维码图片和原始取码回包，只保留可长期写入 MariaDB TEXT 的短运行状态。"""

    compacted = dict(runtime_info)
    # 二维码图片由 874 返回的短 URL 展示，Base64 与媒体类型不属于渠道长期运行状态。
    compacted.pop("qrcode_image_base64", None)
    compacted.pop("qrcode_image_media_type", None)
    login_init_response = compacted.get("last_login_init_response")
    if isinstance(login_init_response, dict):
        compacted["last_login_init_response"] = _summarize_provider_payload(login_init_response)
    return compacted


def build_wechat_ipad_overview(
    channel: Channel,
    *,
    now: datetime | None = None,
    group_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    adapter_config = _parse_json(channel.adapter_config_json)
    account_snapshot = _build_account_snapshot(runtime_info, adapter_config)
    reference_time = now or now_shanghai()
    expiry = _build_qrcode_expiry_view(runtime_info, now=reference_time)
    last_check_qr_payload = runtime_info.get("last_check_qr_response") if isinstance(runtime_info.get("last_check_qr_response"), dict) else {}
    detailed_last_error = (
        WechatIpadApiClient.extract_detail_message(last_check_qr_payload)
        or WechatIpadApiClient.extract_message(last_check_qr_payload)
        or runtime_info.get("last_error")
    )
    login_profiles = _resolve_login_profiles(adapter_config)
    selected_login_profile = _resolve_login_profile_key(
        runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile")
    )
    selected_profile_config = login_profiles.get(selected_login_profile, {})
    qrcode_status = getattr(channel, "qrcode_status", "idle") or "idle"
    login_state = getattr(channel, "login_state", "unknown") or "unknown"
    qrcode_status_display = _derive_display_qrcode_status(qrcode_status, expiry["is_expired"])
    login_state_display = _derive_display_login_state(login_state)
    error_context = _build_error_context_view(runtime_info.get("last_error_context"), now=reference_time)
    message_stats = {
        "last_sync_count": runtime_info.get("last_message_sync_count", 0),
        "last_sync_ingested": runtime_info.get("last_message_sync_ingested", 0),
        "last_sync_deduplicated": runtime_info.get("last_message_sync_deduplicated", 0),
        "receive_total": runtime_info.get("receive_total", 0),
        "dedupe_total": runtime_info.get("dedupe_total", 0),
    }
    protocol_state = {
        "qr_uuid": runtime_info.get("login_qr_uuid"),
        "qr_message": runtime_info.get("login_qr_message"),
        "last_qrcode_at": runtime_info.get("last_qrcode_at"),
        "last_qrcode_at_display": _format_beijing_time(runtime_info.get("last_qrcode_at")),
        "last_get_qr_result": _build_recent_call_view(
            runtime_info.get("last_login_init_response"),
            fallback_message=runtime_info.get("login_qr_message"),
            called_at=runtime_info.get("last_qrcode_at"),
            now=reference_time,
        ),
        "last_check_qr_at": runtime_info.get("last_check_qr_at"),
        "last_check_qr_at_display": _format_beijing_time(runtime_info.get("last_check_qr_at")),
        "last_heartbeat_status": runtime_info.get("heartbeat_status", "unknown"),
        "last_heartbeat_long_status": runtime_info.get("heartbeat_long_status", "unknown"),
        "last_reconnect_method": runtime_info.get("reconnect_method"),
        "sync_key": runtime_info.get("sync_key") or "",
        "last_check_qr_result": _build_recent_call_view(
            runtime_info.get("last_check_qr_response"),
            fallback_message=runtime_info.get("login_qr_message"),
            called_at=runtime_info.get("last_check_qr_at"),
            now=reference_time,
        ),
        "last_heartbeat_result": _build_recent_call_view(
            runtime_info.get("last_heartbeat_response"),
            called_at=getattr(channel, "last_heartbeat_at", None),
            now=reference_time,
        ),
        "last_heartbeat_long_result": _build_recent_call_view(
            runtime_info.get("last_heartbeat_long_response"),
            called_at=runtime_info.get("last_heartbeat_long_at"),
            now=reference_time,
        ),
        "last_user_profile_result": _build_user_profile_view(
            runtime_info.get("last_user_profile_response"),
            runtime_info.get("user_profile_summary"),
            called_at=runtime_info.get("last_user_profile_at"),
            now=reference_time,
        ),
    }
    protocol_state["operator_snapshot"] = _build_recent_protocol_operator_snapshot(protocol_state)
    login_operator_snapshot = _build_login_operator_snapshot(
        login_state=login_state,
        qrcode_status=qrcode_status,
        qrcode_status_display=qrcode_status_display,
        qrcode_is_expired=expiry["is_expired"],
        qrcode_countdown_label=expiry["countdown_label"],
        qrcode_expires_at_display=expiry["expires_at_display"],
        last_login_at=getattr(channel, "last_login_at", None),
        qrcode_refresh_count=runtime_info.get("qrcode_refresh_count", 0),
        qrcode_message=runtime_info.get("login_qr_message"),
        qrcode_image_available=bool(_resolve_qrcode_image(runtime_info)),
        check_qr_result=protocol_state["last_check_qr_result"],
    )
    summaries = {
        "pull": runtime_info.get("last_pull_protocol_summary"),
        "inbound": runtime_info.get("last_inbound_protocol_summary"),
        "outbound": runtime_info.get("last_outbound_protocol_summary"),
    }
    sync_diagnostics = _build_sync_diagnostics(
        runtime_info,
        message_stats,
        summaries,
        last_message_sync_at=getattr(channel, "last_message_sync_at", None),
    )
    recent_message_flow = _build_recent_message_flow_summary(runtime_info, now=reference_time)
    error_diagnostics = _build_error_diagnostics(detailed_last_error, runtime_info.get("last_error_context"))
    error_operator_summary = _build_error_operator_summary(
        detailed_last_error,
        runtime_info.get("last_error_context"),
        now=reference_time,
    )
    error_operator_snapshot = _build_error_operator_snapshot(
        detailed_last_error,
        runtime_info.get("last_error_context"),
        error_diagnostics=error_diagnostics,
        operator_summary=error_operator_summary,
        now=reference_time,
    )
    plugin_scope_snapshot = (group_summary or {}).get("plugin_scope_snapshot") if isinstance(group_summary, dict) else None
    plugin_scope_brief = (
        ((group_summary or {}).get("plugin_scope_brief") if isinstance(group_summary, dict) else None)
        or _build_chatroom_plugin_scope_brief(plugin_scope_snapshot=plugin_scope_snapshot)
    )
    plugin_scope_overview = (
        ((group_summary or {}).get("plugin_scope_overview") if isinstance(group_summary, dict) else None)
        or _build_chatroom_plugin_scope_overview(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
        )
    )
    group_plugin_governance_snapshot = (
        ((group_summary or {}).get("group_plugin_governance_snapshot") if isinstance(group_summary, dict) else None)
        or _build_group_plugin_governance_snapshot(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
            plugin_scope_overview=plugin_scope_overview,
        )
    )
    group_enablement_model_snapshot = (
        ((group_summary or {}).get("group_enablement_model_snapshot") if isinstance(group_summary, dict) else None)
        or _build_group_enablement_model_snapshot(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
        )
    )
    group_allow_deny_model_snapshot = (
        ((group_summary or {}).get("group_allow_deny_model_snapshot") if isinstance(group_summary, dict) else None)
        or _build_group_allow_deny_model_snapshot(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
        )
    )
    current_ops_snapshot = _build_current_ops_snapshot(
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_state["operator_snapshot"],
        sync_operator_snapshot=sync_diagnostics["operator_snapshot"],
        message_operator_snapshot=recent_message_flow["operator_snapshot"],
        error_operator_snapshot=error_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=(group_summary or {}).get("stage_snapshot") if isinstance(group_summary, dict) else None,
        group_plugin_governance_snapshot=group_plugin_governance_snapshot,
    )
    top_priority_snapshot = _build_top_priority_snapshot(
        current_ops_snapshot=current_ops_snapshot,
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_state["operator_snapshot"],
        sync_operator_snapshot=sync_diagnostics["operator_snapshot"],
        message_operator_snapshot=recent_message_flow["operator_snapshot"],
        error_operator_snapshot=error_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=(group_summary or {}).get("stage_snapshot") if isinstance(group_summary, dict) else None,
        group_plugin_governance_snapshot=group_plugin_governance_snapshot,
    )
    action_console = _build_wechat_ipad_action_console(channel, runtime_info)
    return {
        "login_state": login_state,
        "login_state_display": login_state_display,
        "qrcode_status": qrcode_status,
        "qrcode_status_display": qrcode_status_display,
        "last_login_at": getattr(channel, "last_login_at", None),
        "last_login_at_display": _format_beijing_time(getattr(channel, "last_login_at", None)),
        "last_heartbeat_at": getattr(channel, "last_heartbeat_at", None),
        "last_heartbeat_at_display": _format_beijing_time(getattr(channel, "last_heartbeat_at", None)),
        "last_message_sync_at": getattr(channel, "last_message_sync_at", None),
        "last_message_sync_at_display": _format_beijing_time(getattr(channel, "last_message_sync_at", None)),
        "last_receive_at": runtime_info.get("last_receive_at"),
        "last_receive_at_display": _format_beijing_time(runtime_info.get("last_receive_at")),
        "last_send_at": runtime_info.get("last_send_at"),
        "last_send_at_display": _format_beijing_time(runtime_info.get("last_send_at")),
        "wxid": account_snapshot["wxid"],
        "device_id": str(adapter_config.get("device_id") or "").strip() or "-",
        "device_name": account_snapshot["device_name"],
        "selected_login_profile": selected_login_profile,
        "selected_login_profile_display": WECHAT_IPAD_LOGIN_PROFILE_LABELS.get(selected_login_profile, selected_login_profile),
        "available_login_profiles": [
            {
                "key": key,
                "label": WECHAT_IPAD_LOGIN_PROFILE_LABELS.get(key, key),
                "device_name": str((login_profiles.get(key) or {}).get("device_name") or "").strip(),
                "device_id": str((login_profiles.get(key) or {}).get("device_id") or "").strip(),
                "selected": key == selected_login_profile,
            }
            for key in WECHAT_IPAD_LOGIN_PROFILE_KEYS
        ],
        "selected_profile_device_name": str(selected_profile_config.get("device_name") or adapter_config.get("device_name") or "").strip() or "-",
        "selected_profile_device_id": str(selected_profile_config.get("device_id") or adapter_config.get("device_id") or "").strip() or "-",
        "nickname": account_snapshot["nickname"],
        "alias": account_snapshot["alias"],
        "bind_mobile": account_snapshot["bind_mobile"],
        "qrcode_url": runtime_info.get("qrcode_url"),
        "qrcode_link_url": _resolve_qrcode_link_url(runtime_info),
        "qrcode_image_url": f"/channels/{channel.id}/wechat-ipad/qrcode-image" if _resolve_qrcode_image(runtime_info) else None,
        "qrcode_visible": login_state != "online" and qrcode_status != "success",
        "qrcode_uuid": runtime_info.get("login_qr_uuid"),
        "qrcode_message": runtime_info.get("login_qr_message"),
        "login_init_endpoint": str(
            runtime_info.get("last_login_init_endpoint")
            or _login_init_endpoint_for_profile(selected_login_profile)
        ),
        "qrcode_expires_at": expiry["expires_at"],
        "qrcode_expires_at_display": expiry["expires_at_display"],
        "qrcode_remaining_seconds": expiry["remaining_seconds"],
        "qrcode_countdown_label": expiry["countdown_label"],
        "qrcode_is_expired": expiry["is_expired"],
        "qrcode_refresh_count": runtime_info.get("qrcode_refresh_count", 0),
        "last_error": detailed_last_error,
        "last_error_context": error_context,
        "last_error_stage": error_context["stage"],
        "last_error_endpoint": error_context["endpoint"],
        "last_error_at": error_context["at"],
        "last_error_at_display": error_context["at_display"],
        "last_error_operator_summary": error_operator_summary,
        "error_diagnostics": error_diagnostics,
        "error_operator_snapshot": error_operator_snapshot,
        "plugin_scope_brief": plugin_scope_brief,
        "plugin_scope_overview": plugin_scope_overview,
        "group_plugin_governance_snapshot": group_plugin_governance_snapshot,
        "group_enablement_model_snapshot": group_enablement_model_snapshot,
        "group_allow_deny_model_snapshot": group_allow_deny_model_snapshot,
        "current_ops_snapshot": current_ops_snapshot,
        "top_priority_snapshot": top_priority_snapshot,
        "action_console": action_console,
        "last_status_summary": runtime_info.get("last_status_summary"),
        "last_sync_summary": runtime_info.get("last_sync_summary"),
        "last_chatroom_sync_summary": runtime_info.get("last_chatroom_sync_summary"),
        "last_receive_summary": runtime_info.get("last_receive_summary"),
        "last_send_summary": runtime_info.get("last_send_summary"),
        "last_receive_event_key": runtime_info.get("last_receive_event_key"),
        "last_dedupe_event_key": runtime_info.get("last_dedupe_event_key"),
        "heartbeat_status": runtime_info.get("heartbeat_status", "unknown"),
        "reconnect_status": runtime_info.get("reconnect_status", "idle"),
        "login_flow_polling_active": _is_polling_active(
            login_state=getattr(channel, "login_state", "unknown") or "unknown",
            qrcode_status=qrcode_status,
            is_expired=expiry["is_expired"],
        ),
        "message_stats": message_stats,
        "protocol_state": protocol_state,
        "login_operator_snapshot": login_operator_snapshot,
        "protocol_summaries": summaries,
        "sync_diagnostics": sync_diagnostics,
        "recent_message_flow": recent_message_flow,
        "entrypoints": {
            "message_pull": f"/channels/{channel.id}/wechat-ipad/pull-messages",
            "inbound_receive": f"/webhooks/{channel.id}/inbound",
            "outbound_send": f"/channels/{channel.id}/outbound",
        },
        "hooks": {
            # 运行概览只展示固定 874 契约，旧配置中的端点键不会再影响真实调用。
            "login_init_endpoint": IPAD_874_CONTRACT.endpoints.login_get_qr,
            "login_init_car_endpoint": IPAD_874_CONTRACT.endpoints.login_get_qr_car,
            "check_qr_endpoint": IPAD_874_CONTRACT.endpoints.login_check_qr,
            "heartbeat_endpoint": IPAD_874_CONTRACT.endpoints.login_heartbeat,
            "heartbeat_long_endpoint": IPAD_874_CONTRACT.endpoints.login_heartbeat_long,
            "user_profile_endpoint": IPAD_874_CONTRACT.endpoints.user_get_profile,
            "awaken_endpoint": IPAD_874_CONTRACT.endpoints.login_awaken,
            "twice_auto_auth_endpoint": IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
            "message_sync_endpoint": IPAD_874_CONTRACT.endpoints.message_sync,
        },
        "runtime_info": runtime_info,
        "adapter_config": adapter_config,
    }


def _build_wechat_ipad_action_console(channel: Channel, runtime_info: dict[str, Any]) -> dict[str, Any]:
    action_endpoint_template = f"/channels/{channel.id}/wechat-ipad/actions/__ACTION__"
    return {
        "endpoint_template": action_endpoint_template,
        "supported_actions": [
            {
                "key": "get_contact_list",
                "label": "同步联系人列表",
                "description": "用于拉取联系人与群联系人游标，适合先确认通讯录同步是否正常。",
                "sample_payload": {
                    "current_wxcontact_seq": 0,
                    "current_chatroom_contact_seq": 0,
                },
            },
            {
                "key": "get_contact_detail",
                "label": "查看联系人详情",
                "description": "按 wxid 批量查询联系人详情，也支持附带 chatroom_id 做群成员上下文查询。",
                "sample_payload": {
                    "to_wxids": ["wxid_demo_contact_001"],
                    "chatroom_id": "",
                },
            },
            {
                "key": "get_profile",
                "label": "查看个人资料",
                "description": "默认查询当前登录账号，也可改成目标 wxid。",
                "sample_payload": {
                    "target_wxid": "",
                },
            },
            {
                "key": "get_my_qrcode",
                "label": "获取个人二维码",
                "description": "验证账号侧二维码接口是否可用。",
                "sample_payload": {
                    "style": 0,
                },
            },
            {
                "key": "get_chatroom_qrcode",
                "label": "获取群二维码",
                "description": "用于核对群二维码接口与 chatroom_id 是否匹配。",
                "sample_payload": {
                    "chatroom_id": "123456@chatroom",
                },
            },
            {
                "key": "get_chatroom_announce",
                "label": "获取群公告",
                "description": "验证群资料详情接口返回结构。",
                "sample_payload": {
                    "chatroom_id": "123456@chatroom",
                },
            },
            {
                "key": "invite_chatroom_member",
                "label": "邀请群成员",
                "description": "运维侧邀请成员入群，需显式提供群 ID 和 wxid 列表。",
                "sample_payload": {
                    "chatroom_id": "123456@chatroom",
                    "to_wxids": ["wxid_demo_member_001"],
                },
            },
            {
                "key": "accept_friend",
                "label": "通过好友申请",
                "description": "根据 abot client 口径传 scene / v1 / v2。",
                "sample_payload": {
                    "scene": 17,
                    "v1": "encryptusername",
                    "v2": "ticket",
                },
            },
            {
                "key": "download_image",
                "label": "下载图片附件",
                "description": "按消息内 aeskey 与 file_no 拉取图片。",
                "sample_payload": {
                    "file_aes_key": "aes-key",
                    "file_no": "cdnmidimgurl",
                },
            },
            {
                "key": "download_voice",
                "label": "下载语音附件",
                "description": "按消息 ID、发送方、缓冲区标识和语音长度下载语音。",
                "sample_payload": {
                    "msg_id": "1234567890",
                    "from_user_name": "room-001@chatroom",
                    "bufid": "99887766",
                    "length": 3200,
                },
            },
            {
                "key": "download_video",
                "label": "下载视频附件",
                "description": "按消息 ID、发送方和分片范围下载视频。",
                "sample_payload": {
                    "msg_id": "1234567890",
                    "to_wxid": "wxid_sender",
                    "data_len": 65536,
                    "start_pos": 0,
                    "section_length": 32768,
                    "compress_type": 0,
                },
            },
        ],
        "last_action_name": runtime_info.get("last_action_name"),
        "last_action_at": runtime_info.get("last_action_at"),
        "last_action_ok": runtime_info.get("last_action_ok"),
        "last_action_summary": runtime_info.get("last_action_summary"),
        "last_action_params": runtime_info.get("last_action_params") or {},
        "last_action_result_preview": runtime_info.get("last_action_result_preview") or {},
    }


def sync_wechat_ipad_contacts(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    if not wxid:
        return _persist_runtime(
            db,
            channel,
            summary="联系人同步失败：缺少 wxid。",
            error="missing_wxid",
            extra={"last_contact_sync_summary": "联系人同步失败：缺少 wxid。"},
        )

    safe_limit = max(1, min(int(limit or 200), 500))
    list_result = _safe_call(
        api_client.get_contact_list,
        stage="contact_sync",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_list,
        channel=channel,
        wxid=wxid,
        current_wxcontact_seq=0,
        current_chatroom_contact_seq=0,
    )
    if not list_result.get("ok"):
        summary = f"联系人同步失败：{str(list_result.get('error') or 'provider_error')}"
        return _persist_runtime(
            db,
            channel,
            summary=summary,
            error=str(list_result.get("error") or "provider_error"),
            error_context=list_result.get("error_context"),
            extra={"last_contact_sync_summary": summary},
        )

    list_data = list_result.get("data") if isinstance(list_result.get("data"), dict) else {}
    normalized_contacts = [_normalize_wechat_contact_item(item) for item in _extract_contact_items(list_data)]
    normalized_contacts = [item for item in normalized_contacts if item]
    if len(normalized_contacts) > safe_limit:
        normalized_contacts = normalized_contacts[:safe_limit]

    personal_contacts = [item for item in normalized_contacts if item.get("is_personal_contact")]
    wxids_for_detail = [str(item.get("wxid") or "").strip() for item in personal_contacts if str(item.get("wxid") or "").strip()]
    wxids_for_detail = wxids_for_detail[: min(len(wxids_for_detail), 80)]

    detail_map: dict[str, dict[str, Any]] = {}
    if wxids_for_detail:
        detail_result = _safe_call(
            api_client.get_contact_detail,
            stage="contact_detail",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_detail,
            channel=channel,
            wxid=wxid,
            to_wxids=wxids_for_detail,
            chatroom_id="",
        )
        if detail_result.get("ok"):
            detail_data = detail_result.get("data") if isinstance(detail_result.get("data"), dict) else {}
            for item in _extract_contact_items(detail_data):
                normalized = _normalize_wechat_contact_item(item)
                detail_wxid = str(normalized.get("wxid") or "").strip()
                if detail_wxid:
                    detail_map[detail_wxid] = normalized

    merged_contacts: list[dict[str, Any]] = []
    for item in normalized_contacts:
        wxid_value = str(item.get("wxid") or "").strip()
        if wxid_value and wxid_value in detail_map:
            merged_contacts.append(_merge_contact_fields(item, detail_map[wxid_value]))
        else:
            merged_contacts.append(item)

    merged_personal = [item for item in merged_contacts if item.get("is_personal_contact")]
    chatroom_count = max(len(merged_contacts) - len(merged_personal), 0)
    seq = _extract_contact_seq_fields(list_data)
    synced_at = _utcnow_iso()
    summary = (
        f"联系人同步完成：个人联系人 {len(merged_personal)} 个，"
        f"群/其他联系人 {chatroom_count} 个。"
    )
    snapshot = {
        "synced_at": synced_at,
        "wxid": wxid,
        "total_count": len(merged_contacts),
        "personal_count": len(merged_personal),
        "chatroom_count": chatroom_count,
        "seq": seq,
        "contacts": merged_contacts,
    }
    return _persist_runtime(
        db,
        channel,
        summary=summary,
        error=None,
        extra={
            "last_contact_sync_at": synced_at,
            "last_contact_sync_summary": summary,
            "last_contact_sync_snapshot": snapshot,
        },
    )


def build_wechat_ipad_contact_list(channel: Channel, *, limit: int = 100) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    snapshot = runtime_info.get("last_contact_sync_snapshot")
    if not isinstance(snapshot, dict):
        return {
            "has_data": False,
            "summary": runtime_info.get("last_contact_sync_summary") or "还没有联系人同步记录，请先执行“同步联系人”。",
            "synced_at_display": _format_beijing_time(runtime_info.get("last_contact_sync_at")),
            "personal_count": 0,
            "chatroom_count": 0,
            "total_count": 0,
            "seq": {},
            "contacts": [],
        }

    contacts = snapshot.get("contacts") if isinstance(snapshot.get("contacts"), list) else []
    personal_contacts = [item for item in contacts if isinstance(item, dict) and bool(item.get("is_personal_contact"))]
    safe_limit = max(1, min(int(limit or 100), 500))
    return {
        "has_data": True,
        "summary": runtime_info.get("last_contact_sync_summary") or "已同步联系人列表。",
        "synced_at_display": _format_beijing_time(snapshot.get("synced_at") or runtime_info.get("last_contact_sync_at")),
        "personal_count": int(snapshot.get("personal_count") or len(personal_contacts)),
        "chatroom_count": int(snapshot.get("chatroom_count") or max(len(contacts) - len(personal_contacts), 0)),
        "total_count": int(snapshot.get("total_count") or len(contacts)),
        "seq": snapshot.get("seq") if isinstance(snapshot.get("seq"), dict) else {},
        "contacts": personal_contacts[:safe_limit],
    }


def build_wechat_ipad_group_summary(db: Session, channel: Channel, *, now: datetime | None = None) -> dict[str, Any]:
    bot_plugin_count = int(
        db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound")) or 0
    )
    enabled_bot_plugin_count = int(
        db.scalar(
            select(func.count(BotPlugin.id)).where(
                BotPlugin.bot_id == channel.bot_id,
                BotPlugin.status == "enabled",
            )
        )
        or 0
    )
    plugin_registry_count = int(db.scalar(select(func.count(Plugin.id))) or 0)
    chatroom_count = db.scalar(select(func.count(Chatroom.id)).where(Chatroom.channel_id == channel.id)) or 0
    member_count = db.scalar(
        select(func.count(ChatroomMember.id))
        .join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id)
        .where(Chatroom.channel_id == channel.id)
    ) or 0
    member_synced_chatroom_count = db.scalar(
        select(func.count(func.distinct(ChatroomMember.chatroom_id)))
        .join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id)
        .where(Chatroom.channel_id == channel.id)
    ) or 0
    latest_member_synced_at = db.scalar(
        select(func.max(ChatroomMember.last_synced_at))
        .join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id)
        .where(Chatroom.channel_id == channel.id)
    )
    latest_chatroom_synced_at = db.scalar(select(func.max(Chatroom.last_synced_at)).where(Chatroom.channel_id == channel.id))
    status_counts_raw = db.execute(
        select(Chatroom.status, func.count(Chatroom.id))
        .where(Chatroom.channel_id == channel.id)
        .group_by(Chatroom.status)
    ).all()
    status_counts = {str(status or "unknown"): int(count or 0) for status, count in status_counts_raw}
    latest_chatroom = db.scalars(
        select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.updated_at.desc(), Chatroom.id.desc()).limit(1)
    ).first()
    recent_chatrooms = db.scalars(
        select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.updated_at.desc(), Chatroom.id.desc()).limit(5)
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope).where(GroupPluginScope.channel_id == channel.id).order_by(GroupPluginScope.id.asc())
    ).all()
    latest_source = _extract_chatroom_source(latest_chatroom)
    scope_stats = _summarize_group_plugin_scope_rows(scope_rows)
    recent_items = [
        {
            "id": chatroom.id,
            "name": chatroom.name or chatroom.external_id,
            "external_id": chatroom.external_id,
            "status": chatroom.status or "unknown",
            "status_label": _format_chatroom_status(chatroom.status),
            "member_count": int(chatroom.member_count or 0),
            "last_synced_at": _format_beijing_time(chatroom.last_synced_at),
            "observed_source": _extract_chatroom_source(chatroom),
            "is_message_observed_only": str(chatroom.status or "") == "message_observed",
            "scope_total": scope_stats["per_chatroom"].get(chatroom.id, {}).get("total", 0),
            "scope_overridden": scope_stats["per_chatroom"].get(chatroom.id, {}).get("overridden", 0),
        }
        for chatroom in recent_chatrooms
    ]
    message_observed_count = int(status_counts.get("message_observed", 0))
    active_chatroom_count = max(int(chatroom_count) - message_observed_count, 0)
    has_data = int(chatroom_count) > 0
    sync_depth = _build_chatroom_sync_depth(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
    )
    visibility_snapshot = _build_chatroom_visibility_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        recent_items=recent_items,
        sync_depth=sync_depth,
    )
    coverage_snapshot = _build_chatroom_coverage_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        latest_synced_at=_format_beijing_time(latest_chatroom_synced_at),
        latest_member_synced_at=_format_beijing_time(latest_member_synced_at),
        latest_source=latest_source,
        sync_freshness=_build_chatroom_sync_freshness(latest_chatroom_synced_at, now=now),
    )
    operator_snapshot = _build_chatroom_operator_snapshot(
        has_data=has_data,
        message_observed_count=message_observed_count,
        sync_depth=sync_depth,
        visibility_snapshot=visibility_snapshot,
    )
    ops_snapshot = _build_chatroom_ops_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        sync_depth=sync_depth,
        latest_source=latest_source,
        sync_freshness=coverage_snapshot["sync_freshness"],
    )
    plugin_scope_snapshot = _build_chatroom_plugin_scope_snapshot(
        chatroom_count=int(chatroom_count),
        active_chatroom_count=active_chatroom_count,
        message_observed_count=message_observed_count,
        member_synced_chatroom_count=int(member_synced_chatroom_count),
        bot_plugin_count=bot_plugin_count,
        enabled_bot_plugin_count=enabled_bot_plugin_count,
        plugin_registry_count=plugin_registry_count,
        latest_source=latest_source,
        sync_freshness=coverage_snapshot["sync_freshness"],
        persisted_scope_stats=scope_stats,
    )
    plugin_scope_brief = _build_chatroom_plugin_scope_brief(plugin_scope_snapshot=plugin_scope_snapshot)
    plugin_scope_overview = _build_chatroom_plugin_scope_overview(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
    )
    group_plugin_governance_snapshot = _build_group_plugin_governance_snapshot(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
        plugin_scope_overview=plugin_scope_overview,
    )
    group_enablement_model_snapshot = _build_group_enablement_model_snapshot(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
    )
    group_allow_deny_model_snapshot = _build_group_allow_deny_model_snapshot(
        plugin_scope_snapshot=plugin_scope_snapshot,
        plugin_scope_brief=plugin_scope_brief,
    )
    stage_snapshot = _build_chatroom_stage_snapshot(
        sync_depth=sync_depth,
        ops_snapshot=ops_snapshot,
        sync_freshness=coverage_snapshot["sync_freshness"],
    )
    summary_snapshot = _build_chatroom_summary_snapshot(
        sync_depth=sync_depth,
        ops_snapshot=ops_snapshot,
        sync_freshness=coverage_snapshot["sync_freshness"],
    )
    return {
        "chatroom_count": int(chatroom_count),
        "member_count": int(member_count),
        "member_synced_chatroom_count": int(member_synced_chatroom_count),
        "latest_chatroom_name": (latest_chatroom.name or latest_chatroom.external_id) if latest_chatroom else "-",
        "latest_synced_at": _format_beijing_time(latest_chatroom_synced_at),
        "latest_member_synced_at": _format_beijing_time(latest_member_synced_at),
        "latest_observed_source": latest_source,
        "message_observed_count": message_observed_count,
        "active_chatroom_count": active_chatroom_count,
        "status_breakdown": status_counts,
        "recent_chatrooms": recent_items,
        "sync_depth": sync_depth,
        "visibility_snapshot": visibility_snapshot,
        "coverage_snapshot": coverage_snapshot,
        "operator_snapshot": operator_snapshot,
        "ops_snapshot": ops_snapshot,
        "stage_snapshot": stage_snapshot,
        "summary_snapshot": summary_snapshot,
        "plugin_scope_snapshot": plugin_scope_snapshot,
        "plugin_scope_brief": plugin_scope_brief,
        "plugin_scope_overview": plugin_scope_overview,
        "group_plugin_governance_snapshot": group_plugin_governance_snapshot,
        "group_enablement_model_snapshot": group_enablement_model_snapshot,
        "group_allow_deny_model_snapshot": group_allow_deny_model_snapshot,
        "persisted_scope_stats": scope_stats,
        "has_chatroom_data": has_data,
        "data_model_ready": True,
        "empty_state_message": (
            "尚未观测到群资料；本地 chatrooms / chatroom_members 实体骨架已就位，等待后续同步或消息驱动落库。"
            if not has_data
            else "已能在后台看到本地群资料摘要，可用于核对最近落库与同步进度。"
        ),
        "sync_visibility_note": (
            "当前大部分仍可能只是 message_observed 骨架，表示后台已观测到群消息并开始落库。"
            if message_observed_count
            else "当前摘要未发现 message_observed 骨架，可继续核对是否已有更完整群资料来源。"
        ),
        "capability_note": "当前仅为本地落库扩展位，不代表已完成真实群资料/群成员同步。",
    }


def _extract_chatroom_source(chatroom: Chatroom | None) -> str:
    if not chatroom:
        return "-"
    payload = _parse_json(chatroom.extra_json)
    source = str(payload.get("source") or "").strip()
    return source or "-"


def _build_chatroom_visibility_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    recent_items: list[dict[str, Any]],
    sync_depth: dict[str, Any],
) -> dict[str, Any]:
    recent_sources: list[str] = []
    for item in recent_items:
        source = str(item.get("observed_source") or "").strip()
        if not source or source == "-" or source in recent_sources:
            continue
        recent_sources.append(source)

    progress_items = [
        {"label": "已观测群", "value": chatroom_count, "tone": "outline"},
        {"label": "仅消息骨架", "value": message_observed_count, "tone": "muted"},
        {"label": "已补资料", "value": active_chatroom_count, "tone": "success"},
        {"label": "已有成员记录", "value": member_synced_chatroom_count, "tone": "success"},
    ]
    if chatroom_count <= 0:
        return {
            "summary_label": str(sync_depth.get("status_label") or "尚未形成群资料同步样本"),
            "detail_label": "当前后台还没有 chatroom / member 落库记录；一旦消息驱动或真实同步写入，这里会先显示覆盖度和来源。",
            "operator_summary": str(sync_depth.get("operator_summary") or ""),
            "status_key": str(sync_depth.get("status_key") or "no_sample"),
            "status_label": str(sync_depth.get("status_label") or "尚未形成群资料同步样本"),
            "status_tone": str(sync_depth.get("tone") or "muted"),
            "recent_sources": [],
            "progress_items": progress_items,
        }

    summary_label = str(sync_depth.get("status_label") or "")

    recent_source_label = "、".join(recent_sources) if recent_sources else "暂无明确来源"
    detail_label = (
        f"最近来源：{recent_source_label}；"
        f"其中 {message_observed_count} 个群仍是 message_observed 骨架。"
    )
    return {
        "summary_label": summary_label,
        "detail_label": detail_label,
        "operator_summary": str(sync_depth.get("operator_summary") or ""),
        "status_key": str(sync_depth.get("status_key") or "unknown"),
        "status_label": str(sync_depth.get("status_label") or summary_label),
        "status_tone": str(sync_depth.get("tone") or "outline"),
        "recent_sources": recent_sources,
        "progress_items": progress_items,
    }


def _build_chatroom_operator_snapshot(
    *,
    has_data: bool,
    message_observed_count: int,
    sync_depth: dict[str, Any],
    visibility_snapshot: dict[str, Any],
) -> dict[str, str]:
    if not has_data:
        detail = "当前还没有本地群资料样本；后续一旦消息驱动或真实同步写入，这里会先出现同步覆盖度与来源。"
    elif message_observed_count > 0:
        detail = (
            f"当前仍有 {message_observed_count} 个群停留在 message_observed 骨架阶段；"
            "建议继续核对是否已拿到真实群资料或成员同步样本。"
        )
    else:
        detail = "当前摘要未发现 message_observed 骨架，可继续核对最近来源是否稳定提供完整群资料。"
    return {
        "status_key": str(sync_depth.get("status_key") or "unknown"),
        "status_label": str(sync_depth.get("status_label") or "未知"),
        "status_tone": str(sync_depth.get("tone") or "outline"),
        "headline": (
            f"当前群资料同步档位：{str(sync_depth.get('status_label') or '未知')}"
        ),
        "summary": str(sync_depth.get("operator_summary") or ""),
        "detail": detail,
        "recent_source_summary": str(visibility_snapshot.get("detail_label") or ""),
    }


def _build_chatroom_ops_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    sync_depth: dict[str, Any],
    latest_source: str,
    sync_freshness: dict[str, str],
) -> dict[str, str]:
    status_key = str(sync_depth.get("status_key") or "unknown")
    status_label = str(sync_depth.get("status_label") or "未知")
    status_tone = str(sync_depth.get("tone") or "outline")

    if status_key == "no_sample":
        evidence = "当前没有本地 chatroom 或 chatroom_members 样本。"
        action = "先等待首批群消息或群资料同步落库，再判断是否进入下一档。"
    elif status_key == "message_skeleton_only":
        evidence = f"已观测 {chatroom_count} 个群，但 {message_observed_count} 个群仍全部停留在 message_observed 骨架。"
        action = "优先补群资料同步，至少让部分群进入“已有群资料”档。"
    elif status_key == "profile_without_members":
        evidence = (
            f"已观测 {chatroom_count} 个群，其中 {active_chatroom_count} 个群已有资料，"
            "但成员快照仍为 0。"
        )
        action = "优先补成员快照或成员同步结果，确认群成员链路已开始落库。"
    else:
        evidence = (
            f"已观测 {chatroom_count} 个群，{active_chatroom_count} 个群已有资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录。"
        )
        action = "当前可继续核对最近来源和成员覆盖，进入更细的群运维排查。"

    source_hint = f"最近来源：{latest_source}。" if latest_source and latest_source != "-" else "最近来源：暂无明确来源。"
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"当前群资料同步判断：{status_label}",
        "summary": f"当前处于“{status_label}”档。{evidence}",
        "evidence": evidence,
        "action": action,
        "source_hint": source_hint,
        "freshness_label": str(sync_freshness.get("label") or "尚无时间样本"),
        "freshness_key": str(sync_freshness.get("key") or "no_sample"),
        "freshness_tone": str(sync_freshness.get("tone") or "muted"),
        "freshness_hint": str(sync_freshness.get("hint") or ""),
    }


def _build_chatroom_stage_snapshot(
    *,
    sync_depth: dict[str, Any],
    ops_snapshot: dict[str, Any],
    sync_freshness: dict[str, Any],
) -> dict[str, str]:
    status_label = str(sync_depth.get("status_label") or "未知")
    status_key = str(sync_depth.get("status_key") or "unknown")
    status_tone = str(sync_depth.get("tone") or "outline")
    evidence = str(ops_snapshot.get("evidence") or "")
    action = str(ops_snapshot.get("action") or "")
    freshness_label = str(sync_freshness.get("label") or "尚无时间样本")

    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"群资料同步一眼判断：{status_label}",
        "operator_summary": f"{status_label}；最近同步时效：{freshness_label}；{evidence}；下一步：{action}",
        "evidence": evidence,
        "action": action,
        "freshness_key": str(sync_freshness.get("key") or "no_sample"),
        "freshness_label": freshness_label,
        "freshness_tone": str(sync_freshness.get("tone") or "muted"),
        "freshness_hint": str(sync_freshness.get("hint") or ""),
    }


def _build_chatroom_summary_snapshot(
    *,
    sync_depth: dict[str, Any],
    ops_snapshot: dict[str, Any],
    sync_freshness: dict[str, Any],
) -> dict[str, str]:
    status_key = str(sync_depth.get("status_key") or "unknown")
    status_label = str(sync_depth.get("status_label") or "未知")
    status_tone = str(sync_depth.get("tone") or "outline")
    freshness_key = str(sync_freshness.get("key") or "no_sample")
    freshness_label = str(sync_freshness.get("label") or "尚无时间样本")
    freshness_tone = str(sync_freshness.get("tone") or "muted")
    evidence = str(ops_snapshot.get("evidence") or "")
    next_step = str(ops_snapshot.get("action") or "")

    if status_key == "no_sample":
        decision_label = "待首批样本"
        decision_tone = "muted"
    elif status_key == "message_skeleton_only":
        decision_label = "待补群资料"
        decision_tone = "muted"
    elif status_key == "profile_without_members":
        decision_label = "待补成员快照"
        decision_tone = "outline"
    elif freshness_key in {"stale", "expired"}:
        decision_label = "样本偏旧"
        decision_tone = "outline"
    else:
        decision_label = "可继续排查"
        decision_tone = "success"

    decision_summary = (
        f"档位={status_label}；时效={freshness_label}；判断={decision_label}；"
        f"证据={evidence}；下一步={next_step}"
    )
    short_summary = f"{status_label} / {decision_label}"
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "freshness_key": freshness_key,
        "freshness_label": freshness_label,
        "freshness_tone": freshness_tone,
        "decision_label": decision_label,
        "decision_tone": decision_tone,
        "headline": f"群资料运维摘要：{short_summary}",
        "short_summary": short_summary,
        "operator_summary": decision_summary,
        "evidence": evidence,
        "next_step": next_step,
    }


def _build_chatroom_coverage_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    latest_synced_at: str,
    latest_member_synced_at: str,
    latest_source: str,
    sync_freshness: dict[str, str],
    persisted_scope_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    persisted_scope_stats = persisted_scope_stats or {}
    profile_ratio = _build_ratio_item(
        label="群资料覆盖",
        numerator=active_chatroom_count,
        denominator=chatroom_count,
        tone="success" if active_chatroom_count > 0 else "outline",
    )
    member_ratio = _build_ratio_item(
        label="成员记录覆盖",
        numerator=member_synced_chatroom_count,
        denominator=chatroom_count,
        tone="success" if member_synced_chatroom_count > 0 else "outline",
    )
    skeleton_ratio = _build_ratio_item(
        label="消息骨架占比",
        numerator=message_observed_count,
        denominator=chatroom_count,
        tone="muted" if message_observed_count > 0 else "success",
    )
    if chatroom_count <= 0:
        return {
            "summary_label": "暂无群资料样本",
            "operator_summary": "当前还没有 chatroom / chatroom_members 本地样本，覆盖比例会在首次落库后显示。",
            "items": [profile_ratio, member_ratio, skeleton_ratio],
            "sync_freshness": sync_freshness,
            "detail_items": [
                {"label": "最近群资料同步", "value": latest_synced_at},
                {"label": "最近同步时效", "value": str(sync_freshness.get("label") or "尚无时间样本")},
                {"label": "最近成员记录同步", "value": latest_member_synced_at},
                {"label": "最近观测来源", "value": latest_source},
            ],
        }

    return {
        "summary_label": (
            f"已观测 {chatroom_count} 个群，群资料覆盖 {profile_ratio['percent_label']}，"
            f"成员记录覆盖 {member_ratio['percent_label']}，消息骨架占比 {skeleton_ratio['percent_label']}。"
        ),
        "operator_summary": (
            f"最近样本里，{active_chatroom_count} 个群已补基础资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录，"
            f"{message_observed_count} 个群仍停留在 message_observed 骨架。"
        ),
        "sync_freshness": sync_freshness,
        "items": [profile_ratio, member_ratio, skeleton_ratio],
        "detail_items": [
            {"label": "最近群资料同步", "value": latest_synced_at},
            {"label": "最近同步时效", "value": str(sync_freshness.get("label") or "尚无时间样本")},
            {"label": "最近成员记录同步", "value": latest_member_synced_at},
            {"label": "最近观测来源", "value": latest_source},
        ],
    }


def _build_chatroom_plugin_scope_snapshot(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    message_observed_count: int,
    member_synced_chatroom_count: int,
    bot_plugin_count: int,
    enabled_bot_plugin_count: int,
    plugin_registry_count: int,
    latest_source: str,
    sync_freshness: dict[str, str],
    persisted_scope_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    persisted_scope_stats = persisted_scope_stats or {}
    freshness_label = str(sync_freshness.get("label") or "尚无时间样本")
    freshness_tone = str(sync_freshness.get("tone") or "muted")
    freshness_hint = str(sync_freshness.get("hint") or "")
    source_hint = f"最近来源：{latest_source}。" if latest_source and latest_source != "-" else "最近来源：暂无明确来源。"
    preconditions = [
        {
            "label": "群资料实体",
            "value": str(chatroom_count),
            "state": "ready" if chatroom_count > 0 else "missing",
            "summary": f"本地 chatrooms 已有 {chatroom_count} 个样本" if chatroom_count > 0 else "本地 chatrooms 仍无样本",
        },
        {
            "label": "成员记录群",
            "value": str(member_synced_chatroom_count),
            "state": "ready" if member_synced_chatroom_count > 0 else "pending",
            "summary": (
                f"已有 {member_synced_chatroom_count} 个群具备 chatroom_members"
                if member_synced_chatroom_count > 0
                else "chatroom_members 仍未形成稳定样本"
            ),
        },
        {
            "label": "bot 插件绑定",
            "value": f"{enabled_bot_plugin_count} / {bot_plugin_count}",
            "state": "ready" if enabled_bot_plugin_count > 0 else "pending" if bot_plugin_count > 0 else "missing",
            "summary": (
                f"bot 插件绑定已存在，启用 {enabled_bot_plugin_count} / 总计 {bot_plugin_count}"
                if bot_plugin_count > 0
                else "bot 侧还没有插件绑定样本"
            ),
        },
    ]
    missing_capabilities = [
        "缺群级 enable/disable 映射",
        "缺 allow/deny 持久化口径",
        "缺群级命令/能力边界模型",
        "缺审计与运营动作留痕",
    ]

    if chatroom_count <= 0:
        stage_key = "not_ready"
        stage_label = "尚未具备"
        stage_tone = "muted"
        evidence = "当前没有本地 chatroom / chatroom_members 样本，群级插件权限仍停留在扩展位骨架。"
        action = "先补首批群消息或群资料样本，再判断是否进入群级权限建模。"
        summary = "后台已预留群级插件权限治理入口，但当前还没有足够本地群资料支撑下一步。"
    elif active_chatroom_count <= 0:
        stage_key = "skeleton_only"
        stage_label = "仅有群资料骨架"
        stage_tone = "muted"
        evidence = f"已观测 {chatroom_count} 个群，但 {message_observed_count} 个群仍全部停留在 message_observed 骨架。"
        action = "先补真实群资料同步，至少让部分群进入非骨架状态，再评估群级插件权限边界。"
        summary = "当前只能确认后台已开始观测群聊，仍不足以支撑群级插件权限建模。"
    elif member_synced_chatroom_count <= 0:
        stage_key = "profile_only"
        stage_label = "仅有群资料"
        stage_tone = "outline"
        evidence = (
            f"已观测 {chatroom_count} 个群，其中 {active_chatroom_count} 个群已有资料，"
            "但成员快照仍为 0。"
        )
        action = "下一步先补成员快照，确认本地样本能支撑按群做插件治理。"
        summary = "当前已有群资料基础，但群级插件权限仍缺成员维度证据，暂不适合落真实规则。"
    else:
        stage_key = "ready_for_modeling"
        stage_label = "可进入群级权限建模"
        stage_tone = "success"
        evidence = (
            f"已观测 {chatroom_count} 个群，{active_chatroom_count} 个群已有资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录。"
        )
        action = "可开始设计群级启停 / allow/deny / 能力边界的数据口径，但仍需真实样本继续对齐。"
        summary = "当前本地群资料已足够进入群级插件权限建模准备阶段，可继续收口最小规则边界。"

    readiness_summary = (
        f"前置条件：群资料={chatroom_count}，成员记录群={member_synced_chatroom_count}，"
        f"bot 插件绑定启用/总计={enabled_bot_plugin_count}/{bot_plugin_count}，插件镜像={plugin_registry_count}。"
    )
    persisted_scope_summary = (
        f"persisted scopes={persisted_scope_stats.get('total', 0)}, "
        f"inherited={persisted_scope_stats.get('inherited', 0)}, overridden={persisted_scope_stats.get('overridden', 0)}, "
        f"chatrooms={persisted_scope_stats.get('overridden_chatrooms', 0)}/{persisted_scope_stats.get('scoped_chatrooms', 0)}"
    )
    return {
        "stage_key": stage_key,
        "stage_label": stage_label,
        "stage_tone": stage_tone,
        "headline": f"群级插件权限准备度：{stage_label}",
        "operator_summary": f"{stage_label}；{evidence}；下一步：{action}",
        "summary": summary,
        "evidence": evidence,
        "action": action,
        "readiness_summary": readiness_summary,
        "persisted_scope_summary": persisted_scope_summary,
        "persisted_scope_total": int(persisted_scope_stats.get("total", 0)),
        "persisted_scope_inherited": int(persisted_scope_stats.get("inherited", 0)),
        "persisted_scope_overridden": int(persisted_scope_stats.get("overridden", 0)),
        "persisted_scope_overridden_chatrooms": int(persisted_scope_stats.get("overridden_chatrooms", 0)),
        "persisted_scope_scoped_chatrooms": int(persisted_scope_stats.get("scoped_chatrooms", 0)),
        "preconditions": preconditions,
        "missing_capabilities": missing_capabilities,
        "source_hint": source_hint,
        "freshness_key": str(sync_freshness.get("key") or "no_sample"),
        "freshness_label": freshness_label,
        "freshness_tone": freshness_tone,
        "freshness_hint": freshness_hint,
        "boundary_note": "当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。",
    }


def _build_chatroom_plugin_scope_brief(*, plugin_scope_snapshot: dict[str, Any] | None) -> dict[str, str]:
    plugin_scope_snapshot = plugin_scope_snapshot or {}
    stage_key = str(plugin_scope_snapshot.get("stage_key") or "unknown")
    stage_label = str(plugin_scope_snapshot.get("stage_label") or "未知")
    stage_tone = str(plugin_scope_snapshot.get("stage_tone") or "outline")
    freshness_key = str(plugin_scope_snapshot.get("freshness_key") or "no_sample")
    evidence = str(plugin_scope_snapshot.get("evidence") or "")
    action = str(plugin_scope_snapshot.get("action") or "")
    missing_capabilities = [str(item).strip() for item in plugin_scope_snapshot.get("missing_capabilities") or [] if str(item).strip()]

    if stage_key == "not_ready":
        priority_label = "高"
        priority_tone = "muted"
        focus_label = "先补样本"
        summary = "当前仍停留在准备度，先补首批群资料样本，再谈群级权限治理。"
        next_step_label = "暂不可建模"
        next_step_summary = "下一步先补首批群消息或群资料样本，再回来看群级权限建模。"
    elif stage_key == "skeleton_only":
        priority_label = "高"
        priority_tone = "muted"
        focus_label = "先脱离骨架"
        summary = "当前只看到群消息骨架，优先补真实群资料同步，不适合直接建模。"
        next_step_label = "暂不可建模"
        next_step_summary = "下一步先补真实群资料同步，让群状态先脱离 message_observed 骨架。"
    elif stage_key == "profile_only":
        priority_label = "中"
        priority_tone = "outline"
        focus_label = "先补成员快照"
        summary = "当前已有群资料，但成员维度证据不足，先补成员快照再推进群级治理。"
        next_step_label = "暂不可建模"
        next_step_summary = "下一步先补成员快照，确认可以按群和成员维度进入权限建模。"
    else:
        focus_label = "可建模"
        if freshness_key in {"stale", "expired"}:
            priority_label = "中"
            priority_tone = "outline"
            summary = "当前已具备建模准备度，但群资料样本不够新，先刷新样本再收口群级权限边界。"
            next_step_label = "可进入群级权限建模"
            next_step_summary = "可进入群级权限建模；先刷新群资料样本，再定义群级启停 / allow/deny / 能力边界口径。"
        else:
            priority_label = "中"
            priority_tone = "success"
            summary = "当前已可进入群级权限建模准备，但仍只到扩展位，不代表真实规则已上线。"
            next_step_label = "可进入群级权限建模"
            next_step_summary = "可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。"

    modeling_judgement = _build_chatroom_plugin_scope_modeling_judgement(
        stage_key=stage_key,
        stage_label=stage_label,
        stage_tone=stage_tone,
        freshness_key=freshness_key,
        freshness_label=str(plugin_scope_snapshot.get("freshness_label") or "尚无时间样本"),
    )
    blocker_snapshot = _build_chatroom_plugin_scope_blocker(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    missing_focus = _build_chatroom_plugin_scope_missing_focus(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    missing_summary = _build_chatroom_plugin_scope_missing_summary(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    return {
        "status_label": stage_label,
        "status_tone": stage_tone,
        "focus_label": focus_label,
        "priority_label": priority_label,
        "priority_tone": priority_tone,
        "summary": summary,
        "operator_summary": f"{stage_label} / {focus_label}；{summary}",
        "evidence": evidence,
        "next_action": action,
        "focus_gap": missing_focus,
        "missing_summary": missing_summary,
        "next_step_label": next_step_label,
        "next_step_summary": next_step_summary,
        "modeling_tier_key": str(modeling_judgement.get("tier_key") or ""),
        "modeling_tier_label": str(modeling_judgement.get("tier_label") or ""),
        "modeling_tier_tone": str(modeling_judgement.get("tier_tone") or "outline"),
        "modeling_tier_summary": str(modeling_judgement.get("summary") or ""),
        "primary_gap_key": str(blocker_snapshot.get("gap_key") or ""),
        "primary_gap_label": str(blocker_snapshot.get("gap_label") or ""),
        "primary_gap_summary": str(blocker_snapshot.get("summary") or ""),
        "blocker_family_key": str(blocker_snapshot.get("family_key") or ""),
        "blocker_family_label": str(blocker_snapshot.get("family_label") or ""),
        "blocker_type_key": str(blocker_snapshot.get("gap_key") or ""),
        "blocker_type_label": str(blocker_snapshot.get("gap_label") or ""),
        "blocker_tone": str(blocker_snapshot.get("tone") or stage_tone),
        "blocker_headline": str(blocker_snapshot.get("headline") or ""),
        "blocker_gate_summary": str(blocker_snapshot.get("gate_summary") or ""),
        "blocker_short_summary": str(blocker_snapshot.get("short_summary") or ""),
        "decision_summary": (
            f"{str(modeling_judgement.get('tier_label') or stage_label)}；"
            f"主 blocker：{str(blocker_snapshot.get('gap_label') or '核心治理模型')}；"
            f"下一步：{next_step_summary}"
        ),
    }


def _summarize_group_plugin_scope_rows(scope_rows: list[GroupPluginScope]) -> dict[str, Any]:
    per_chatroom: dict[int, dict[str, int]] = {}
    total = len(scope_rows)
    inherited = 0
    overridden = 0
    enabled = 0
    disabled = 0

    for row in scope_rows:
        chatroom_bucket = per_chatroom.setdefault(
            int(row.chatroom_id),
            {"total": 0, "inherited": 0, "overridden": 0, "enabled": 0, "disabled": 0},
        )
        chatroom_bucket["total"] += 1
        if bool(row.enabled):
            enabled += 1
            chatroom_bucket["enabled"] += 1
        else:
            disabled += 1
            chatroom_bucket["disabled"] += 1

        if str(row.scope_status or "inherited") == "inherited":
            inherited += 1
            chatroom_bucket["inherited"] += 1
        else:
            overridden += 1
            chatroom_bucket["overridden"] += 1

    overridden_chatrooms = sum(1 for item in per_chatroom.values() if item["overridden"] > 0)
    return {
        "total": total,
        "inherited": inherited,
        "overridden": overridden,
        "enabled": enabled,
        "disabled": disabled,
        "scoped_chatrooms": len(per_chatroom),
        "overridden_chatrooms": overridden_chatrooms,
        "per_chatroom": per_chatroom,
    }


def build_wechat_ipad_chatroom_list(db: Session, channel: Channel) -> dict[str, Any]:
    chatrooms = db.scalars(
        select(Chatroom)
        .where(Chatroom.channel_id == channel.id)
        .order_by(Chatroom.last_synced_at.desc(), Chatroom.id.desc())
    ).all()
    bindings = db.scalars(
        select(BotPlugin)
        .where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound")
        .order_by(BotPlugin.id.asc())
    ).all()
    member_count_rows = db.execute(
        select(ChatroomMember.chatroom_id, func.count(ChatroomMember.id))
        .where(ChatroomMember.channel_id == channel.id)
        .group_by(ChatroomMember.chatroom_id)
    ).all()
    role_rows = db.execute(
        select(ChatroomMember.chatroom_id, ChatroomMember.role, func.count(ChatroomMember.id))
        .where(ChatroomMember.channel_id == channel.id)
        .group_by(ChatroomMember.chatroom_id, ChatroomMember.role)
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(GroupPluginScope.channel_id == channel.id)
        .order_by(GroupPluginScope.chatroom_id.asc(), GroupPluginScope.bot_plugin_id.asc())
    ).all()

    binding_enabled_map = {
        binding.id: str(binding.status or "").strip() == "enabled"
        for binding in bindings
    }
    member_count_map = {int(chatroom_id): int(count or 0) for chatroom_id, count in member_count_rows}
    role_count_map: dict[int, dict[str, int]] = {}
    for chatroom_id, role, count in role_rows:
        chatroom_role_counts = role_count_map.setdefault(int(chatroom_id), {})
        normalized_role = str(role or "").strip() or "member"
        chatroom_role_counts[normalized_role] = int(count or 0)

    scope_map_by_chatroom: dict[int, dict[int, GroupPluginScope]] = {}
    for scope in scope_rows:
        scope_map_by_chatroom.setdefault(int(scope.chatroom_id), {})[int(scope.bot_plugin_id)] = scope

    items: list[dict[str, Any]] = []
    inherited_chatroom_count = 0
    overridden_chatroom_count = 0
    member_snapshot_chatroom_count = 0
    for chatroom in chatrooms:
        member_record_count = int(member_count_map.get(chatroom.id, 0))
        if member_record_count > 0:
            member_snapshot_chatroom_count += 1
        role_counts = role_count_map.get(chatroom.id, {})
        scope_map = scope_map_by_chatroom.get(chatroom.id, {})
        overridden_scope_count = 0
        effective_enabled_count = 0
        for binding in bindings:
            scope = scope_map.get(binding.id)
            if scope and str(scope.scope_status or "").strip() != "inherited":
                overridden_scope_count += 1
            if scope is not None:
                if bool(scope.enabled):
                    effective_enabled_count += 1
            elif binding_group_default_enabled(binding):
                effective_enabled_count += 1
        effective_disabled_count = max(len(bindings) - effective_enabled_count, 0)
        if overridden_scope_count > 0:
            overridden_chatroom_count += 1
        else:
            inherited_chatroom_count += 1
        role_summary = " / ".join(
            f"{_format_chatroom_member_role(role)} {count}"
            for role, count in sorted(role_counts.items(), key=lambda item: item[0])
        ) or "-"
        items.append(
            {
                "id": chatroom.id,
                "name": chatroom.name or chatroom.external_id,
                "external_id": chatroom.external_id,
                "remark": chatroom.remark or "-",
                "owner_external_id": chatroom.owner_external_id or "-",
                "status": chatroom.status or "unknown",
                "status_label": _format_chatroom_status(chatroom.status),
                "management_status": chatroom.management_status or "discovered",
                "management_status_label": format_chatroom_management_status(chatroom.management_status),
                "management_status_tone": management_tone(chatroom.management_status),
                "is_active": bool(chatroom.is_active),
                "sync_depth_label": _build_chatroom_detail_sync_depth(
                    chatroom=chatroom,
                    member_items=[{}] * member_record_count,
                ),
                "member_count": int(chatroom.member_count or 0),
                "member_record_count": member_record_count,
                "member_role_summary": role_summary,
                "scope_total": len(bindings),
                "scope_inherited": max(len(bindings) - overridden_scope_count, 0),
                "scope_overridden": overridden_scope_count,
                "scope_effective_enabled": effective_enabled_count,
                "scope_effective_disabled": effective_disabled_count,
                "last_synced_at": _format_beijing_time(chatroom.last_synced_at),
                "observed_source": _extract_chatroom_source(chatroom),
            }
        )

    return {
        "items": items,
        "total": len(items),
        "binding_total": len(bindings),
        "binding_enabled_total": sum(1 for enabled in binding_enabled_map.values() if enabled),
        "member_snapshot_chatroom_count": member_snapshot_chatroom_count,
        "pending_chatroom_count": sum(1 for item in items if item["management_status"] == "discovered"),
        "active_chatroom_count": sum(1 for item in items if item["is_active"]),
        "disabled_chatroom_count": sum(1 for item in items if item["management_status"] == "disabled"),
        "inherited_chatroom_count": inherited_chatroom_count,
        "overridden_chatroom_count": overridden_chatroom_count,
    }


def build_wechat_ipad_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    members = db.scalars(
        select(ChatroomMember)
        .where(ChatroomMember.chatroom_id == chatroom.id)
        .order_by(ChatroomMember.role.asc(), ChatroomMember.display_name.asc(), ChatroomMember.nickname.asc(), ChatroomMember.id.asc())
    ).all()
    bindings = db.scalars(
        select(BotPlugin)
        .where(BotPlugin.bot_id == channel.bot_id, BotPlugin.status != "unbound")
        .order_by(BotPlugin.id.asc())
    ).all()
    scope_rows = db.scalars(
        select(GroupPluginScope)
        .where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
        )
        .order_by(GroupPluginScope.id.asc())
    ).all()
    if not scope_rows and bindings:
        scope_rows = materialize_group_plugin_scopes_for_chatroom(db, channel=channel, chatroom=chatroom, source="chatroom_detail_view")

    role_counts: dict[str, int] = {}
    member_items: list[dict[str, Any]] = []
    for member in members:
        role_key = str(member.role or "").strip() or "member"
        role_counts[role_key] = role_counts.get(role_key, 0) + 1
        member_extra = _parse_json(member.extra_json)
        province = str(member_extra.get("province") or "").strip()
        city = str(member_extra.get("city") or "").strip()
        member_flag = str(member_extra.get("member_flag") or "").strip()
        alias = str(member_extra.get("alias") or "").strip() or "-"
        remark = str(member_extra.get("remark") or "").strip() or "-"
        inviter_user_name = str(member_extra.get("inviter_user_name") or "").strip() or "-"
        avatar_url = str(member_extra.get("small_head_img_url") or "").strip()
        region_parts = [item for item in (province, city) if item]
        member_items.append(
            {
                "id": member.id,
                "member_external_id": member.member_external_id,
                "nickname": member.nickname or "-",
                "display_name": member.display_name or "-",
                "role": role_key,
                "role_label": _format_chatroom_member_role(role_key),
                "status": member.status or "active",
                "status_label": _format_chatroom_member_status(member.status),
                "member_flag": member_flag or "-",
                "member_flag_label": _format_chatroom_member_flag(member_flag),
                "alias": alias,
                "remark": remark,
                "inviter_user_name": inviter_user_name,
                "province": province or "-",
                "city": city or "-",
                "region_label": " / ".join(region_parts) if region_parts else "-",
                "avatar_url": avatar_url,
                "has_avatar": bool(avatar_url),
                "last_active_at": _format_beijing_time(member.last_active_at),
                "last_synced_at": _format_beijing_time(member.last_synced_at),
            }
        )

    binding_items: list[dict[str, Any]] = []
    enabled_binding_count = 0
    inherited_scope_count = 0
    overridden_scope_count = 0
    effective_enabled_count = 0
    scope_map = {item.bot_plugin_id: item for item in scope_rows}
    for binding in bindings:
        plugin = binding.plugin
        binding_enabled = str(binding.status or "").strip() == "enabled"
        if binding_enabled:
            enabled_binding_count += 1
        scope = scope_map.get(binding.id)
        scope_status = str(getattr(scope, "scope_status", "") or "inherited")
        scope_enabled = group_plugin_scope_enabled(binding, scope)
        if scope_status == "inherited":
            inherited_scope_count += 1
        else:
            overridden_scope_count += 1
        if scope_enabled:
            effective_enabled_count += 1
        scope_action = str(getattr(scope, "action_scope", "") or "all")
        scope_effect = str(getattr(scope, "policy_effect", "") or "")
        binding_config = parse_config_json(binding.config_json)
        scope_config = parse_config_json(getattr(scope, "config_override_json", None))
        effective_config = _build_effective_group_config(
            plugin_code=plugin.code if plugin else None,
            binding_config=binding_config,
            scope_config=scope_config,
        )
        binding_items.append(
            {
                "binding_id": binding.id,
                "plugin_code": plugin.code if plugin else "-",
                "plugin_name": plugin.display_name if plugin else f"Plugin #{binding.plugin_id}",
                "binding_status": binding.status or "unknown",
                "binding_status_label": "已启用" if binding_enabled else "已停用",
                "binding_status_tone": "success" if binding_enabled else "muted",
                "runtime_status": binding.runtime_status or "idle",
                "last_execution_status": binding.last_execution_status or "-",
                "last_execution_summary": binding.last_execution_summary or "-",
                "last_execution_at": _format_beijing_time(binding.last_execution_at),
                "group_scope_status_label": _format_group_scope_status(scope_status),
                "group_scope_status_tone": "outline" if scope_status == "inherited" else "success",
                "group_scope_enabled_label": "启用" if scope_enabled else "停用",
                "group_scope_action_scope": scope_action,
                "group_scope_effect": scope_effect or "-",
                "group_scope_status": scope_status,
                "group_scope_enabled": scope_enabled,
                "group_scope_mode": "inherited" if scope_status == "inherited" else ("enabled" if scope_enabled else "disabled"),
                "group_scope_summary": (
                    "当前没有群级覆盖配置；该群直接继承 bot_plugin 的启停与配置。"
                    if scope_status == "inherited"
                    else "当前已存在群级作用域覆盖记录；该群会优先使用单群启停和配置覆盖。"
                ),
                "group_scope_boundary_note": "未填写群级配置时会继续继承 bot_plugin 默认配置；填写后仅覆盖当前群。",
                "group_scope_can_enable": not scope_enabled,
                "group_scope_can_disable": scope_enabled,
                "group_scope_can_reset": scope_status != "inherited",
                "group_scope_has_config_override": bool(getattr(scope, "config_override_json", None)),
                "group_scope_config_override_json": str(getattr(scope, "config_override_json", "") or "").strip(),
                "group_scope_effective_config_json": _format_config_for_editor(effective_config),
                "group_scope_effective_config_has_values": bool(effective_config),
                "binding_config_json": dump_config_json(binding_config) or "",
            }
        )

    source = _extract_chatroom_source(chatroom)
    return {
        "chatroom_id": chatroom.id,
        "chatroom_external_id": chatroom.external_id,
        "name": chatroom.name or chatroom.external_id,
        "remark": chatroom.remark or "-",
        "owner_external_id": chatroom.owner_external_id or "-",
        "status": chatroom.status or "unknown",
        "status_label": _format_chatroom_status(chatroom.status),
        "management_status": chatroom.management_status or "discovered",
        "management_status_label": format_chatroom_management_status(chatroom.management_status),
        "management_status_tone": management_tone(chatroom.management_status),
        "is_active": bool(chatroom.is_active),
        "member_count": int(chatroom.member_count or 0),
        "member_record_count": len(member_items),
        "last_synced_at": _format_beijing_time(chatroom.last_synced_at),
        "observed_source": source,
        "sync_depth_label": _build_chatroom_detail_sync_depth(chatroom=chatroom, member_items=member_items),
        "role_counts": role_counts,
        "members": member_items,
        "bindings": binding_items,
        "binding_summary": {
            "total": len(binding_items),
            "enabled": enabled_binding_count,
            "disabled": max(len(binding_items) - enabled_binding_count, 0),
            "materialized_scope_count": len(scope_rows),
            "inherited_scope_count": inherited_scope_count,
            "overridden_scope_count": overridden_scope_count,
            "effective_enabled_count": effective_enabled_count,
            "effective_disabled_count": max(len(binding_items) - effective_enabled_count, 0),
            "operator_summary": (
                f"当前 bot 共绑定 {len(binding_items)} 个插件，bot 级启用 {enabled_binding_count} 个；"
                f"已落库 {len(scope_rows)} 条群级作用域记录，继承 {inherited_scope_count} / 覆盖 {overridden_scope_count}；"
                f"当前有效启用 {effective_enabled_count} / 停用 {max(len(binding_items) - effective_enabled_count, 0)}，仍属于本地草稿控制。"
            ),
        },
    }


def update_wechat_ipad_chatroom_scope_enabled(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    enabled: bool,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_enabled(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        enabled=enabled,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_ipad_chatroom_scope_toggle",
    )


def reset_wechat_ipad_chatroom_scope(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return reset_group_plugin_scope_to_inherited(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_ipad_chatroom_scope_reset",
    )


def update_wechat_ipad_chatroom_scope_config_override(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    bot_plugin_id: int,
    config_override_json: str | None,
    operator_id: int | None = None,
    audit_note: str | None = None,
) -> GroupPluginScope:
    return update_group_plugin_scope_config_override(
        db,
        channel=channel,
        chatroom=chatroom,
        bot_plugin_id=bot_plugin_id,
        config_override_json=config_override_json,
        operator_id=operator_id,
        audit_note=audit_note,
        source="wechat_ipad_chatroom_scope_config_override",
    )


def _build_group_plugin_governance_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
    plugin_scope_overview: dict[str, Any] | None,
) -> dict[str, str]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    overview = plugin_scope_overview or {}
    status_label = str(brief.get("status_label") or snapshot.get("stage_label") or "未纳入判断")
    status_tone = str(brief.get("status_tone") or snapshot.get("stage_tone") or "muted")
    focus_label = str(brief.get("focus_label") or "扩展位")
    priority_label = str(overview.get("priority_label") or brief.get("priority_label") or "观察")
    priority_tone = str(overview.get("priority_tone") or brief.get("priority_tone") or status_tone)
    summary = str(overview.get("summary") or brief.get("summary") or snapshot.get("summary") or "当前仅补到群级插件权限治理扩展位。")
    evidence = str(overview.get("evidence") or snapshot.get("evidence") or brief.get("evidence") or "群级插件权限准备度仍待判断。")
    action = str(brief.get("next_action") or snapshot.get("action") or "先补群资料样本。")
    missing_summary = str(brief.get("missing_summary") or "")
    boundary_note = str(snapshot.get("boundary_note") or "")
    next_step_label = str(brief.get("next_step_label") or "继续观察")
    next_step_summary = str(overview.get("next_step_summary") or brief.get("next_step_summary") or action)
    ops_digest = str(
        overview.get("ops_digest")
        or f"当前阶段={status_label}；关键证据={evidence}；下一步={next_step_summary}"
    )
    modeling_tier_label = str(overview.get("modeling_tier_label") or brief.get("modeling_tier_label") or status_label)
    modeling_tier_tone = str(overview.get("modeling_tier_tone") or brief.get("modeling_tier_tone") or status_tone)
    primary_gap_label = str(overview.get("primary_gap_label") or brief.get("primary_gap_label") or focus_label)
    primary_gap_summary = str(brief.get("primary_gap_summary") or missing_summary)
    decision_summary = str(brief.get("decision_summary") or f"{status_label}；主 blocker：{focus_label}；下一步：{next_step_summary}")
    blocker_family_label = str(overview.get("blocker_family_label") or brief.get("blocker_family_label") or "模型前置")
    blocker_type_label = str(overview.get("blocker_type_label") or brief.get("blocker_type_label") or primary_gap_label)
    blocker_tone = str(overview.get("blocker_tone") or brief.get("blocker_tone") or status_tone)
    blocker_headline = str(overview.get("blocker_headline") or brief.get("blocker_headline") or f"当前最卡：{blocker_type_label}")
    blocker_gate_summary = str(overview.get("blocker_gate_summary") or brief.get("blocker_gate_summary") or primary_gap_summary)
    blocker_short_summary = str(overview.get("blocker_short_summary") or brief.get("blocker_short_summary") or blocker_gate_summary)
    blocker_snapshot_summary = f"{blocker_family_label} / {blocker_type_label}：{blocker_short_summary}"
    single_overview_summary = (
        f"当前档位：{status_label}；"
        f"建模判断：{modeling_tier_label}；"
        f"优先级：{priority_label}；"
        f"主 blocker：{primary_gap_label}；"
        f"下一步：{next_step_summary}"
    )
    sample_gate_snapshot = _build_group_plugin_sample_gate_snapshot(
        plugin_scope_snapshot=snapshot,
        plugin_scope_brief=brief,
        plugin_scope_overview=overview,
    )
    return {
        "status_label": status_label,
        "status_tone": status_tone,
        "focus_label": focus_label,
        "priority_label": priority_label,
        "priority_tone": priority_tone,
        "headline": str(overview.get("headline") or f"群级治理准备度：{status_label}"),
        "summary": summary,
        "evidence": evidence,
        "action": action,
        "missing_summary": missing_summary,
        "next_step_label": next_step_label,
        "next_step_summary": next_step_summary,
        "boundary_note": boundary_note,
        "operator_summary": str(overview.get("operator_summary") or f"{status_label} / {focus_label} / {priority_label}；{summary}"),
        "ops_digest": ops_digest,
        "modeling_tier_label": modeling_tier_label,
        "modeling_tier_tone": modeling_tier_tone,
        "modeling_tier_summary": str(brief.get("modeling_tier_summary") or summary),
        "primary_gap_label": primary_gap_label,
        "primary_gap_summary": primary_gap_summary,
        "blocker_family_label": blocker_family_label,
        "blocker_type_label": blocker_type_label,
        "blocker_tone": blocker_tone,
        "blocker_headline": blocker_headline,
        "blocker_gate_summary": blocker_gate_summary,
        "blocker_short_summary": blocker_short_summary,
        "blocker_snapshot_summary": blocker_snapshot_summary,
        "decision_summary": decision_summary,
        "overview_summary": single_overview_summary,
        "single_overview_summary": single_overview_summary,
        "focus_gap": str(overview.get("focus_gap") or brief.get("focus_gap") or ""),
        "sample_gate_snapshot": sample_gate_snapshot,
    }


def _build_group_plugin_sample_gate_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
    plugin_scope_overview: dict[str, Any] | None,
) -> dict[str, str]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    overview = plugin_scope_overview or {}
    stage_key = str(snapshot.get("stage_key") or "unknown")
    stage_label = str(snapshot.get("stage_label") or "未纳入判断")
    freshness_key = str(snapshot.get("freshness_key") or "no_sample")
    freshness_label = str(snapshot.get("freshness_label") or "尚无时间样本")
    readiness_summary = str(snapshot.get("readiness_summary") or "前置条件仍待补齐。")
    source_hint = str(snapshot.get("source_hint") or "最近来源仍待补齐。")
    boundary_note = str(snapshot.get("boundary_note") or "")
    next_step_summary = str(overview.get("next_step_summary") or brief.get("next_step_summary") or snapshot.get("action") or "继续补样本。")

    if stage_key in {"not_ready", "skeleton_only", "profile_only"}:
        gate_key = "samples_missing"
        gate_label = "样本门槛未满足"
        gate_tone = "muted" if stage_key in {"not_ready", "skeleton_only"} else "outline"
        headline = "群级权限样本门槛：未达下一步"
        summary = f"当前仍处于“{stage_label}”，本地 chatroom/member 样本还不足以进入下一步群级权限建模。"
    elif freshness_key in {"stale", "expired"}:
        gate_key = "samples_stale"
        gate_label = "样本基本满足但偏旧"
        gate_tone = "outline"
        headline = "群级权限样本门槛：可进入下一步前先刷新"
        summary = f"当前样本数量已够，但群资料时效为“{freshness_label}”，建议先刷新样本再进入群级权限建模。"
    else:
        gate_key = "samples_ready"
        gate_label = "样本门槛已满足"
        gate_tone = "success"
        headline = "群级权限样本门槛：可进入下一步"
        summary = "当前本地 chatroom/member 样本已具备最小门槛，可以进入群级权限建模准备，但仍只是扩展位治理视图。"

    evidence = f"{readiness_summary.rstrip('。')}；样本时效={freshness_label}。"
    operator_summary = f"{gate_label}；关键证据={evidence}；下一步={next_step_summary}"
    return {
        "gate_key": gate_key,
        "gate_label": gate_label,
        "gate_tone": gate_tone,
        "headline": headline,
        "summary": summary,
        "evidence": evidence,
        "action": next_step_summary,
        "source_hint": source_hint,
        "boundary_note": boundary_note,
        "operator_summary": operator_summary,
    }


def _build_group_enablement_model_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    stage_label = str(snapshot.get("stage_label") or brief.get("status_label") or "未纳入判断")
    stage_tone = str(snapshot.get("stage_tone") or brief.get("status_tone") or "muted")
    readiness_summary = str(snapshot.get("readiness_summary") or "前置条件仍待补齐。")
    primary_gap_label = str(brief.get("primary_gap_label") or "群级启停模型")
    next_step_summary = str(
        brief.get("next_step_summary")
        or snapshot.get("action")
        or "继续用当前摘要口径观察群资料样本，不进入真实群级启停控制。"
    )
    primary_key_dimensions = [
        {
            "key": "channel_id",
            "label": "渠道",
            "summary": "限定在单个 wechat_ipad 渠道内，避免跨渠道误复用。",
        },
        {
            "key": "chatroom_id_or_external_id",
            "label": "群",
            "summary": "优先使用本地 chatroom.id；若仍处于样本阶段，可回退 chatroom.external_id。",
        },
        {
            "key": "bot_plugin_id",
            "label": "bot 插件绑定",
            "summary": "以 bot_plugins 作为最小控制对象，表达 bot 内具体插件实例的启停。",
        },
        {
            "key": "plugin_projection",
            "label": "插件镜像",
            "summary": "plugins/plugin.code 仅用于展示与对账，不作为真实群级控制主键。",
        },
    ]
    relationship_scope_summary = (
        "channel -> chatroom -> bot_plugin 为最小控制链路；plugin/plugin_code 仅作镜像展示与草案对账。"
    )
    boundary_note = "当前仅定义群级启停模型口径摘要与扩展位，不代表真实群级启停控制、写入接口或权限系统已经上线。"
    operator_summary = (
        "最小模型已定义为草案口径；"
        "建议主键=channel_id + chatroom_id(或 external_id) + bot_plugin_id；"
        f"当前准备度={stage_label}；下一步={next_step_summary}"
    )
    return {
        "model_key": "group_enablement_model_snapshot",
        "model_label": "群级启停模型口径",
        "status_key": "draft_defined",
        "status_label": "已定义草案级口径",
        "status_tone": "outline" if stage_tone == "muted" else stage_tone,
        "is_defined": True,
        "definition_level": "draft",
        "definition_level_label": "草案级",
        "primary_gap_label": primary_gap_label,
        "recommended_primary_key": "channel_id + chatroom_id(或 chatroom_external_id) + bot_plugin_id",
        "primary_key_dimensions": primary_key_dimensions,
        "relationship_scope_summary": relationship_scope_summary,
        "current_stage_label": stage_label,
        "current_stage_summary": readiness_summary,
        "summary": "当前已把群级启停模型收口到只读草案：群定位看 channel/chatroom，插件定位看 bot_plugin，plugin 镜像只做展示。",
        "next_step": next_step_summary,
        "operator_summary": operator_summary,
        "boundary_note": boundary_note,
    }


def _build_group_allow_deny_model_snapshot(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    stage_label = str(snapshot.get("stage_label") or brief.get("status_label") or "未纳入判断")
    stage_tone = str(snapshot.get("stage_tone") or brief.get("status_tone") or "muted")
    readiness_summary = str(snapshot.get("readiness_summary") or "前置条件仍待补齐。")
    next_step_summary = str(
        brief.get("next_step_summary")
        or snapshot.get("action")
        or "继续只读展示 allow/deny 草案摘要，不进入真实群级 allow/deny 判定执行。"
    )
    recommended_dimensions = [
        {
            "key": "channel_id",
            "label": "渠道",
            "summary": "限定在单个 wechat_ipad 渠道内，避免跨渠道复用未确认的口径。",
        },
        {
            "key": "chatroom_id_or_external_id",
            "label": "群",
            "summary": "优先使用本地 chatroom.id；样本阶段允许回退 chatroom.external_id 做草案对账。",
        },
        {
            "key": "bot_plugin_id",
            "label": "bot 插件绑定",
            "summary": "仍以 bot_plugins 作为最小控制对象，不把 plugin 镜像误当执行主键。",
        },
        {
            "key": "action_scope",
            "label": "动作范围",
            "summary": "只描述群级启停、消息消费或命令触发这类最小动作范围，不扩成完整能力矩阵。",
        },
        {
            "key": "policy_effect",
            "label": "策略效果",
            "summary": "草案仅区分 allow / deny 两类效果，用于摘要表达最小权限边界。",
        },
    ]
    relationship_scope_summary = (
        "channel -> chatroom -> bot_plugin 为主体链路，再补 action_scope + policy_effect 表达群级 allow/deny 草案边界。"
    )
    boundary_note = (
        "当前仅定义群级 allow/deny 口径摘要与扩展位，不代表真实群级 allow/deny 控制、写入接口、判定执行或权限系统已经上线。"
    )
    operator_summary = (
        "最小 allow/deny 口径已定义为草案摘要；"
        "建议维度=channel_id + chatroom_id(或 external_id) + bot_plugin_id + action_scope + policy_effect；"
        f"当前准备度={stage_label}；下一步={next_step_summary}"
    )
    return {
        "model_key": "group_allow_deny_model_snapshot",
        "model_label": "群级 allow/deny 口径",
        "status_key": "draft_defined",
        "status_label": "已定义草案级口径",
        "status_tone": "outline" if stage_tone == "muted" else stage_tone,
        "is_defined": True,
        "definition_level": "draft",
        "definition_level_label": "草案级",
        "is_live": False,
        "current_stage_label": stage_label,
        "current_stage_summary": readiness_summary,
        "summary": "当前已把群级 allow/deny 收口到只读草案摘要：按 channel/chatroom/bot_plugin 定位，再用 action_scope 与 policy_effect 表达最小权限边界。",
        "recommended_dimensions": recommended_dimensions,
        "recommended_dimension_keys": [item["key"] for item in recommended_dimensions],
        "relationship_scope_summary": relationship_scope_summary,
        "next_step": next_step_summary,
        "operator_summary": operator_summary,
        "boundary_note": boundary_note,
    }


def _build_chatroom_plugin_scope_overview(
    *,
    plugin_scope_snapshot: dict[str, Any] | None,
    plugin_scope_brief: dict[str, Any] | None,
) -> dict[str, str]:
    snapshot = plugin_scope_snapshot or {}
    brief = plugin_scope_brief or {}
    status_label = str(brief.get("status_label") or snapshot.get("stage_label") or "未纳入判断")
    status_tone = str(brief.get("status_tone") or snapshot.get("stage_tone") or "muted")
    modeling_tier_label = str(brief.get("modeling_tier_label") or status_label)
    modeling_tier_tone = str(brief.get("modeling_tier_tone") or status_tone)
    priority_label = str(brief.get("priority_label") or "观察")
    priority_tone = str(brief.get("priority_tone") or status_tone)
    primary_gap_label = str(brief.get("primary_gap_label") or "核心治理模型")
    next_step_summary = str(brief.get("next_step_summary") or snapshot.get("action") or "继续补样本。")
    readiness_summary = str(snapshot.get("readiness_summary") or "")
    focus_gap = str(brief.get("focus_gap") or "")
    decision_summary = str(brief.get("decision_summary") or f"{modeling_tier_label}；主 blocker：{primary_gap_label}；下一步：{next_step_summary}")
    blocker_family_label = str(brief.get("blocker_family_label") or "模型前置")
    blocker_type_label = str(brief.get("blocker_type_label") or primary_gap_label)
    blocker_tone = str(brief.get("blocker_tone") or status_tone)
    blocker_headline = str(brief.get("blocker_headline") or f"当前最卡：{blocker_type_label}")
    blocker_gate_summary = str(brief.get("blocker_gate_summary") or brief.get("primary_gap_summary") or "")
    blocker_short_summary = str(brief.get("blocker_short_summary") or blocker_gate_summary)
    blocker_snapshot_summary = f"{blocker_family_label} / {blocker_type_label}：{blocker_short_summary}"

    if readiness_summary:
        evidence = readiness_summary.rstrip("。")
    else:
        evidence = str(snapshot.get("evidence") or brief.get("evidence") or "当前群级治理准备度仍待判断。").rstrip("。")

    short_summary = f"{decision_summary}；优先级：{priority_label}；{evidence}。"
    if focus_gap:
        short_summary = f"{short_summary} {focus_gap}"
    ops_digest = f"当前阶段={status_label}；关键证据={evidence}；下一步={next_step_summary}"
    single_overview_summary = (
        f"当前档位：{status_label}；"
        f"建模判断：{modeling_tier_label}；"
        f"优先级：{priority_label}；"
        f"主 blocker：{primary_gap_label}；"
        f"下一步：{next_step_summary}"
    )

    return {
        "headline": f"群级权限治理摘要：{modeling_tier_label}",
        "status_label": status_label,
        "status_tone": status_tone,
        "modeling_tier_label": modeling_tier_label,
        "modeling_tier_tone": modeling_tier_tone,
        "priority_label": priority_label,
        "priority_tone": priority_tone,
        "primary_gap_label": primary_gap_label,
        "blocker_family_label": blocker_family_label,
        "blocker_type_label": blocker_type_label,
        "blocker_tone": blocker_tone,
        "summary": short_summary,
        "operator_summary": (
            f"建模判断={modeling_tier_label}；"
            f"准备度={status_label}；"
            f"优先级={priority_label}；"
            f"主 blocker={primary_gap_label}；"
            f"下一步={next_step_summary}"
        ),
        "next_step_summary": next_step_summary,
        "evidence": evidence,
        "focus_gap": focus_gap,
        "blocker_headline": blocker_headline,
        "blocker_gate_summary": blocker_gate_summary,
        "blocker_short_summary": blocker_short_summary,
        "blocker_snapshot_summary": blocker_snapshot_summary,
        "ops_digest": ops_digest,
        "single_overview_summary": single_overview_summary,
    }


def _build_chatroom_plugin_scope_missing_summary(*, stage_key: str, missing_capabilities: list[str]) -> str:
    labels = _build_chatroom_plugin_scope_gap_labels(missing_capabilities)
    if labels:
        gap_label = "、".join(labels)
        if len(labels) >= 4:
            gap_label = f"{gap_label}四类核心模型"
        else:
            gap_label = f"{gap_label}等核心模型"
    else:
        gap_label = "核心模型"

    if stage_key == "not_ready":
        return f"主缺{gap_label}；当前先补群资料样本。"
    if stage_key == "skeleton_only":
        return f"主缺{gap_label}；当前先脱离消息骨架。"
    if stage_key == "profile_only":
        return f"主缺{gap_label}；当前先补成员快照。"
    return f"主缺{gap_label}；当前可进入最小规则建模。"


def _build_chatroom_plugin_scope_missing_focus(*, stage_key: str, missing_capabilities: list[str]) -> str:
    labels = _build_chatroom_plugin_scope_gap_labels(missing_capabilities)
    gap_label = "、".join(labels) if labels else "核心模型缺口待补"
    if stage_key == "not_ready":
        return f"仍缺 {gap_label}；当前先补群资料样本。"
    if stage_key == "skeleton_only":
        return f"仍缺 {gap_label}；当前先脱离 message_observed 骨架。"
    if stage_key == "profile_only":
        return f"仍缺 {gap_label}；当前先补成员快照。"
    return f"仍缺 {gap_label}；当前可进入最小规则建模。"


def _build_chatroom_plugin_scope_gap_labels(missing_capabilities: list[str]) -> list[str]:
    labels: list[str] = []
    for item in missing_capabilities:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        if cleaned.startswith("缺群级 "):
            cleaned = cleaned.removeprefix("缺群级 ")
        elif cleaned.startswith("缺"):
            cleaned = cleaned.removeprefix("缺")
        cleaned = cleaned.strip()
        if cleaned.startswith("群级"):
            cleaned = cleaned.removeprefix("群级").strip()
        labels.append(cleaned)
    return labels


def _build_chatroom_plugin_scope_modeling_judgement(
    *,
    stage_key: str,
    stage_label: str,
    stage_tone: str,
    freshness_key: str,
    freshness_label: str,
) -> dict[str, str]:
    if stage_key in {"not_ready", "skeleton_only", "profile_only"}:
        return {
            "tier_key": "not_modelable",
            "tier_label": "仍不可建模",
            "tier_tone": "muted" if stage_tone == "muted" else "outline",
            "summary": f"当前仍处于“{stage_label}”，群级权限治理还不适合进入建模。",
        }
    if freshness_key in {"stale", "expired"}:
        return {
            "tier_key": "modelable_stale",
            "tier_label": "可进入建模但样本偏旧",
            "tier_tone": "outline",
            "summary": f"当前已具备建模前提，但群资料样本时效为“{freshness_label}”，应先刷新样本再收口规则。",
        }
    return {
        "tier_key": "modelable_ready",
        "tier_label": "已具备建模前提",
        "tier_tone": "success",
        "summary": "当前群资料、成员记录已形成最小建模前提，可直接进入群级治理规则收口。",
    }


def _build_chatroom_plugin_scope_blocker(*, stage_key: str, missing_capabilities: list[str]) -> dict[str, str]:
    prioritized_items = [
        (
            "enable/disable",
            "model_prerequisite",
            "模型前置",
            "群级启停模型",
            "优先补群级启停映射，否则无法落最小开关治理。",
            "先补群级启停模型，补完后才适合继续收口 allow/deny 与命令/能力边界。",
        ),
        (
            "allow/deny",
            "model_prerequisite",
            "模型前置",
            "allow/deny 边界",
            "优先补 allow/deny 口径，否则无法稳定表达准入边界。",
            "先补 allow/deny 边界，补完后才适合继续固化命令/能力边界。",
        ),
        (
            "命令/能力边界",
            "model_prerequisite",
            "模型前置",
            "命令/能力边界",
            "优先补命令/能力边界，否则无法定义群内插件可用范围。",
            "先补命令/能力边界，补完后才适合稳定表达群内插件可用范围。",
        ),
        (
            "审计与运营动作",
            "model_prerequisite",
            "模型前置",
            "审计留痕",
            "优先补审计与运营动作留痕，否则后续治理动作不可追溯。",
            "先补审计留痕，补完后才适合把群级治理动作作为正式运维能力推进。",
        ),
    ]
    if stage_key in {"not_ready", "skeleton_only"}:
        return {
            "family_key": "sample_prerequisite",
            "family_label": "样本前置",
            "gap_key": "chatroom_sample",
            "gap_label": "群资料样本",
            "tone": "muted",
            "headline": "当前最卡：群资料样本",
            "summary": "当前最影响下一步的是群资料样本不足，未到真实群级治理模型收口阶段。",
            "gate_summary": "先补群资料样本，补完后才有资格进入群成员样本或群级规则建模。",
            "short_summary": "群资料样本不足；先补群资料样本，补完后才有资格进入下一步。",
        }
    if stage_key == "profile_only":
        return {
            "family_key": "sample_prerequisite",
            "family_label": "样本前置",
            "gap_key": "member_sample",
            "gap_label": "群成员样本",
            "tone": "outline",
            "headline": "当前最卡：群成员样本",
            "summary": "当前最影响下一步的是群成员样本缺失，先补成员快照再谈规则建模。",
            "gate_summary": "先补群成员样本，补完后才有资格进入群级启停、allow/deny 与能力边界建模。",
            "short_summary": "群成员样本缺失；先补成员快照，补完后才有资格进入下一步。",
        }

    joined = " / ".join(missing_capabilities)
    for keyword, family_key, family_label, label, summary, gate_summary in prioritized_items:
        if keyword in joined:
            return {
                "family_key": family_key,
                "family_label": family_label,
                "gap_key": keyword,
                "gap_label": label,
                "tone": "outline",
                "headline": f"当前最卡：{label}",
                "summary": summary,
                "gate_summary": gate_summary,
                "short_summary": f"{label}仍缺；{gate_summary}",
            }
    return {
        "family_key": "model_prerequisite",
        "family_label": "模型前置",
        "gap_key": "missing_model",
        "gap_label": "核心治理模型",
        "tone": "outline",
        "headline": "当前最卡：核心治理模型",
        "summary": "当前仍缺最小群级治理模型，需要继续按优先级收口。",
        "gate_summary": "先补核心治理模型，补完后才适合继续推进群级权限治理落地。",
        "short_summary": "核心治理模型仍缺；先补模型，补完后才有资格进入下一步。",
    }


def _build_chatroom_plugin_scope_primary_gap(*, stage_key: str, missing_capabilities: list[str]) -> dict[str, str]:
    blocker_snapshot = _build_chatroom_plugin_scope_blocker(
        stage_key=stage_key,
        missing_capabilities=missing_capabilities,
    )
    return {
        "gap_key": str(blocker_snapshot.get("gap_key") or ""),
        "gap_label": str(blocker_snapshot.get("gap_label") or ""),
        "summary": str(blocker_snapshot.get("summary") or ""),
    }


def _build_chatroom_sync_freshness(synced_at: Any, *, now: datetime | None = None) -> dict[str, str]:
    freshness = _build_recent_call_freshness(synced_at, now=now)
    key = str(freshness.get("key") or "no_sample")
    if key == "fresh":
        return {
            "key": "fresh",
            "label": "新鲜",
            "tone": "success",
            "hint": "最近群资料同步仍较新，可直接按当前样本继续核对覆盖度。",
        }
    if key == "stale":
        return {
            "key": "stale",
            "label": "偏旧",
            "tone": "outline",
            "hint": "最近群资料同步已偏旧，建议先刷新群资料样本再判断覆盖是否停滞。",
        }
    if key == "expired":
        return {
            "key": "expired",
            "label": "过旧",
            "tone": "outline",
            "hint": "最近群资料同步已过旧，当前覆盖度更可能只是历史样本。",
        }
    return {
        "key": "no_sample",
        "label": "尚无时间样本",
        "tone": "muted",
        "hint": "最近群资料缺少可用同步时间，当前无法判断样本是否仍新鲜。",
    }


def _build_ratio_item(*, label: str, numerator: int, denominator: int, tone: str) -> dict[str, str]:
    percent_value = 0 if denominator <= 0 else round((numerator / denominator) * 100)
    return {
        "label": label,
        "value_label": f"{numerator} / {denominator}",
        "percent_label": f"{percent_value}%",
        "tone": tone,
    }


def _build_chatroom_sync_depth(
    *,
    chatroom_count: int,
    active_chatroom_count: int,
    member_synced_chatroom_count: int,
) -> dict[str, str]:
    if chatroom_count <= 0:
        return {
            "status_key": "no_sample",
            "status_label": "尚未形成样本",
            "operator_summary": "尚未形成群资料同步样本，当前还没有本地 chatroom 或成员落库记录。",
            "tone": "muted",
        }
    if active_chatroom_count <= 0:
        return {
            "status_key": "message_skeleton_only",
            "status_label": "仅消息骨架",
            "operator_summary": f"已观测 {chatroom_count} 个群，但仍全部停留在 message_observed 骨架阶段。",
            "tone": "muted",
        }
    if member_synced_chatroom_count <= 0:
        return {
            "status_key": "profile_without_members",
            "status_label": "已有群资料，无成员快照",
            "operator_summary": f"已观测 {chatroom_count} 个群，其中 {active_chatroom_count} 个群已补到基础资料，但还没有成员快照。",
            "tone": "outline",
        }
    return {
        "status_key": "member_records_available",
        "status_label": "已有成员记录",
        "operator_summary": (
            f"已观测 {chatroom_count} 个群，{active_chatroom_count} 个群已补到资料，"
            f"{member_synced_chatroom_count} 个群已有成员记录。"
        ),
        "tone": "success",
    }


def _build_chatroom_detail_sync_depth(*, chatroom: Chatroom, member_items: list[dict[str, Any]]) -> str:
    status = str(chatroom.status or "").strip()
    if status == "message_observed":
        return "仅消息骨架"
    if status == "profile_synced":
        return "已有群资料，无成员快照"
    if status == "members_synced":
        return "已有成员记录"
    if member_items:
        return "已有成员记录"
    if status in {"active", "pending_sync"}:
        return "已有群资料，无成员快照"
    return "待判断"


def _format_chatroom_member_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"owner", "group_owner"}:
        return "群主"
    if normalized in {"admin", "administrator"}:
        return "管理员"
    if normalized in {"member", "active", ""}:
        return "成员"
    return normalized or "成员"


def _format_chatroom_member_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"active", ""}:
        return "正常"
    if normalized == "message_observed":
        return "仅消息观测"
    if normalized in {"left", "removed"}:
        return "已退群"
    if normalized == "inactive":
        return "非活跃"
    return normalized or "未知"


def _format_chatroom_member_flag(flag: str | None) -> str:
    normalized = str(flag or "").strip()
    if not normalized:
        return "普通成员"
    labels = {
        "0": "普通成员",
        "1": "普通成员",
        "9": "特殊标记",
        "17": "邀请链路标记",
        "25": "群主/核心成员标记",
        "2049": "管理员",
        "2057": "管理员",
        "8193": "活跃成员标记",
        "8201": "活跃管理员标记",
    }
    return labels.get(normalized, f"Flag {normalized}")


def _format_group_scope_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"", "inherited"}:
        return "继承 bot_plugin"
    if normalized == "overridden":
        return "已存在群级覆盖"
    return normalized or "继承 bot_plugin"


def _format_chatroom_status(status: str | None) -> str:
    normalized = str(status or "").strip()
    if not normalized:
        return "未知"
    if normalized == "message_observed":
        return "消息观测骨架"
    if normalized == "profile_synced":
        return "已补群资料"
    if normalized == "members_synced":
        return "已补成员快照"
    if normalized == "active":
        return "已补充资料"
    if normalized == "pending_sync":
        return "待同步"
    return normalized


def _resolve_login_profile_key(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in WECHAT_IPAD_LOGIN_PROFILE_KEYS:
        return normalized
    return "ipad"


def _default_device_name_for_profile(profile_key: str) -> str:
    return "fulcrum iPad"


def _resolve_login_profiles(adapter_config: dict[str, Any]) -> dict[str, dict[str, str]]:
    legacy_profiles = adapter_config.get("login_profiles")
    legacy_profile_map = legacy_profiles if isinstance(legacy_profiles, dict) else {}
    legacy_ipad_profile = legacy_profile_map.get("ipad") if isinstance(legacy_profile_map.get("ipad"), dict) else {}
    legacy_car_profile = legacy_profile_map.get("car") if isinstance(legacy_profile_map.get("car"), dict) else {}
    legacy_device_name = str(
        adapter_config.get("device_name")
        or legacy_ipad_profile.get("device_name")
        or legacy_car_profile.get("device_name")
        or ""
    ).strip()
    legacy_device_id = str(
        adapter_config.get("device_id")
        or legacy_ipad_profile.get("device_id")
        or legacy_car_profile.get("device_id")
        or ""
    ).strip()
    shared_device_name = legacy_device_name or _default_device_name_for_profile("ipad")
    shared_device_id = legacy_device_id
    normalized_profiles: dict[str, dict[str, str]] = {}
    for key in WECHAT_IPAD_LOGIN_PROFILE_KEYS:
        normalized_profiles[key] = {
            "device_name": shared_device_name,
            "device_id": shared_device_id,
        }
    return normalized_profiles


def _apply_login_profile_selection(
    channel: Channel,
    adapter_config: dict[str, Any],
    runtime_info: dict[str, Any],
    *,
    requested_profile: Any = None,
) -> tuple[str, str, str]:
    login_profiles = _resolve_login_profiles(adapter_config)
    profile_key = _resolve_login_profile_key(
        requested_profile or runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile")
    )
    shared = login_profiles.get("ipad") or login_profiles.get(profile_key) or {}
    device_name = str(shared.get("device_name") or _default_device_name_for_profile("ipad")).strip()
    device_id = str(shared.get("device_id") or "").strip()
    if not device_id:
        device_id = WechatIpadApiClient.create_device_id(seed=f"channel-{channel.id}-shared-{device_name}")
    adapter_config.pop("login_profiles", None)
    adapter_config["selected_login_profile"] = profile_key
    adapter_config["device_name"] = device_name
    adapter_config["device_id"] = device_id
    runtime_info["selected_login_profile"] = profile_key
    return profile_key, device_name, device_id


def _login_init_endpoint_for_profile(profile_key: str) -> str:
    """按已确认的登录终端选择固定 874 取码接口，不读取任何旧端点覆盖。"""

    if profile_key == "car":
        return IPAD_874_CONTRACT.endpoints.login_get_qr_car
    return IPAD_874_CONTRACT.endpoints.login_get_qr


def start_wechat_ipad_login(
    db: Session,
    channel: Channel,
    *,
    login_profile: str | None = None,
    proxy: dict[str, str] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    """使用调用方已经解析好的单次登录代理获取二维码，不读取渠道长期代理配置。"""

    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    login_profile_key, device_name, device_id = _apply_login_profile_selection(
        channel,
        adapter_config,
        runtime_info,
        requested_profile=login_profile,
    )
    runtime_info = _compact_wechat_ipad_runtime_info(runtime_info)
    login_init_endpoint = _login_init_endpoint_for_profile(login_profile_key)
    channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    logger.info(
        "wechat ipad login init started",
        extra={
            "channel_id": channel.id,
            "channel_name": channel.name,
            "api_base": channel.api_base or "",
            "login_profile": login_profile_key,
            "device_name": device_name,
            "device_id": device_id,
            "login_init_endpoint": login_init_endpoint,
            "server_url": str(adapter_config.get("server_url") or ""),
        },
    )
    if not channel.api_base:
        return _persist_runtime(
            db,
            channel,
            login_state="error",
            qrcode_status="unavailable",
            summary="缺少 API Base，无法初始化登录。",
            error="missing_api_base",
        )

    response = _safe_call(
        api_client.get_qr_code,
        stage="login_init",
        trace_endpoint=login_init_endpoint,
        channel=channel,
        device_name=device_name,
        device_id=device_id,
        proxy=proxy,
        login_profile=login_profile_key,
    )
    if not response["ok"]:
        logger.warning(
            "wechat ipad login init failed",
            extra={
                "channel_id": channel.id,
                "login_profile": login_profile_key,
                "device_name": device_name,
                "device_id": device_id,
                "login_init_endpoint": login_init_endpoint,
                "provider_message": response.get("message") or "",
                "provider_code": response.get("code"),
                "error": response["error"] or "",
            },
        )
        return _persist_runtime(
            db,
            channel,
            login_state="login_init_failed",
            qrcode_status="failed",
            summary="GetQR 调用失败，未拿到二维码。",
            error=response["error"],
            error_context=response.get("error_context"),
            extra={
                "reconnect_status": "idle",
                "last_login_init_response": _summarize_provider_payload(response.get("payload") or {}),
                "last_login_init_endpoint": login_init_endpoint,
                "selected_login_profile": login_profile_key,
                "last_login_profile": login_profile_key,
            },
        )

    payload = response["payload"]
    qr_fields = WechatIpadApiClient.extract_qr_fields(payload)
    provider_summary = _summarize_provider_payload(payload)
    if not qr_fields["qr_url"]:
        return _persist_runtime(
            db,
            channel,
            login_state="login_init_failed",
            qrcode_status="failed",
            summary="协议服务未返回可展示的二维码链接。",
            error="qrcode_url_missing",
            extra={
                "last_login_init_response": provider_summary,
                "last_login_init_endpoint": login_init_endpoint,
                "selected_login_profile": login_profile_key,
                "last_login_profile": login_profile_key,
            },
        )
    previous_refresh_count = int(_parse_json(getattr(channel, "runtime_info_json", None)).get("qrcode_refresh_count") or 0)
    runtime_info = {
        "login_qr_uuid": qr_fields["uuid"],
        "qrcode_url": qr_fields["qr_url"],
        "qrcode_expires_at": _normalize_qrcode_expires_at(qr_fields["expires_at"]),
        "qrcode_refresh_count": previous_refresh_count + 1,
        "login_qr_message": response["message"] or "waiting_scan",
        "last_qrcode_at": _utcnow_iso(),
        "last_login_init_response": provider_summary,
        "last_login_init_endpoint": login_init_endpoint,
        "last_login_init_request": {"DeviceName": device_name, "DeviceID": device_id},
        "selected_login_profile": login_profile_key,
        "last_login_profile": login_profile_key,
    }
    logger.info(
        "wechat ipad login init completed",
        extra={
            "channel_id": channel.id,
            "login_profile": login_profile_key,
            "device_name": device_name,
            "device_id": device_id,
            "login_init_endpoint": login_init_endpoint,
            "qr_uuid": qr_fields["uuid"] or "",
            "has_qr_url": bool(qr_fields["qr_url"]),
            "expires_at": str(runtime_info.get("qrcode_expires_at") or ""),
            "provider_message": response.get("message") or "",
        },
    )
    return _persist_runtime(
        db,
        channel,
        login_state="qrcode_ready",
        qrcode_status="waiting_scan",
        summary="已获取登录二维码。",
        extra=runtime_info,
        error=None,
    )


def refresh_wechat_ipad_qrcode(
    db: Session,
    channel: Channel,
    *,
    login_profile: str | None = None,
    proxy: dict[str, str] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    """刷新二维码时继续使用当前登录会话已经锁定的代理。"""

    overview = start_wechat_ipad_login(
        db,
        channel,
        login_profile=login_profile,
        proxy=proxy,
        client=client,
    )
    return _persist_runtime(
        db,
        channel,
        login_state=overview.get("login_state") or "qrcode_ready",
        qrcode_status=overview.get("qrcode_status") or "waiting_scan",
        summary="已刷新二维码。",
        error=None,
    )


def logout_wechat_ipad(
    db: Session,
    channel: Channel,
    *,
    login_profile: str | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    selected_profile, _, _ = _apply_login_profile_selection(
        channel,
        adapter_config,
        runtime_info,
        requested_profile=login_profile,
    )
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    logout_result = None
    if wxid:
        logout_result = _safe_call(
            api_client.log_out,
            stage="logout",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_logout,
            channel=channel,
            wxid=wxid,
        )
    extra = {
        "selected_login_profile": selected_profile,
        "last_logout_at": _utcnow_iso(),
        "last_logout_response": (logout_result or {}).get("payload") if isinstance(logout_result, dict) else {},
        "last_logout_profile": selected_profile,
        "active_wxid": "",
        "active_nickname": "",
        "active_alias": "",
        "active_bind_mobile": "",
        "login_qr_uuid": "",
        "qrcode_url": "",
        "qrcode_expires_at": "",
        "heartbeat_status": "unknown",
        "heartbeat_long_status": "unknown",
        "reconnect_status": "idle",
        "sync_key": "",
    }
    summary = f"已退出当前登录，可切换到 {WECHAT_IPAD_LOGIN_PROFILE_LABELS.get(selected_profile, selected_profile)} 重新获取二维码。"
    error = None
    error_context = None
    if logout_result and not logout_result.get("ok"):
        summary = f"退出登录接口异常：{logout_result.get('error') or logout_result.get('message') or 'provider_error'}；本地状态已切回待登录。"
        error = logout_result.get("error")
        error_context = logout_result.get("error_context")
    return _persist_runtime(
        db,
        channel,
        login_state="idle",
        qrcode_status="idle",
        summary=summary,
        error=error,
        error_context=error_context,
        extra=extra,
    )


def refresh_wechat_ipad_status(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    probe_at = _utcnow_iso()
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    device_id = str(adapter_config.get("device_id") or "").strip()
    qr_uuid = str(runtime_info.get("login_qr_uuid") or "").strip()
    logger.info(
        "wechat ipad status refresh started",
        extra={
            "channel_id": channel.id,
            "channel_name": channel.name,
            "login_state": channel.login_state,
            "qrcode_status": channel.qrcode_status,
            "wxid": wxid,
            "device_id": device_id,
            "qr_uuid": qr_uuid,
            "probe_at": probe_at,
        },
    )

    qr_result: dict[str, Any] | None = None
    if _build_qrcode_expiry_view(runtime_info)["is_expired"] and getattr(channel, "qrcode_status", "") not in {"success", "expired"}:
        return _persist_runtime(
            db,
            channel,
            login_state="qrcode_expired",
            qrcode_status="expired",
            summary="二维码已过期，请刷新二维码后重新扫码。",
            error=None,
            extra=runtime_info,
        )
    if qr_uuid:
        qr_result = _safe_call(
            api_client.check_qr,
            stage="check_qr",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_check_qr,
            channel=channel,
            uuid=qr_uuid,
        )
        runtime_info["last_check_qr_at"] = probe_at
        runtime_info["last_check_qr_response"] = qr_result.get("payload")
        runtime_info["login_qr_message"] = qr_result.get("message")
        qr_fields = WechatIpadApiClient.extract_qr_fields(qr_result.get("payload") or {})
        if qr_fields["qr_url"]:
            runtime_info["qrcode_url"] = qr_fields["qr_url"]
        if qr_fields["expires_at"] not in (None, ""):
            runtime_info["qrcode_expires_at"] = _normalize_qrcode_expires_at(qr_fields["expires_at"])
        if _qr_login_succeeded(qr_result):
            account_fields = WechatIpadApiClient.extract_account_fields(qr_result["payload"])
            runtime_info["active_wxid"] = account_fields["wxid"] or wxid
            runtime_info["active_nickname"] = account_fields["nickname"]
            runtime_info["active_alias"] = account_fields["alias"]
            runtime_info["active_bind_mobile"] = account_fields["bind_mobile"]
            if runtime_info.get("active_wxid"):
                adapter_config["wxid"] = runtime_info.get("active_wxid")
                channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
            wxid = _resolve_wxid(channel, runtime_info, adapter_config)
        logger.info(
            "wechat ipad check qr evaluated",
            extra={
                "channel_id": channel.id,
                "provider_message": qr_result.get("message") or "",
                "provider_code": qr_result.get("code"),
                "qr_status_code": _extract_qr_status_code(qr_result),
                "derived_login_state": _derive_login_state_from_qr(qr_result) or "",
                "derived_qrcode_status": _derive_qrcode_status(qr_result) or "",
                "qr_login_succeeded": _qr_login_succeeded(qr_result),
                "wxid_after_qr": wxid,
            },
        )

    if not channel.api_base or not wxid:
        summary = "缺少 API Base 或 wxid，无法继续同步登录态。"
        if qr_result and qr_result["ok"]:
            summary = _build_qr_status_summary(qr_result)
        return _persist_runtime(
            db,
            channel,
            login_state=_derive_login_state_from_qr(qr_result) if qr_result else "offline",
            qrcode_status=_derive_qrcode_status(qr_result) if qr_result else "idle",
            summary=summary,
            error="missing_api_base_or_wxid" if not wxid else None,
            error_context=qr_result.get("error_context") if qr_result and not qr_result["ok"] else None,
            extra=runtime_info,
        )

    heartbeat = _safe_call(
        api_client.heartbeat,
        stage="heartbeat",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat,
        channel=channel,
        wxid=wxid,
    )
    heartbeat_long = _safe_call(
        api_client.heartbeat,
        stage="heartbeat_long",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat_long,
        channel=channel,
        wxid=wxid,
        long=True,
    )
    user_profile = _safe_call(
        api_client.get_profile,
        stage="user_profile",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.user_get_profile,
        channel=channel,
        wxid=wxid,
        target_wxid=wxid,
    )

    login_ok = heartbeat["ok"]
    profile_data = user_profile["data"] if isinstance(user_profile.get("data"), dict) else {}
    profile_fields = WechatIpadApiClient.extract_account_fields(user_profile.get("payload") or {})
    selected_profile = _resolve_login_profile_key(
        runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile")
    )
    adapter_config.pop("login_profiles", None)
    adapter_config["selected_login_profile"] = selected_profile
    runtime_info.update(
        {
            "active_wxid": wxid,
            "active_nickname": runtime_info.get("active_nickname") or profile_fields.get("nickname") or profile_data.get("NickName") or profile_data.get("nickname"),
            "active_alias": runtime_info.get("active_alias") or profile_fields.get("alias") or profile_data.get("Alias") or profile_data.get("alias"),
            "active_bind_mobile": runtime_info.get("active_bind_mobile") or profile_fields.get("bind_mobile") or profile_data.get("BindMobile") or profile_data.get("bind_mobile"),
            "heartbeat_status": "ok" if login_ok else "failed",
            "heartbeat_long_status": "ok" if heartbeat_long["ok"] else "failed",
            "reconnect_status": "ready" if login_ok else "required",
            "last_success_login_profile": (
                runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile") or "ipad"
            ) if login_ok else runtime_info.get("last_success_login_profile"),
            "last_heartbeat_long_at": probe_at,
            "last_user_profile_at": probe_at,
            "last_heartbeat_response": heartbeat["payload"],
            "last_heartbeat_long_response": heartbeat_long["payload"],
            "last_user_profile_response": user_profile["payload"],
            "user_profile_summary": _extract_user_profile_summary(profile_data, profile_fields),
        }
    )
    if wxid:
        adapter_config["wxid"] = wxid
        channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
    summary = _build_status_summary(qr_result, heartbeat, user_profile)
    logger.info(
        "wechat ipad status refresh completed",
        extra={
            "channel_id": channel.id,
            "login_ok": login_ok,
            "wxid": wxid,
            "device_id": str(adapter_config.get("device_id") or ""),
            "heartbeat_ok": heartbeat["ok"],
            "heartbeat_long_ok": heartbeat_long["ok"],
            "user_profile_ok": user_profile["ok"],
            "summary": summary,
        },
    )
    state = _persist_runtime(
        db,
        channel,
        login_state="online" if login_ok else _derive_login_state_from_qr(qr_result) or "offline",
        qrcode_status="success" if login_ok else _derive_qrcode_status(qr_result) or getattr(channel, "qrcode_status", "idle"),
        summary=summary,
        heartbeat_at=now_shanghai(),
        login_at=now_shanghai() if login_ok else None,
        extra=runtime_info,
        error=None if login_ok else heartbeat["error"],
        error_context=None if login_ok else heartbeat.get("error_context"),
    )
    channel.status = "online" if login_ok else "degraded"
    db.commit()
    return state


def heartbeat_wechat_ipad_channel(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    now = now_shanghai()
    last_heartbeat_at = _parse_datetime_like(getattr(channel, "last_heartbeat_at", None))
    last_heartbeat_long_at = _parse_datetime_like(runtime_info.get("last_heartbeat_long_at"))
    heartbeat_due = force or last_heartbeat_at is None or (now - last_heartbeat_at).total_seconds() >= WECHAT_IPAD_HEARTBEAT_INTERVAL_SECONDS
    heartbeat_long_due = force or last_heartbeat_long_at is None or (now - last_heartbeat_long_at).total_seconds() >= WECHAT_IPAD_HEARTBEAT_LONG_INTERVAL_SECONDS
    if not wxid:
        logger.info(
            "wechat ipad scheduled heartbeat skipped",
            extra={"channel_id": channel.id, "reason": "missing_wxid", "login_state": channel.login_state},
        )
        return {
            "ok": False,
            "skipped": True,
            "reason": "missing_wxid",
        }
    if not heartbeat_due and not heartbeat_long_due:
        logger.info(
            "wechat ipad scheduled heartbeat skipped",
            extra={
                "channel_id": channel.id,
                "reason": "not_due",
                "wxid": wxid,
                "last_heartbeat_at": str(last_heartbeat_at or ""),
                "last_heartbeat_long_at": str(last_heartbeat_long_at or ""),
            },
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": "not_due",
        }

    logger.info(
        "wechat ipad scheduled heartbeat started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "heartbeat_due": heartbeat_due,
            "heartbeat_long_due": heartbeat_long_due,
            "last_heartbeat_at": str(last_heartbeat_at or ""),
            "last_heartbeat_long_at": str(last_heartbeat_long_at or ""),
        },
    )

    heartbeat_result = None
    heartbeat_long_result = None
    if heartbeat_due:
        heartbeat_result = _safe_call(
            api_client.heartbeat,
            stage="heartbeat",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat,
            channel=channel,
            wxid=wxid,
        )
        runtime_info["last_heartbeat_response"] = heartbeat_result["payload"]
        runtime_info["heartbeat_status"] = "ok" if heartbeat_result["ok"] else "failed"

    if heartbeat_long_due:
        heartbeat_long_result = _safe_call(
            api_client.heartbeat,
            stage="heartbeat_long",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.login_heartbeat_long,
            channel=channel,
            wxid=wxid,
            long=True,
        )
        runtime_info["last_heartbeat_long_response"] = heartbeat_long_result["payload"]
        runtime_info["heartbeat_long_status"] = "ok" if heartbeat_long_result["ok"] else "failed"
        runtime_info["last_heartbeat_long_at"] = _utcnow_iso()

    heartbeat_ok = True if heartbeat_result is None else bool(heartbeat_result["ok"])
    heartbeat_long_ok = True if heartbeat_long_result is None else bool(heartbeat_long_result["ok"])
    twice_auto_auth_result = None
    if wxid and (not heartbeat_ok or not heartbeat_long_ok):
        twice_auto_auth_result = _maybe_run_twice_auto_auth_after_heartbeat_failure(
            api_client,
            channel=channel,
            adapter_config=adapter_config,
            runtime_info=runtime_info,
            wxid=wxid,
            heartbeat_result=heartbeat_result,
            heartbeat_long_result=heartbeat_long_result,
            now=now,
        )
    runtime_info["reconnect_status"] = "ready" if heartbeat_ok else "required"
    error = None
    error_context = None
    login_state = getattr(channel, "login_state", "online") or "online"
    channel_status = "online"
    summary = None
    if not heartbeat_ok:
        error = heartbeat_result["error"] if heartbeat_result else "heartbeat_failed"
        error_context = heartbeat_result.get("error_context") if heartbeat_result else None
        login_state = "reconnecting" if twice_auto_auth_result and twice_auto_auth_result.get("ok") else "offline"
        channel_status = "degraded"
        summary = (
            "HeartBeat 失败，已自动触发 TwiceAutoAuth。"
            if twice_auto_auth_result and twice_auto_auth_result.get("ok")
            else f"HeartBeat 失败：{error}"
        )
    elif not heartbeat_long_ok:
        error = heartbeat_long_result["error"] if heartbeat_long_result else "heartbeat_long_failed"
        error_context = heartbeat_long_result.get("error_context") if heartbeat_long_result else None
        login_state = "online"
        channel_status = "degraded"
        summary = (
            "HeartBeatLong 失败，已自动触发 TwiceAutoAuth。"
            if twice_auto_auth_result and twice_auto_auth_result.get("ok")
            else f"HeartBeatLong 失败：{error}"
        )
    elif twice_auto_auth_result and twice_auto_auth_result.get("ok"):
        runtime_info["reconnect_status"] = "auto_auth_ok"
        summary = "自动 TwiceAutoAuth 已执行成功。"

    state = _persist_runtime(
        db,
        channel,
        login_state=login_state,
        qrcode_status=getattr(channel, "qrcode_status", "success") or "success",
        summary=summary,
        error=error,
        error_context=error_context,
        heartbeat_at=now if heartbeat_due else None,
        extra=runtime_info,
    )
    channel.status = channel_status
    db.commit()
    logger.info(
        "wechat ipad scheduled heartbeat completed",
        extra={
            "channel_id": channel.id,
            "heartbeat_due": heartbeat_due,
            "heartbeat_long_due": heartbeat_long_due,
            "heartbeat_ok": heartbeat_ok,
            "heartbeat_long_ok": heartbeat_long_ok,
            "twice_auto_auth_ok": bool(twice_auto_auth_result and twice_auto_auth_result.get("ok")),
            "channel_status": channel_status,
            "login_state": login_state,
        },
    )
    return {
        "ok": heartbeat_ok and heartbeat_long_ok,
        "skipped": False,
        "heartbeat_due": heartbeat_due,
        "heartbeat_long_due": heartbeat_long_due,
        "state": state,
    }


def _maybe_run_twice_auto_auth_after_heartbeat_failure(
    api_client: WechatIpadApiClient,
    *,
    channel: Channel,
    adapter_config: dict[str, Any],
    runtime_info: dict[str, Any],
    wxid: str,
    heartbeat_result: dict[str, Any] | None,
    heartbeat_long_result: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any] | None:
    last_attempt_at = _parse_datetime_like(runtime_info.get("last_twice_auto_auth_at"))
    if last_attempt_at and (now - last_attempt_at).total_seconds() < WECHAT_IPAD_TWICE_AUTO_AUTH_COOLDOWN_SECONDS:
        logger.info(
            "wechat ipad twice auto auth skipped by cooldown",
            extra={
                "channel_id": channel.id,
                "wxid": wxid,
                "last_twice_auto_auth_at": str(last_attempt_at),
            },
        )
        return None

    trigger_error = ""
    if heartbeat_result and not heartbeat_result.get("ok"):
        trigger_error = str(heartbeat_result.get("error") or heartbeat_result.get("message") or "").strip()
    elif heartbeat_long_result and not heartbeat_long_result.get("ok"):
        trigger_error = str(heartbeat_long_result.get("error") or heartbeat_long_result.get("message") or "").strip()

    logger.info(
        "wechat ipad twice auto auth started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "trigger_error": trigger_error,
        },
    )
    result = _safe_call(
        api_client.twice_auto_auth,
        stage="twice_auto_auth",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
        channel=channel,
        wxid=wxid,
    )
    runtime_info["last_twice_auto_auth_at"] = _utcnow_iso()
    runtime_info["last_twice_auto_auth_response"] = result.get("payload")
    runtime_info["last_twice_auto_auth_error"] = result.get("error") if not result.get("ok") else ""
    runtime_info["reconnect_status"] = "auto_auth_ok" if result.get("ok") else "auto_auth_failed"
    logger.info(
        "wechat ipad twice auto auth completed",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "ok": bool(result.get("ok")),
            "provider_message": result.get("message") or "",
            "provider_code": result.get("code"),
        },
    )
    return result


def poll_wechat_ipad_login_status(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    probe_at = _utcnow_iso()
    qr_uuid = str(runtime_info.get("login_qr_uuid") or "").strip()
    logger.info(
        "wechat ipad login poll started",
        extra={
            "channel_id": channel.id,
            "channel_name": channel.name,
            "login_state": channel.login_state,
            "qrcode_status": channel.qrcode_status,
            "qr_uuid": qr_uuid,
            "probe_at": probe_at,
        },
    )

    if _build_qrcode_expiry_view(runtime_info)["is_expired"] and getattr(channel, "qrcode_status", "") not in {"success", "expired"}:
        return _persist_runtime(
            db,
            channel,
            login_state="qrcode_expired",
            qrcode_status="expired",
            summary="二维码已过期，请刷新二维码后重新扫码。",
            error=None,
            extra=runtime_info,
        )

    if not qr_uuid:
        return _persist_runtime(
            db,
            channel,
            login_state=getattr(channel, "login_state", "idle") or "idle",
            qrcode_status=getattr(channel, "qrcode_status", "idle") or "idle",
            summary="当前没有可轮询的二维码，请先获取二维码。",
            error=None,
            extra=runtime_info,
        )

    qr_result = _safe_call(
        api_client.check_qr,
        stage="check_qr",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_check_qr,
        channel=channel,
        uuid=qr_uuid,
    )
    runtime_info["last_check_qr_at"] = probe_at
    runtime_info["last_check_qr_response"] = qr_result.get("payload")
    runtime_info["login_qr_message"] = qr_result.get("message")
    qr_fields = WechatIpadApiClient.extract_qr_fields(qr_result.get("payload") or {})
    if qr_fields["qr_url"]:
        runtime_info["qrcode_url"] = qr_fields["qr_url"]
    if qr_fields["expires_at"] not in (None, ""):
        runtime_info["qrcode_expires_at"] = _normalize_qrcode_expires_at(qr_fields["expires_at"])

    login_state = _derive_login_state_from_qr(qr_result) or getattr(channel, "login_state", "offline") or "offline"
    qrcode_status = _derive_qrcode_status(qr_result) or getattr(channel, "qrcode_status", "idle") or "idle"
    logger.info(
        "wechat ipad login poll evaluated",
        extra={
            "channel_id": channel.id,
            "provider_message": qr_result.get("message") or "",
            "provider_code": qr_result.get("code"),
            "qr_status_code": _extract_qr_status_code(qr_result),
            "derived_login_state": login_state,
            "derived_qrcode_status": qrcode_status,
            "qr_login_succeeded": _qr_login_succeeded(qr_result),
        },
    )
    if _qr_login_succeeded(qr_result):
        account_fields = WechatIpadApiClient.extract_account_fields(qr_result.get("payload") or {})
        if account_fields["wxid"]:
            runtime_info["active_wxid"] = account_fields["wxid"]
            adapter_config["wxid"] = account_fields["wxid"]
        if account_fields["nickname"]:
            runtime_info["active_nickname"] = account_fields["nickname"]
        if account_fields["alias"]:
            runtime_info["active_alias"] = account_fields["alias"]
        if account_fields["bind_mobile"]:
            runtime_info["active_bind_mobile"] = account_fields["bind_mobile"]
        channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False, sort_keys=True)
        runtime_info["last_success_login_profile"] = (
            runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile") or "ipad"
        )
        channel.status = "online"
        logger.info(
            "wechat ipad login poll marked success",
            extra={
                "channel_id": channel.id,
                "provider_message": qr_result.get("message") or "",
                "provider_code": qr_result.get("code"),
                "qr_status_code": _extract_qr_status_code(qr_result),
                "active_wxid": runtime_info.get("active_wxid") or "",
                "active_nickname": runtime_info.get("active_nickname") or "",
            },
        )
        return _persist_runtime(
            db,
            channel,
            login_state="online",
            qrcode_status="success",
            summary=_build_qr_status_summary(qr_result),
            error=None,
            login_at=now_shanghai(),
            extra=runtime_info,
        )

    return _persist_runtime(
        db,
        channel,
        login_state=login_state,
        qrcode_status=qrcode_status,
        summary=_build_qr_status_summary(qr_result),
        error=None if qr_result["ok"] else qr_result["error"],
        error_context=None if qr_result["ok"] else qr_result.get("error_context"),
        extra=runtime_info,
    )


def reconnect_wechat_ipad(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    if not wxid:
        return _persist_runtime(
            db,
            channel,
            login_state="reconnect_blocked",
            summary="缺少 wxid，无法调用 Awaken/TwiceAutoAuth。",
            error="missing_wxid",
            extra={"reconnect_status": "blocked"},
        )

    awaken = _safe_call(
        api_client.awaken_login,
        stage="awaken",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_awaken,
        channel=channel,
        wxid=wxid,
    )
    extra = {
        "last_reconnect_response": awaken["payload"],
        "reconnect_method": "Awaken",
    }
    if not awaken["ok"]:
        return _persist_runtime(
            db,
            channel,
            login_state="reconnect_failed",
            summary="Awaken 调用失败。",
            error=awaken["error"],
            error_context=awaken.get("error_context"),
            extra={**extra, "reconnect_status": "failed"},
        )

    awakened_uuid = (
        awaken["data"].get("QrCodeResponse", {}).get("Uuid")
        if isinstance(awaken["data"].get("QrCodeResponse"), dict)
        else None
    )
    if awakened_uuid:
        extra["login_qr_uuid"] = awakened_uuid
        extra["qrcode_status_after_reconnect"] = "waiting_scan"
        extra["reconnect_status"] = "awakened"
        extra["qrcode_url"] = f"http://weixin.qq.com/x/{awakened_uuid}"
    twice_auth = _safe_call(
        api_client.twice_auto_auth,
        stage="twice_auto_auth",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
        channel=channel,
        wxid=wxid,
    )
    extra["last_twice_auto_auth_response"] = twice_auth["payload"]
    extra["reconnect_status"] = "auto_auth_ok" if twice_auth["ok"] else extra.get("reconnect_status", "awakened")
    summary = "已调用 Awaken/TwiceAutoAuth。"
    if awakened_uuid:
        summary = "已调用 Awaken 获取新的二维码，并尝试 TwiceAutoAuth。"
    return _persist_runtime(
        db,
        channel,
        login_state="reconnecting",
        qrcode_status="waiting_scan" if awakened_uuid else getattr(channel, "qrcode_status", "idle"),
        summary=summary,
        error=None if twice_auth["ok"] or awakened_uuid else twice_auth["error"],
        error_context=None if twice_auth["ok"] or awakened_uuid else twice_auth.get("error_context"),
        extra=extra,
    )


def pull_wechat_ipad_messages(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    if not wxid:
        return _persist_runtime(
            db,
            channel,
            summary="缺少 wxid，无法调用 Msg/Sync。",
            error="missing_wxid",
            sync_at=now_shanghai(),
            extra={"reconnect_status": "required"},
        )

    response = _safe_call(
        api_client.sync_message,
        stage="message_sync",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.message_sync,
        channel=channel,
        wxid=wxid,
        scene=int(adapter_config.get("message_sync_scene") or 0),
        synckey=str(runtime_info.get("sync_key") or ""),
    )
    if not response["ok"]:
        return _persist_runtime(
            db,
            channel,
            summary="Msg/Sync 调用失败。",
            error=response["error"],
            error_context=response.get("error_context"),
            sync_at=now_shanghai(),
            extra={
                "last_message_sync_response": response.get("payload"),
                "reconnect_status": "required",
            },
        )

    payload = response["payload"]
    sync_fields = WechatIpadApiClient.extract_sync_fields(payload)
    data = sync_fields["data"]
    messages = sync_fields["list"]
    ingested = 0
    deduplicated = 0
    ingest_results: list[dict[str, Any]] = []
    for item in messages:
        result = ingest_inbound_event(db, channel, item, source="wechat_ipad_sync")
        ingest_results.append(result)
        deduplicated += 1 if result["deduplicated"] else 0
        ingested += 1 if result["mapped"] and not result["deduplicated"] else 0

    chatroom_sync_candidates = _collect_chatroom_sync_candidates_from_results(
        db,
        channel,
        ingest_results,
    )
    chatroom_sync_summary = ""
    if chatroom_sync_candidates["chatroom_ids"]:
        sync_wechat_ipad_chatrooms(
            db,
            channel,
            chatroom_ids=chatroom_sync_candidates["chatroom_ids"],
            client=api_client,
        )
        db.refresh(channel)
        runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
        chatroom_sync_summary = str(runtime_info.get("last_chatroom_sync_summary") or "")

    state = _persist_runtime(
        db,
        channel,
        summary=f"已按 Msg/Sync 拉取 {len(messages)} 条消息，新增 {ingested} 条。",
        sync_at=now_shanghai(),
        extra={
            "last_message_sync_response": payload,
            "last_message_sync_count": len(messages),
            "last_message_sync_ingested": ingested,
            "last_message_sync_deduplicated": deduplicated,
            "last_pull_protocol_summary": _build_pull_summary(messages, sync_fields),
            "sync_key": sync_fields["sync_key"] or runtime_info.get("sync_key") or "",
            "sync_continue_flag": sync_fields["continue_flag"],
            "last_message_sync_chatroom_candidates": chatroom_sync_candidates["signals"],
            "last_message_sync_chatroom_candidate_count": len(chatroom_sync_candidates["chatroom_ids"]),
            "last_message_sync_chatroom_sync_triggered": bool(chatroom_sync_candidates["chatroom_ids"]),
            "last_message_sync_chatroom_sync_summary": chatroom_sync_summary or None,
        },
        error=None,
    )
    db.commit()
    return state


def sync_wechat_ipad_chatrooms(
    db: Session,
    channel: Channel,
    *,
    chatroom_ids: list[str] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    logger.info(
        "wechat ipad chatroom sync started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "requested_chatroom_ids": chatroom_ids or [],
        },
    )
    if not wxid:
        logger.info(
            "wechat ipad chatroom sync blocked",
            extra={"channel_id": channel.id, "reason": "missing_wxid"},
        )
        return _persist_runtime(
            db,
            channel,
            summary="缺少 wxid，无法调用群资料同步接口。",
            error="missing_wxid",
            extra={"last_chatroom_sync_summary": "群资料同步被阻塞：缺少 wxid。"},
        )

    if chatroom_ids:
        targets = [str(item or "").strip() for item in chatroom_ids if str(item or "").strip()]
    else:
        targets = [
            item.external_id
            for item in db.scalars(
                select(Chatroom)
                .where(Chatroom.channel_id == channel.id)
                .order_by(Chatroom.updated_at.desc(), Chatroom.id.desc())
                .limit(20)
            ).all()
            if str(item.external_id or "").strip()
        ]

    deduped_targets: list[str] = []
    seen: set[str] = set()
    for item in targets:
        if item in seen:
            continue
        seen.add(item)
        deduped_targets.append(item)

    if not deduped_targets:
        logger.info(
            "wechat ipad chatroom sync skipped",
            extra={"channel_id": channel.id, "reason": "no_targets"},
        )
        return _persist_runtime(
            db,
            channel,
            summary="当前没有可同步的本地群样本；请先等待群消息落库或手动补样本。",
            error=None,
            extra={"last_chatroom_sync_summary": "群资料同步未执行：没有可同步的群样本。"},
        )

    # 群资料同步固定使用 874 群接口，租户配置不能再覆盖到旧协议路由。
    info_endpoint = IPAD_874_CONTRACT.endpoints.group_get_info
    member_endpoint = IPAD_874_CONTRACT.endpoints.group_get_member_detail
    synced_profiles = 0
    synced_members = 0
    failed_targets: list[dict[str, str]] = []
    sync_started_at = now_shanghai()

    for chatroom_id in deduped_targets:
        logger.info(
            "wechat ipad chatroom sync target started",
            extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "wxid": wxid},
        )
        info_result = _safe_call(
            api_client.get_chatroom_info,
            stage="chatroom_sync",
            trace_endpoint=info_endpoint,
            channel=channel,
            wxid=wxid,
            chatroom_id=chatroom_id,
        )
        if not info_result["ok"]:
            logger.warning(
                "wechat ipad chatroom profile sync failed",
                extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "error": str(info_result["error"] or "")},
            )
            failed_targets.append({"chatroom_id": chatroom_id, "stage": "profile", "error": str(info_result["error"] or "")})
            continue

        member_result = _safe_call(
            api_client.get_chatroom_member_detail,
            stage="chatroom_member_sync",
            trace_endpoint=member_endpoint,
            channel=channel,
            wxid=wxid,
            chatroom_id=chatroom_id,
        )
        upsert_wechat_ipad_chatroom_profile(
            db,
            channel,
            chatroom_external_id=chatroom_id,
            profile_payload=info_result["payload"],
            member_payload=member_result["payload"] if member_result["ok"] else None,
            synced_at=sync_started_at,
            source="wechat_ipad_chatroom_sync",
        )
        synced_profiles += 1
        synced_members += 1 if member_result["ok"] else 0
        if not member_result["ok"]:
            logger.warning(
                "wechat ipad chatroom member sync failed",
                extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "error": str(member_result["error"] or "")},
            )
            failed_targets.append({"chatroom_id": chatroom_id, "stage": "members", "error": str(member_result["error"] or "")})
        else:
            logger.info(
                "wechat ipad chatroom sync target completed",
                extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "members_synced": True},
            )

    summary = (
        f"已同步 {len(deduped_targets)} 个群目标，成功补齐群资料 {synced_profiles} 个，"
        f"成功补齐成员快照 {synced_members} 个。"
    )
    if failed_targets:
        summary += f" 其中 {len(failed_targets)} 个目标仍需继续对齐。"

    state = _persist_runtime(
        db,
        channel,
        summary=summary,
        error=None,
        extra={
            "last_chatroom_sync_started_at": sync_started_at.isoformat(),
            "last_chatroom_sync_target_count": len(deduped_targets),
            "last_chatroom_sync_profile_count": synced_profiles,
            "last_chatroom_sync_member_count": synced_members,
            "last_chatroom_sync_failed_targets": failed_targets,
            "last_chatroom_sync_summary": summary,
        },
    )
    db.commit()
    logger.info(
        "wechat ipad chatroom sync completed",
        extra={
            "channel_id": channel.id,
            "target_count": len(deduped_targets),
            "synced_profiles": synced_profiles,
            "synced_members": synced_members,
            "failed_target_count": len(failed_targets),
        },
    )
    return state


def run_wechat_ipad_action(
    db: Session,
    channel: Channel,
    *,
    action: str,
    params: dict[str, Any] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    payload = params or {}
    action_key = str(action or "").strip().lower()

    action_specs: dict[str, dict[str, Any]] = {
        "get_contact_list": {
            "func": api_client.get_contact_list,
            "kwargs": {
                "wxid": wxid,
                "current_wxcontact_seq": int(payload.get("current_wxcontact_seq") or 0),
                "current_chatroom_contact_seq": int(payload.get("current_chatroom_contact_seq") or 0),
            },
            "stage": "contact_list",
            "endpoint": IPAD_874_CONTRACT.endpoints.friend_get_list,
            "requires_wxid": True,
        },
        "get_contact_detail": {
            "func": api_client.get_contact_detail,
            "kwargs": {
                "wxid": wxid,
                "to_wxids": payload.get("to_wxids") or payload.get("wxids") or [],
                "chatroom_id": str(payload.get("chatroom_id") or ""),
            },
            "stage": "contact_detail",
            "endpoint": IPAD_874_CONTRACT.endpoints.friend_get_detail,
            "requires_wxid": True,
        },
        "get_profile": {
            "func": api_client.get_profile,
            "kwargs": {
                "wxid": wxid,
                "target_wxid": str(payload.get("target_wxid") or payload.get("wxid") or wxid),
            },
            "stage": "user_profile",
            "endpoint": IPAD_874_CONTRACT.endpoints.user_get_profile,
            "requires_wxid": True,
        },
        "get_my_qrcode": {
            "func": api_client.get_my_qrcode,
            "kwargs": {
                "wxid": wxid,
                "style": int(payload.get("style") or 0),
            },
            "stage": "my_qrcode",
            "endpoint": IPAD_874_CONTRACT.endpoints.user_get_qrcode,
            "requires_wxid": True,
        },
        "get_chatroom_qrcode": {
            "func": api_client.get_chatroom_qrcode,
            "kwargs": {
                "wxid": wxid,
                "chatroom_id": str(payload.get("chatroom_id") or ""),
            },
            "stage": "chatroom_qrcode",
            "endpoint": IPAD_874_CONTRACT.endpoints.group_get_qrcode,
            "requires_wxid": True,
        },
        "get_chatroom_announce": {
            "func": api_client.get_chatroom_announce,
            "kwargs": {
                "wxid": wxid,
                "chatroom_id": str(payload.get("chatroom_id") or ""),
            },
            "stage": "chatroom_announce",
            "endpoint": IPAD_874_CONTRACT.endpoints.group_get_info_detail,
            "requires_wxid": True,
        },
        "invite_chatroom_member": {
            "func": api_client.invite_chatroom_member,
            "kwargs": {
                "wxid": wxid,
                "chatroom_id": str(payload.get("chatroom_id") or ""),
                "to_wxids": payload.get("to_wxids") or payload.get("wxids") or [],
            },
            "stage": "chatroom_invite",
            "endpoint": IPAD_874_CONTRACT.endpoints.group_invite_member,
            "requires_wxid": True,
        },
        "accept_friend": {
            "func": api_client.accept_friend,
            "kwargs": {
                "wxid": wxid,
                "scene": int(payload.get("scene") or 0),
                "v1": str(payload.get("v1") or ""),
                "v2": str(payload.get("v2") or ""),
            },
            "stage": "accept_friend",
            "endpoint": IPAD_874_CONTRACT.endpoints.friend_pass_verify,
            "requires_wxid": True,
        },
        "download_image": {
            "func": api_client.download_image,
            "kwargs": {
                "wxid": wxid,
                "file_aes_key": str(payload.get("file_aes_key") or payload.get("aeskey") or ""),
                "file_no": str(payload.get("file_no") or payload.get("cdnmidimgurl") or ""),
            },
            "stage": "download_image",
            "endpoint": IPAD_874_CONTRACT.endpoints.tools_download_image,
            "requires_wxid": True,
        },
        "download_voice": {
            "func": api_client.download_voice,
            "kwargs": {
                "wxid": wxid,
                "msg_id": str(payload.get("msg_id") or ""),
                "from_user_name": str(payload.get("from_user_name") or ""),
                "bufid": str(payload.get("bufid") or ""),
                "length": int(payload.get("length") or 0),
            },
            "stage": "download_voice",
            "endpoint": IPAD_874_CONTRACT.endpoints.tools_download_voice,
            "requires_wxid": True,
        },
        "download_video": {
            "func": api_client.download_video,
            "kwargs": {
                "wxid": wxid,
                "msg_id": str(payload.get("msg_id") or ""),
                "to_wxid": str(payload.get("to_wxid") or ""),
                "data_len": int(payload.get("data_len") or 0),
                "start_pos": int(payload.get("start_pos") or 0),
                "section_length": int(payload.get("section_length") or payload.get("data_len") or 0),
                "compress_type": int(payload.get("compress_type") or 0),
            },
            "stage": "download_video",
            "endpoint": IPAD_874_CONTRACT.endpoints.tools_download_video,
            "requires_wxid": True,
        },
    }

    spec = action_specs.get(action_key)
    if not spec:
        raise ValueError(f"unsupported wechat_ipad action: {action}")
    if spec["requires_wxid"] and not wxid:
        return _persist_runtime(
            db,
            channel,
            summary=f"wechat_ipad 动作 {action_key} 被阻塞：缺少 wxid。",
            error="missing_wxid",
            extra={
                "last_action_name": action_key,
                "last_action_ok": False,
                "last_action_params": payload,
            },
        )

    missing_field = _detect_wechat_ipad_action_missing_field(action_key, spec["kwargs"])
    if missing_field:
        raise ValueError(f"{action_key} missing required field: {missing_field}")

    result = _safe_call(
        spec["func"],
        stage=spec["stage"],
        trace_endpoint=str(spec["endpoint"]),
        channel=channel,
        **spec["kwargs"],
    )
    action_summary = _build_wechat_ipad_action_summary(action_key, result)
    extra = {
        "last_action_name": action_key,
        "last_action_at": _utcnow_iso(),
        "last_action_ok": bool(result["ok"]),
        "last_action_params": payload,
        "last_action_response": result.get("payload"),
        "last_action_summary": action_summary,
        "last_action_result_preview": _build_wechat_ipad_action_preview(result.get("payload")),
    }
    return _persist_runtime(
        db,
        channel,
        summary=action_summary,
        error=None if result["ok"] else result["error"],
        error_context=None if result["ok"] else result.get("error_context"),
        extra=extra,
    )


def auto_recover_wechat_ipad_login(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    now = now_shanghai()
    last_attempt_at = _parse_datetime_like(runtime_info.get("last_auto_recover_at"))
    qrcode_status = str(getattr(channel, "qrcode_status", "") or runtime_info.get("qrcode_status") or "").strip()
    login_state = str(getattr(channel, "login_state", "") or "").strip()

    if not wxid:
        return {"ok": False, "skipped": True, "reason": "missing_wxid"}
    if not force and qrcode_status in {"waiting_scan", "scanned", "confirmed"}:
        return {"ok": False, "skipped": True, "reason": "qr_flow_active"}
    if not force and login_state in {"online", "reconnecting"}:
        return {"ok": True, "skipped": True, "reason": "state_not_recoverable"}
    if last_attempt_at and not force and (now - last_attempt_at).total_seconds() < WECHAT_IPAD_AUTO_RECOVER_COOLDOWN_SECONDS:
        return {"ok": False, "skipped": True, "reason": "cooldown"}

    logger.info(
        "wechat ipad auto recover started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "login_state": login_state,
            "qrcode_status": qrcode_status,
        },
    )
    runtime_info["last_auto_recover_at"] = _utcnow_iso()
    state = reconnect_wechat_ipad(db, channel, client=api_client)
    logger.info(
        "wechat ipad auto recover completed",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "reconnect_status": state.get("reconnect_status") if isinstance(state, dict) else "",
            "login_state": state.get("login_state") if isinstance(state, dict) else "",
        },
    )
    return {"ok": True, "skipped": False, "reason": "reconnect_triggered", "state": state}


def record_wechat_ipad_receive_event(
    db: Session,
    channel: Channel,
    *,
    event_key: str | None,
    mapped: bool,
    deduplicated: bool,
    source: str,
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    runtime_info["last_receive_at"] = _utcnow_iso()
    runtime_info["last_receive_event_key"] = event_key or None
    runtime_info["last_receive_summary"] = (
        f"收到 {source} 事件，event_key={event_key or '-'}，"
        f"{'命中去重' if deduplicated else '已入处理链路'}。"
    )
    runtime_info["last_inbound_protocol_summary"] = {
        "source": source,
        "event_key": event_key,
        "mapped": mapped,
        "deduplicated": deduplicated,
    }
    runtime_info["receive_total"] = int(runtime_info.get("receive_total") or 0) + 1
    if deduplicated:
        runtime_info["dedupe_total"] = int(runtime_info.get("dedupe_total") or 0) + 1
        runtime_info["last_dedupe_event_key"] = event_key or None
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    return build_wechat_ipad_overview(channel)


def record_wechat_ipad_send_event(
    db: Session,
    channel: Channel,
    *,
    adapter_result: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    receiver_id = payload.get("receiver_id") or payload.get("external_id") or "-"
    runtime_info["last_send_at"] = _utcnow_iso()
    runtime_info["last_send_summary"] = (
        f"发送 {payload.get('message_type') or 'text'} 到 {receiver_id}，"
        f"回执 {adapter_result.get('external_status') or adapter_result.get('status') or 'unknown'}。"
    )
    runtime_info["last_send_result"] = {
        "ok": bool(adapter_result.get("ok")),
        "status": adapter_result.get("status"),
        "transport_status": adapter_result.get("transport_status"),
        "endpoint": adapter_result.get("endpoint"),
        "external_message_id": adapter_result.get("external_message_id"),
        "client_msg_id": adapter_result.get("client_msg_id"),
        "create_time": adapter_result.get("create_time"),
        "new_msg_id": adapter_result.get("new_msg_id"),
    }
    runtime_info["last_outbound_protocol_summary"] = {
        "direction": adapter_result.get("direction"),
        "session_id": adapter_result.get("session_id"),
        "receiver_id": receiver_id,
        "endpoint": adapter_result.get("endpoint"),
        "external_message_id": adapter_result.get("external_message_id"),
        "client_msg_id": adapter_result.get("client_msg_id"),
        "new_msg_id": adapter_result.get("new_msg_id"),
    }
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    db.commit()
    db.refresh(channel)
    return build_wechat_ipad_overview(channel)


def _persist_runtime(
    db: Session,
    channel: Channel,
    *,
    login_state: str | None = None,
    qrcode_status: str | None = None,
    summary: str | None = None,
    error: str | None = None,
    error_context: dict[str, Any] | None = None,
    heartbeat_at: datetime | None = None,
    login_at: datetime | None = None,
    sync_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    if summary is not None:
        runtime_info["last_status_summary"] = summary
    if sync_at is not None:
        runtime_info["last_sync_summary"] = summary
    if error is not None:
        runtime_info["last_error"] = error
        runtime_info["last_error_context"] = _normalize_error_context(error_context) if error_context else {}
    elif "last_error" in runtime_info:
        runtime_info.pop("last_error", None)
        runtime_info.pop("last_error_context", None)
    elif "last_error_context" in runtime_info:
        runtime_info.pop("last_error_context", None)
    if not runtime_info.get("last_error_context"):
        runtime_info.pop("last_error_context", None)
    if extra:
        runtime_info.update(extra)
    runtime_info = _compact_wechat_ipad_runtime_info(runtime_info)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    if login_state is not None:
        channel.login_state = login_state
    if qrcode_status is not None:
        channel.qrcode_status = qrcode_status
    if heartbeat_at is not None:
        channel.last_heartbeat_at = heartbeat_at
    if login_at is not None:
        channel.last_login_at = login_at
    if sync_at is not None:
        channel.last_message_sync_at = sync_at
    db.commit()
    db.refresh(channel)
    logger.info(
        "wechat ipad runtime persisted",
        extra={
            "channel_id": channel.id,
            "login_state": channel.login_state,
            "qrcode_status": channel.qrcode_status,
            "status": channel.status,
            "last_login_at": str(channel.last_login_at or ""),
            "last_heartbeat_at": str(channel.last_heartbeat_at or ""),
            "last_message_sync_at": str(channel.last_message_sync_at or ""),
            "summary": summary or "",
            "error": error or "",
            "runtime_keys": sorted(runtime_info.keys()),
        },
    )
    return build_wechat_ipad_overview(channel)


def _safe_call(func: Any, *, stage: str, trace_endpoint: str | None = None, **kwargs: Any) -> dict[str, Any]:
    logger.info(
        "wechat ipad provider call started",
        extra={
            "stage": stage,
            "endpoint": trace_endpoint or "",
            "params": _summarize_wechat_call_kwargs(kwargs),
        },
    )
    try:
        payload = func(**kwargs)
    except Exception as exc:
        logger.exception(
            "wechat ipad provider call raised",
            extra={
                "stage": stage,
                "endpoint": trace_endpoint or "",
                "params": _summarize_wechat_call_kwargs(kwargs),
            },
        )
        return {
            "ok": False,
            "error": str(exc),
            "payload": {},
            "data": {},
            "message": None,
            "code": None,
            "error_context": _normalize_error_context({"stage": stage, "endpoint": trace_endpoint, "at": _utcnow_iso()}),
        }
    http_status = int(payload.get("http_status") or 200)
    ok = http_status < 400 and WechatIpadApiClient.is_success(payload)
    logger.info(
        "wechat ipad provider call completed",
        extra={
            "stage": stage,
            "endpoint": trace_endpoint or "",
            "ok": ok,
            "http_status": http_status,
            "code": WechatIpadApiClient.extract_code(payload),
            "provider_message": WechatIpadApiClient.extract_message(payload) or "",
            "payload_preview": _summarize_provider_payload(payload),
        },
    )
    return {
        "ok": ok,
        "error": None if ok else (WechatIpadApiClient.extract_message(payload) or "provider_error"),
        "payload": payload,
        "data": WechatIpadApiClient.extract_data(payload),
        "message": WechatIpadApiClient.extract_message(payload),
        "code": WechatIpadApiClient.extract_code(payload),
        "error_context": None if ok else _normalize_error_context({"stage": stage, "endpoint": trace_endpoint, "at": _utcnow_iso()}),
    }


def _extract_message_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return WechatIpadApiClient.extract_sync_fields(payload)["list"]


def _detect_wechat_ipad_action_missing_field(action: str, kwargs: dict[str, Any]) -> str | None:
    required_fields = {
        "get_contact_detail": ("to_wxids",),
        "get_chatroom_qrcode": ("chatroom_id",),
        "get_chatroom_announce": ("chatroom_id",),
        "invite_chatroom_member": ("chatroom_id", "to_wxids"),
        "accept_friend": ("scene", "v1", "v2"),
        "download_image": ("file_aes_key", "file_no"),
        "download_voice": ("msg_id", "from_user_name", "bufid", "length"),
        "download_video": ("msg_id", "to_wxid", "data_len", "section_length"),
    }
    for field in required_fields.get(action, ()):
        value = kwargs.get(field)
        if isinstance(value, list):
            if value:
                continue
        elif value not in (None, "", 0):
            continue
        return field
    return None


def _build_wechat_ipad_action_summary(action: str, result: dict[str, Any]) -> str:
    if result.get("ok"):
        return f"wechat_ipad 动作 {action} 调用成功。"
    message = str(result.get("error") or result.get("message") or "provider_error")
    return f"wechat_ipad 动作 {action} 调用失败：{message}"


def _build_wechat_ipad_action_preview(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    preview: dict[str, Any] = {}
    for key in ("Success", "Code", "Message", "Msg", "Ret", "http_status"):
        if key in payload:
            preview[key] = payload.get(key)
    data = WechatIpadApiClient.extract_data(payload)
    if data:
        preview["data_keys"] = sorted(data.keys())[:10]
    return preview


def _collect_chatroom_sync_candidates_from_results(
    db: Session,
    channel: Channel,
    ingest_results: list[dict[str, Any]],
) -> dict[str, Any]:
    chatroom_ids: list[str] = []
    signals: list[dict[str, str]] = []
    seen: set[str] = set()

    for result in ingest_results:
        normalized = result.get("normalized") if isinstance(result, dict) else None
        signal = _build_chatroom_sync_signal(db, channel, normalized if isinstance(normalized, dict) else {})
        if not signal:
            continue
        chatroom_id = signal["chatroom_id"]
        if chatroom_id in seen:
            continue
        seen.add(chatroom_id)
        chatroom_ids.append(chatroom_id)
        signals.append(signal)

    return {"chatroom_ids": chatroom_ids, "signals": signals}


def should_sync_wechat_ipad_chatroom_for_event(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    entity_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    signal = _build_chatroom_sync_signal(db, channel, normalized, entity_state=entity_state)
    if signal:
        logger.info(
            "wechat ipad chatroom sync signal matched",
            extra={
                "channel_id": channel.id,
                "chatroom_id": signal.get("chatroom_id"),
                "chatroom_ids": signal.get("chatroom_ids") or [],
                "reason": signal.get("reason"),
                "event_type": signal.get("event_type"),
                "message_type": signal.get("message_type"),
                "event_key": signal.get("event_key"),
            },
        )
    else:
        conversation_payload = normalized.get("conversation") or {}
        message_payload = normalized.get("message") or {}
        details = normalized.get("details") or {}
        logger.info(
            "wechat ipad chatroom sync signal skipped",
            extra={
                "channel_id": channel.id,
                "chatroom_id": str(conversation_payload.get("external_id") or "").strip(),
                "chat_type": str(conversation_payload.get("chat_type") or ""),
                "event_type": str(normalized.get("event_type") or ""),
                "message_type": str(message_payload.get("message_type") or ""),
                "event_key": str(normalized.get("event_key") or ""),
                "direction": str(details.get("direction") or ""),
                "reason": "no_match",
            },
        )
    return signal


def _build_chatroom_sync_signal(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    entity_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    details = normalized.get("details") or {}
    chatroom_ids = _extract_chatroom_sync_target_ids(normalized)
    if not chatroom_ids:
        return None
    chatroom_id = chatroom_ids[0]

    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == chatroom_id)
    )
    message_type = str(message_payload.get("message_type") or "").strip().lower()
    content = str(message_payload.get("content") or "").strip()
    event_type = str(normalized.get("event_type") or "").strip()
    state = entity_state if isinstance(entity_state, dict) else {}
    cache_chatroom_known = bool(state.get("cache_chatroom_known"))
    cache_member_known = bool(state.get("cache_member_known"))
    chatroom_created = bool(state.get("chatroom_created"))
    member_created = bool(state.get("member_created"))

    reason = ""
    if _is_wechat_ipad_member_change_event(
        event_type=event_type,
        message_type=message_type,
        content=content,
    ):
        reason = "member_change_event"
    elif not cache_chatroom_known or chatroom_created or not chatroom:
        reason = "new_chatroom_observed"
    elif not cache_member_known or member_created:
        reason = "member_not_cached"
    elif str(chatroom.status or "").strip() in {"message_observed", "pending_sync"}:
        reason = "chatroom_profile_incomplete"

    if not reason:
        return None

    return {
        "chatroom_id": chatroom_id,
        "chatroom_ids": chatroom_ids,
        "reason": reason,
        "event_key": str(normalized.get("event_key") or "").strip() or "-",
        "message_type": message_type or "unknown",
        "sender_id": str(message_payload.get("sender_id") or "").strip() or "-",
        "content_preview": _build_chatroom_signal_preview(content),
        "status_before_sync": str(getattr(chatroom, "status", None) or "missing"),
        "direction": str(details.get("direction") or "in"),
        "event_type": event_type or "message",
        "cache_chatroom_known": "true" if cache_chatroom_known else "false",
        "cache_member_known": "true" if cache_member_known else "false",
    }


def _extract_chatroom_sync_target_ids(normalized: dict[str, Any]) -> list[str]:
    conversation_payload = normalized.get("conversation") or {}
    details = normalized.get("details") or {}
    targets: list[str] = []
    seen: set[str] = set()

    conversation_chatroom_id = str(conversation_payload.get("external_id") or "").strip()
    if (
        str(conversation_payload.get("chat_type") or "").strip() == "group"
        and conversation_chatroom_id.endswith("@chatroom")
        and conversation_chatroom_id not in seen
    ):
        seen.add(conversation_chatroom_id)
        targets.append(conversation_chatroom_id)

    mod_contacts_ids = details.get("mod_contacts_chatroom_ids")
    if isinstance(mod_contacts_ids, list):
        for item in mod_contacts_ids:
            chatroom_id = str(item or "").strip()
            if not chatroom_id or not chatroom_id.endswith("@chatroom") or chatroom_id in seen:
                continue
            seen.add(chatroom_id)
            targets.append(chatroom_id)

    return targets


def _is_wechat_ipad_member_change_event(*, event_type: str, message_type: str, content: str) -> bool:
    normalized_event_type = event_type.strip().lower()
    normalized_content = content.strip().lower()
    if normalized_event_type in {"modcontacts", "mod_contacts", "sync_push", "sync_callback", "sync_push_callback"}:
        return True
    if "modcontacts" in normalized_event_type:
        return True
    if message_type != "system":
        return False
    if "<sysmsg" in normalized_content or "<memberlist" in normalized_content or "sysmsgtemplate" in normalized_content:
        return True
    for keyword in (
        "加入了群聊",
        "加入群",
        "邀请",
        "移出群聊",
        "移出了群聊",
        "踢出群聊",
        "removed from the group",
        "joined the group",
        "invited",
        "scan the qr code to join",
    ):
        if keyword in normalized_content:
            return True
    return False


def _build_chatroom_signal_preview(content: str) -> str:
    cleaned = " ".join(str(content or "").split())
    if len(cleaned) <= 80:
        return cleaned or "-"
    return f"{cleaned[:77]}..."


def _extract_cache_summary(cache_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "wxid": cache_data.get("Wxid") or cache_data.get("UserName"),
        "nickname": cache_data.get("NickName") or cache_data.get("nickname"),
        "device_id": cache_data.get("DeviceID"),
    }


def _build_account_snapshot(runtime_info: dict[str, Any], adapter_config: dict[str, Any]) -> dict[str, str]:
    last_cache_info_response = runtime_info.get("last_cache_info_response")
    cache_payload = last_cache_info_response if isinstance(last_cache_info_response, dict) else {}
    cache_data = WechatIpadApiClient.extract_data(cache_payload) if cache_payload else {}
    if not isinstance(cache_data, dict):
        cache_data = {}
    last_user_profile_response = runtime_info.get("last_user_profile_response")
    profile_payload = last_user_profile_response if isinstance(last_user_profile_response, dict) else {}
    profile_data = WechatIpadApiClient.extract_data(profile_payload) if profile_payload else {}
    if not isinstance(profile_data, dict):
        profile_data = {}
    profile_fields = WechatIpadApiClient.extract_account_fields(profile_payload) if profile_payload else {}
    wxid = str(
        runtime_info.get("active_wxid")
        or profile_fields.get("wxid")
        or profile_data.get("Wxid")
        or profile_data.get("UserName")
        or cache_data.get("Wxid")
        or cache_data.get("UserName")
        or adapter_config.get("wxid")
        or ""
    ).strip() or "-"
    nickname = str(
        runtime_info.get("active_nickname")
        or profile_fields.get("nickname")
        or profile_data.get("NickName")
        or profile_data.get("nickname")
        or cache_data.get("NickName")
        or cache_data.get("nickname")
        or ""
    ).strip() or "-"
    alias = str(
        runtime_info.get("active_alias")
        or profile_fields.get("alias")
        or profile_data.get("Alias")
        or profile_data.get("alias")
        or cache_data.get("Alias")
        or cache_data.get("alias")
        or cache_data.get("Wechat")
        or cache_data.get("WeChat")
        or ""
    ).strip() or "-"
    bind_mobile = str(
        runtime_info.get("active_bind_mobile")
        or profile_fields.get("bind_mobile")
        or profile_data.get("BindMobile")
        or profile_data.get("bind_mobile")
        or cache_data.get("BindMobile")
        or cache_data.get("bind_mobile")
        or cache_data.get("Mobile")
        or cache_data.get("mobile")
        or ""
    ).strip() or "-"
    device_name = str(
        adapter_config.get("device_name")
        or cache_data.get("DeviceName")
        or cache_data.get("device_name")
        or ""
    ).strip() or "-"
    return {
        "wxid": wxid,
        "nickname": nickname,
        "alias": alias,
        "bind_mobile": bind_mobile,
        "device_name": device_name,
    }


def _build_pull_summary(messages: list[dict[str, Any]], sync_fields: dict[str, Any]) -> dict[str, Any]:
    first = messages[0] if messages else {}
    return {
        "message_count": len(messages),
        "continue_flag": sync_fields.get("continue_flag"),
        "sync_key": sync_fields.get("sync_key"),
        "first_event": first.get("Event") if isinstance(first, dict) else None,
    }


def _build_error_context_view(value: Any, *, now: datetime | None = None) -> dict[str, str | None]:
    context = _normalize_error_context(value)
    freshness = _build_recent_call_freshness(context.get("at"), now=now)
    return {
        "stage": context.get("stage") or "-",
        "endpoint": context.get("endpoint") or "-",
        "at": context.get("at"),
        "at_display": _format_beijing_time(context.get("at")),
        "freshness_key": freshness["key"],
        "freshness_label": freshness["label"],
        "freshness_tone": freshness["tone"],
        "freshness_hint": freshness["hint"],
    }


def _normalize_error_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key in ("stage", "endpoint", "at"):
        current = str(value.get(key) or "").strip()
        if current:
            normalized[key] = current
    return normalized


def _build_error_operator_summary(error: Any, error_context: Any, *, now: datetime | None = None) -> str:
    message = str(error or "").strip()
    if not message:
        return "-"
    context = _normalize_error_context(error_context)
    stage = str(context.get("stage") or "").strip()
    endpoint = str(context.get("endpoint") or "").strip()
    at_display = _format_beijing_time(context.get("at"))
    freshness = _build_recent_call_freshness(context.get("at"), now=now)
    stage_label = _format_error_stage_label(stage)

    parts: list[str] = []
    if stage_label != "-":
        parts.append(f"{stage_label}阶段")
    if endpoint:
        parts.append(f"接口 {endpoint}")
    if at_display and at_display != "-":
        parts.append(f"时间 {at_display}")
    if freshness["key"] != "no_sample":
        parts.append(f"时效 {freshness['label']}")
    if parts:
        return f"{'，'.join(parts)}；最近错误：{message}。"
    return f"最近错误：{message}；当前缺少阶段、接口和时间上下文。"


def _build_error_diagnostics(error: Any, error_context: Any) -> dict[str, str]:
    message = str(error or "").strip()
    if not message:
        return {
            "state": "empty",
            "state_label": "当前无错误",
            "state_tone": "success",
            "category": "无",
            "operator_hint": "最近没有记录到错误；若链路仍异常，优先执行状态同步或消息拉取，补齐最新运行态样本。",
        }

    context = _normalize_error_context(error_context)
    stage = str(context.get("stage") or "").strip()
    normalized_stage = _normalize_error_stage_name(stage)
    category = _classify_error_category(
        stage=stage,
        endpoint=context.get("endpoint"),
        message=message,
    )
    if category == "login":
        return {
            "state": "warning",
            "state_label": "登录态异常",
            "state_tone": "outline",
            "category": "登录流程",
            "operator_hint": _build_error_operator_hint(category="login", stage=normalized_stage),
        }
    if category == "sync":
        return {
            "state": "warning",
            "state_label": "同步异常",
            "state_tone": "outline",
            "category": "消息同步",
            "operator_hint": _build_error_operator_hint(category="sync", stage=normalized_stage),
        }
    if category == "runtime":
        return {
            "state": "warning",
            "state_label": "运行态异常",
            "state_tone": "outline",
            "category": "运行态检查",
            "operator_hint": _build_error_operator_hint(category="runtime", stage=normalized_stage),
        }
    return {
        "state": "warning",
        "state_label": "待归类异常",
        "state_tone": "outline",
        "category": "待人工判断",
        "operator_hint": "最近错误已落库，但阶段分类不足；建议结合错误摘要、接口和原始回包继续定位。",
    }


def _build_error_attention(
    *,
    has_error: bool,
    freshness_key: str,
    state_label: str,
) -> dict[str, str]:
    if not has_error:
        return {
            "label": "无需处理",
            "tone": "success",
            "action": "当前没有最近错误样本，保持常规观察即可。",
        }
    if freshness_key == "fresh":
        return {
            "label": "优先处理",
            "tone": "outline",
            "action": f"最近错误仍是新鲜样本，建议按“{state_label}”立即核对对应接口与最新回包。",
        }
    if freshness_key == "stale":
        return {
            "label": "先刷新再判断",
            "tone": "outline",
            "action": "最近错误样本已偏旧，建议先执行状态同步或相关探活，确认问题是否仍在持续。",
        }
    if freshness_key == "expired":
        return {
            "label": "历史样本",
            "tone": "muted",
            "action": "最近错误样本已过旧，建议先刷新运行态与同步样本，再决定是否继续处置旧错误。",
        }
    return {
        "label": "样本不足",
        "tone": "muted",
        "action": "最近错误缺少可用时间样本，建议先补一次最新状态同步或探活结果。",
    }


def _build_error_operator_snapshot(
    error: Any,
    error_context: Any,
    *,
    error_diagnostics: dict[str, str],
    operator_summary: str,
    now: datetime | None = None,
) -> dict[str, str]:
    message = str(error or "").strip()
    context = _normalize_error_context(error_context)
    stage_label = _format_error_stage_label(context.get("stage"))
    endpoint = str(context.get("endpoint") or "").strip() or "-"
    at_display = _format_beijing_time(context.get("at"))
    freshness = _build_recent_call_freshness(context.get("at"), now=now)
    state_label = str(error_diagnostics.get("state_label") or "待归类异常")
    state_tone = str(error_diagnostics.get("state_tone") or "outline")
    category = str(error_diagnostics.get("category") or "-")
    operator_hint = str(error_diagnostics.get("operator_hint") or "").strip()
    attention = _build_error_attention(
        has_error=bool(message),
        freshness_key=str(freshness.get("key") or "no_sample"),
        state_label=state_label,
    )

    if not message:
        return {
            "status_key": "no_error",
            "status_label": "当前无错误",
            "status_tone": "success",
            "headline": "当前错误态：当前无错误",
            "summary": operator_hint or "最近没有记录到错误；当前没有需要优先处理的失败样本。",
            "detail": "关键信息：最近错误=-；阶段=-；接口=-；时间=-；时效=尚无时间样本；处置=无需处理。",
            "attention_label": attention["label"],
            "attention_tone": attention["tone"],
            "attention_action": attention["action"],
        }

    detail_parts = [
        f"分类={state_label}",
        f"归类={category}",
        f"阶段={stage_label if stage_label != '-' else '-'}",
        f"接口={endpoint}",
        f"时间={at_display if at_display and at_display != '-' else '-'}",
        f"时效={freshness['label']}",
        f"处置={attention['label']}",
        f"最近错误={message}",
    ]
    summary = operator_hint or operator_summary or f"最近一次错误已落库，可按阶段、接口和时间继续排查。"
    return {
        "status_key": str(error_diagnostics.get("state") or "warning"),
        "status_label": state_label,
        "status_tone": state_tone,
        "headline": f"当前错误态：{state_label}",
        "summary": summary,
        "detail": "关键信息：" + "；".join(detail_parts) + "。",
        "attention_label": attention["label"],
        "attention_tone": attention["tone"],
        "attention_action": attention["action"],
    }


def _format_error_stage_label(stage: str | None) -> str:
    raw = str(stage or "").strip()
    normalized = _normalize_error_stage_name(raw)
    if not normalized:
        return "-"
    labels = {
        "login_init": "登录初始化",
        "qrcode_refresh": "二维码刷新",
        "check_qr": "CheckQR",
        "login_confirm": "登录确认",
        "waiting_scan": "等待扫码",
        "reconnect": "重连恢复",
        "status_sync": "状态同步",
        "heartbeat": "HeartBeat",
        "heartbeat_long": "HeartBeatLong",
        "cache_info": "CacheInfo",
        "awaken": "Awaken",
        "twice_auto_auth": "自动登录",
        "message_sync": "消息同步",
        "contact_sync": "联系人同步",
        "chatroom_sync": "群资料同步",
        "sync_push": "同步回调",
    }
    return labels.get(normalized, raw or normalized)


def _normalize_error_stage_name(stage: str | None) -> str:
    normalized = str(stage or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return ""
    aliases = {
        "get_qr": "login_init",
        "getqr": "login_init",
        "login_qr": "login_init",
        "refresh_qr": "qrcode_refresh",
        "refresh_qrcode": "qrcode_refresh",
        "qrcode_refresh": "qrcode_refresh",
        "checkqr": "check_qr",
        "qr_check": "check_qr",
        "check_qrcode": "check_qr",
        "qrcode_check": "check_qr",
        "scan_qr_check": "check_qr",
        "qr_confirm": "login_confirm",
        "login_confirm": "login_confirm",
        "confirm_login": "login_confirm",
        "confirm_qr": "login_confirm",
        "waiting_confirm": "login_confirm",
        "wait_confirm": "login_confirm",
        "login_confirmation": "login_confirm",
        "device_confirm": "login_confirm",
        "scan_confirm": "login_confirm",
        "waiting_scan": "waiting_scan",
        "wait_scan": "waiting_scan",
        "scan_qr": "waiting_scan",
        "scan_code": "waiting_scan",
        "qrcode_wait_scan": "waiting_scan",
        "qr_code": "qrcode_refresh",
        "qrcode": "qrcode_refresh",
        "refresh_qr_code": "qrcode_refresh",
        "refresh_login_qrcode": "qrcode_refresh",
        "login_qrcode_refresh": "qrcode_refresh",
        "reconnect": "reconnect",
        "reconnect_login": "reconnect",
        "reconnect_probe": "reconnect",
        "relogin": "reconnect",
        "status_sync": "status_sync",
        "sync_status": "status_sync",
        "status_probe": "status_sync",
        "heart_beat": "heartbeat",
        "heartbeatlong": "heartbeat_long",
        "heart_beat_long": "heartbeat_long",
        "get_cache_info": "cache_info",
        "cacheinfo": "cache_info",
        "awaken_login": "awaken",
        "reconnect_awaken": "awaken",
        "awake": "awaken",
        "wakeup": "awaken",
        "wake_up": "awaken",
        "auto_auth": "twice_auto_auth",
        "twiceauth": "twice_auto_auth",
        "twice_autoauth": "twice_auto_auth",
        "second_auto_auth": "twice_auto_auth",
        "reconnect_auto_auth": "twice_auto_auth",
        "auto_login": "twice_auto_auth",
        "auto_login_confirm": "twice_auto_auth",
        "msg_sync": "message_sync",
        "msgsync": "message_sync",
        "sync_message": "message_sync",
        "pull_message": "message_sync",
        "message_pull": "message_sync",
        "sync_pull": "message_sync",
        "sync_messages": "message_sync",
        "message_sync_continue": "message_sync",
        "sync_contact": "contact_sync",
        "contacts_sync": "contact_sync",
        "contact_sync_pull": "contact_sync",
        "sync_contacts": "contact_sync",
        "chatroom_sync": "chatroom_sync",
        "sync_chatroom": "chatroom_sync",
        "chatroom_member_sync": "chatroom_sync",
        "sync_chatroom_members": "chatroom_sync",
        "sync_callback": "sync_push",
        "sync_push_callback": "sync_push",
    }
    return aliases.get(normalized, normalized)


def _classify_error_category(*, stage: str | None, endpoint: str | None, message: str | None) -> str:
    normalized_stage = _normalize_error_stage_name(stage)
    if normalized_stage in {"login_init", "qrcode_refresh", "check_qr", "login_confirm", "waiting_scan"}:
        return "login"
    if normalized_stage in {"message_sync", "contact_sync", "chatroom_sync", "sync_push"}:
        return "sync"
    if normalized_stage in {"reconnect", "status_sync", "heartbeat", "heartbeat_long", "cache_info", "awaken", "twice_auto_auth"}:
        return "runtime"

    evidence = " ".join(
        str(item or "").strip().lower()
        for item in (stage, endpoint, message)
        if str(item or "").strip()
    )
    if _contains_any_token(
        evidence,
        (
            "/api/login/getqr",
            "/api/login/checkqr",
            "/api/login/qrcode",
            "/api/login/confirm",
            "二维码",
            "qrcode",
            "扫码",
            "等待确认",
            "手机确认",
            "scan qr",
            "登录确认",
            "login confirm",
            "confirm login",
        ),
    ):
        return "login"
    if _contains_any_token(
        evidence,
        (
            "/api/msg/sync",
            "/api/message/sync",
            "/api/contact/sync",
            "/api/chatroom/sync",
            "sync_key",
            "synckey",
            "continue_flag",
            "continueflag",
            "消息同步",
            "拉取消息",
            "联系人同步",
            "群资料同步",
            "继续拉取",
            "同步失败",
        ),
    ):
        return "sync"
    if _contains_any_token(
        evidence,
        (
            "/api/login/heartbeat",
            "/api/login/heartbeatlong",
            "/api/login/getcacheinfo",
            "/api/login/awaken",
            "/api/login/twiceautoauth",
            "/api/login/reconnect",
            "heartbeat",
            "cacheinfo",
            "cache info",
            "awaken",
            "auto auth",
            "auto login",
            "自动登录",
            "重连",
            "重新连接",
            "唤醒",
            "状态同步",
        ),
    ):
        return "runtime"
    return "unknown"


def _build_error_operator_hint(*, category: str, stage: str) -> str:
    if category == "login":
        if stage in {"login_init", "qrcode_refresh"}:
            return "问题出在二维码准备链路；建议先核对 GetQR/二维码刷新是否返回 uuid、二维码链接和过期时间。"
        if stage in {"check_qr", "login_confirm", "waiting_scan"}:
            return "问题出在扫码或登录确认链路；建议先核对 CheckQR 最近回包、二维码状态和手机端确认进度。"
        return "问题出在二维码或登录确认链路；建议先检查二维码是否过期，再重新执行登录初始化或 CheckQR。"
    if category == "sync":
        if stage in {"contact_sync", "chatroom_sync", "sync_push"}:
            return "问题出在资料同步链路；建议先核对对应同步接口回包、continue_flag 与最近一次落库摘要。"
        return "问题出在消息拉取链路；建议先核对 sync_key、continue_flag 和最近一次 Msg/Sync 回包。"
    if stage in {"reconnect", "awaken"}:
        return "问题出在重连唤醒链路；建议先核对 Awaken 回包、新二维码或 wxid 是否已经刷新。"
    if stage == "twice_auto_auth":
        return "问题出在自动登录链路；建议先核对 TwiceAutoAuth 回包、wxid 与最近 HeartBeat 是否恢复。"
    return "问题出在运行态探活或重连链路；建议先检查 HeartBeat、CacheInfo 与重连接口回包是否一致。"


def _contains_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _build_sync_diagnostics(
    runtime_info: dict[str, Any],
    message_stats: dict[str, Any],
    summaries: dict[str, Any],
    *,
    last_message_sync_at: Any = None,
) -> dict[str, Any]:
    sync_key = str(runtime_info.get("sync_key") or "").strip()
    continue_flag = runtime_info.get("sync_continue_flag")
    last_error = str(runtime_info.get("last_error") or "").strip() or "-"
    last_sync_at_display = _format_beijing_time(last_message_sync_at or runtime_info.get("last_message_sync_at") or runtime_info.get("last_sync_at"))
    sync_state = _classify_sync_state(
        runtime_info=runtime_info,
        sync_key=sync_key,
        continue_flag=continue_flag,
        message_stats=message_stats,
        summaries=summaries,
        last_error=last_error,
    )
    operator_summary = _build_sync_operator_summary(
        sync_key=sync_key,
        continue_flag=continue_flag,
        message_stats=message_stats,
        last_error=last_error,
        state_label=sync_state["label"],
        last_sync_at_display=last_sync_at_display,
        error_context=runtime_info.get("last_error_context"),
    )
    operator_snapshot = _build_sync_operator_snapshot(
        sync_state=sync_state,
        sync_key=sync_key,
        continue_flag=continue_flag,
        message_stats=message_stats,
        last_error=last_error,
        last_sync_at_display=last_sync_at_display,
        error_context=runtime_info.get("last_error_context"),
    )
    return {
        "sync_key": sync_key or "-",
        "continue_flag": _format_continue_flag(continue_flag),
        "continue_flag_raw": continue_flag,
        "state": sync_state["state"],
        "state_label": sync_state["label"],
        "operator_hint": sync_state["hint"],
        "operator_summary": operator_summary,
        "operator_snapshot": operator_snapshot,
        "last_pull_protocol_summary": _format_protocol_summary(summaries.get("pull")),
        "last_inbound_protocol_summary": _format_protocol_summary(summaries.get("inbound")),
        "last_outbound_protocol_summary": _format_protocol_summary(summaries.get("outbound")),
        "last_sync_count": int(message_stats.get("last_sync_count") or 0),
        "last_sync_ingested": int(message_stats.get("last_sync_ingested") or 0),
        "last_sync_deduplicated": int(message_stats.get("last_sync_deduplicated") or 0),
        "receive_total": int(message_stats.get("receive_total") or 0),
        "dedupe_total": int(message_stats.get("dedupe_total") or 0),
        "last_error": last_error,
        "last_sync_at_display": last_sync_at_display,
    }


def _build_recent_message_flow_summary(runtime_info: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    receive_at = runtime_info.get("last_receive_at")
    send_at = runtime_info.get("last_send_at")
    receive_summary = str(runtime_info.get("last_receive_summary") or "").strip()
    send_summary = str(runtime_info.get("last_send_summary") or "").strip()
    receive_event_key = str(runtime_info.get("last_receive_event_key") or "").strip() or "-"
    dedupe_event_key = str(runtime_info.get("last_dedupe_event_key") or "").strip() or "-"
    receive_total = int(runtime_info.get("receive_total") or 0)
    dedupe_total = int(runtime_info.get("dedupe_total") or 0)
    inbound_summary = _parse_protocol_summary_dict(runtime_info.get("last_inbound_protocol_summary"))
    outbound_summary = _parse_protocol_summary_dict(runtime_info.get("last_outbound_protocol_summary"))

    receive_view = _build_recent_message_flow_side(
        kind="receive",
        at=receive_at,
        summary=receive_summary,
        reference=inbound_summary.get("source") or receive_event_key,
        fallback_reference=receive_event_key,
        now=now,
    )
    send_view = _build_recent_message_flow_side(
        kind="send",
        at=send_at,
        summary=send_summary,
        reference=outbound_summary.get("receiver_id") or outbound_summary.get("external_message_id"),
        fallback_reference="-",
        now=now,
    )
    focus = _select_recent_message_flow_focus(receive_view=receive_view, send_view=send_view)
    side_by_side_summary = _build_recent_message_flow_side_by_side_summary(
        receive_view=receive_view,
        send_view=send_view,
        focus=focus,
    )

    if receive_view["has_sample"] and send_view["has_sample"]:
        status_key = "both_active"
        status_label = "最近收发均有样本"
        status_tone = "success"
        summary = "最近接收与发送链路都已有样本，可直接判断两侧是否都在持续产生有效回包。"
    elif receive_view["has_sample"]:
        status_key = "receive_only"
        status_label = "仅最近接收有样本"
        status_tone = "outline"
        summary = "当前只看到最近接收样本；若运维关注出站链路，建议补一次发送动作确认回执。"
    elif send_view["has_sample"]:
        status_key = "send_only"
        status_label = "仅最近发送有样本"
        status_tone = "outline"
        summary = "当前只看到最近发送样本；若运维关注入站链路，建议补一次接收或拉取样本确认消息是否入链。"
    else:
        status_key = "no_sample"
        status_label = "尚无最近收发样本"
        status_tone = "muted"
        summary = "当前还没有最近接收或发送样本；先补一条入站或出站动作，这里才会形成可读摘要。"

    if focus["priority"] > 0 and focus["side"] in {"receive", "send"}:
        summary = f"{summary} 当前更值得先处理{focus['side_label']}。"

    attention = _build_recent_message_flow_attention(
        receive_view=receive_view,
        send_view=send_view,
        focus=focus,
    )
    operator_snapshot = _build_recent_message_flow_operator_snapshot(
        status_label=status_label,
        status_tone=status_tone,
        summary=summary,
        receive_view=receive_view,
        send_view=send_view,
        attention=attention,
        focus=focus,
        receive_total=receive_total,
        dedupe_total=dedupe_total,
        dedupe_event_key=dedupe_event_key,
    )

    detail = (
        f"优先关注={focus['side_label']}；"
        f"接收判断={receive_view['judgement_label']}；"
        f"发送判断={send_view['judgement_label']}；"
        f"接收：{receive_view['summary_line']}；"
        f"发送：{send_view['summary_line']}；"
        f"累计接收/去重={receive_total}/{dedupe_total}；"
        f"最近去重事件={dedupe_event_key}。"
    )
    operator_summary = (
        f"分类={status_label}；"
        f"优先关注={focus['side_label']}；"
        f"接收判断={receive_view['judgement_label']}；"
        f"发送判断={send_view['judgement_label']}；"
        f"接收={receive_view['operator_summary']}；"
        f"发送={send_view['operator_summary']}；"
        f"累计接收/去重={receive_total}/{dedupe_total}；"
        f"最近去重事件={dedupe_event_key}"
    )
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"最近收发摘要：{status_label}",
        "summary": summary,
        "detail": detail,
        "operator_summary": operator_summary,
        "attention_label": attention["label"],
        "attention_tone": attention["tone"],
        "attention_action": attention["action"],
        "freshness_label": attention["freshness_label"],
        "freshness_tone": attention["freshness_tone"],
        "focus_side": focus["side"],
        "focus_side_label": focus["side_label"],
        "focus_reason": focus["reason"],
        "side_by_side_summary": side_by_side_summary["summary"],
        "priority_side_summary": side_by_side_summary["priority_summary"],
        "operator_snapshot": operator_snapshot,
        "receive": receive_view,
        "send": send_view,
        "receive_total": receive_total,
        "dedupe_total": dedupe_total,
        "last_dedupe_event_key": dedupe_event_key,
    }


def _build_recent_message_flow_side(
    *,
    kind: str,
    at: Any,
    summary: str,
    reference: Any,
    fallback_reference: str,
    now: datetime | None = None,
) -> dict[str, str | bool]:
    reference_text = str(reference or "").strip() or fallback_reference
    called_at_display = _format_beijing_time(at)
    freshness = _build_recent_call_freshness(at, now=now)
    has_sample = any(item not in (None, "", "-") for item in (at, summary, reference_text if reference_text != "-" else ""))
    if has_sample:
        state_label = "有样本"
        state_tone = "success" if freshness["key"] == "fresh" else "outline"
        summary_line = (
            f"有样本，最近时间={called_at_display}，"
            f"时效={freshness['label']}，"
            f"{'来源' if kind == 'receive' else '接收方'}={reference_text}"
        )
        operator_summary = (
            f"有样本/时效={freshness['label']}/最近时间={called_at_display}/"
            f"{'来源' if kind == 'receive' else '接收方'}={reference_text}/摘要={summary or '-'}"
        )
    else:
        state_label = "暂无样本"
        state_tone = "muted"
        summary_line = "暂无样本，最近时间=-，时效=尚无时间样本。"
        operator_summary = "暂无样本/时效=尚无时间样本/最近时间=-/摘要=-"
    judgement = _build_recent_message_flow_side_judgement(
        kind=kind,
        has_sample=has_sample,
        freshness=freshness,
    )
    return {
        "has_sample": has_sample,
        "kind": kind,
        "side_label": "接收" if kind == "receive" else "发送",
        "state_label": state_label,
        "state_tone": state_tone,
        "called_at_display": called_at_display,
        "freshness_key": freshness["key"],
        "freshness_label": freshness["label"],
        "freshness_tone": freshness["tone"],
        "judgement_key": judgement["key"],
        "judgement_label": judgement["label"],
        "judgement_tone": judgement["tone"],
        "judgement_hint": judgement["hint"],
        "priority": judgement["priority"],
        "summary": summary or "-",
        "reference": reference_text,
        "summary_line": summary_line,
        "operator_summary": operator_summary,
        "ops_brief": f"{judgement['label']} / {freshness['label']} / {called_at_display}",
    }


def _build_recent_message_flow_side_by_side_summary(
    *,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
    focus: dict[str, Any],
) -> dict[str, str]:
    summary = (
        f"接收：{receive_view['judgement_label']}（{receive_view['freshness_label']}）"
        f"；发送：{send_view['judgement_label']}（{send_view['freshness_label']}）"
    )
    if focus["side"] in {"receive", "send"}:
        return {
            "summary": summary,
            "priority_summary": f"当前拖后腿侧：{focus['side_label']}；{focus['reason']}",
        }
    if focus["side_label"] == "双侧都需处理":
        return {
            "summary": summary,
            "priority_summary": f"当前拖后腿侧：双侧都需处理；{focus['reason']}",
        }
    return {
        "summary": summary,
        "priority_summary": f"当前拖后腿侧：双侧都正常；{focus['reason']}",
    }


def _build_recent_message_flow_operator_snapshot(
    *,
    status_label: str,
    status_tone: str,
    summary: str,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
    attention: dict[str, str],
    focus: dict[str, Any],
    receive_total: int,
    dedupe_total: int,
    dedupe_event_key: str,
) -> dict[str, Any]:
    ordered_views = [receive_view, send_view]
    if focus["side"] == "send":
        ordered_views = [send_view, receive_view]

    evidence_parts = [
        f"优先关注={focus['side_label']}",
        *[
            f"{view['side_label']}={view['judgement_label']} {view['freshness_label']} {view['called_at_display']}"
            for view in ordered_views
        ],
        f"累计接收/去重={receive_total}/{dedupe_total}",
        f"最近去重事件={dedupe_event_key}",
    ]
    items = []
    for view in ordered_views:
        items.append(
            {
                "label": view["side_label"],
                "state_label": view["state_label"],
                "state_tone": view["state_tone"],
                "freshness_label": view["freshness_label"],
                "freshness_tone": view["freshness_tone"],
                "judgement_label": view["judgement_label"],
                "judgement_tone": view["judgement_tone"],
                "detail": f"{view['called_at_display']} / {view['reference']}",
                "operator_summary": view["operator_summary"],
            }
        )
    return {
        "headline": f"最近收发摘要：{status_label}",
        "status_label": status_label,
        "status_tone": status_tone,
        "summary": summary,
        "evidence": "；".join(evidence_parts),
        "action": attention["action"],
        "focus_label": attention["label"],
        "focus_tone": attention["tone"],
        "focus_side": focus["side"],
        "focus_side_label": focus["side_label"],
        "focus_reason": focus["reason"],
        "freshness_label": attention["freshness_label"],
        "freshness_tone": attention["freshness_tone"],
        "items": items,
        "operator_summary": (
            f"当前判断={status_label}；"
            f"优先关注={focus['side_label']}；"
            f"接收判断={receive_view['judgement_label']}；"
            f"发送判断={send_view['judgement_label']}；"
            f"关键证据={';'.join(evidence_parts)}；"
            f"下一步={attention['action']}"
        ),
    }


def _build_recent_message_flow_attention(
    *,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
    focus: dict[str, Any],
) -> dict[str, str]:
    receive_has_sample = bool(receive_view.get("has_sample"))
    send_has_sample = bool(send_view.get("has_sample"))
    receive_freshness = str(receive_view.get("freshness_key") or "no_sample")
    send_freshness = str(send_view.get("freshness_key") or "no_sample")

    if receive_has_sample and send_has_sample and receive_freshness == "fresh" and send_freshness == "fresh":
        return {
            "label": "持续观察",
            "tone": "success",
            "action": "最近接收和发送都是新鲜样本，当前先按常规观察即可。",
            "freshness_label": "双向新鲜",
            "freshness_tone": "success",
        }
    if not receive_has_sample and not send_has_sample:
        return {
            "label": "补首批样本",
            "tone": "muted",
            "action": "当前收发两侧都还没有样本，建议先补一条入站或出站动作让卡片形成基线。",
            "freshness_label": "暂无样本",
            "freshness_tone": "muted",
        }
    if not receive_has_sample or not send_has_sample:
        missing_side = str(focus.get("target_label") or ("接收" if not receive_has_sample else "发送"))
        return {
            "label": f"先补{missing_side}样本",
            "tone": "outline",
            "action": f"当前缺少最近{missing_side}样本，建议优先补齐单侧链路，避免只凭另一侧样本误判整体收发状态。",
            "freshness_label": "单侧缺样本",
            "freshness_tone": "outline",
        }
    if "expired" in {receive_freshness, send_freshness}:
        expired_side = str(focus.get("target_label") or ("接收" if receive_freshness == "expired" else "发送"))
        return {
            "label": f"先刷新{expired_side}样本",
            "tone": "outline",
            "action": f"最近{expired_side}样本已过旧，建议先补一次最新动作再判断当前链路是否仍正常。",
            "freshness_label": f"{expired_side}侧过旧",
            "freshness_tone": "outline",
        }
    if focus["side"] in {"receive", "send"}:
        return {
            "label": f"先刷新{focus['target_label']}样本",
            "tone": "outline",
            "action": f"最近{focus['target_label']}样本已偏旧，建议优先补一次最新{focus['target_label']}动作后再继续排查。",
            "freshness_label": f"{focus['target_label']}侧偏旧",
            "freshness_tone": "outline",
        }
    return {
        "label": "刷新偏旧链路",
        "tone": "outline",
        "action": "最近收发样本已不够新鲜，建议补一次最新接收或发送动作后再继续排查。",
        "freshness_label": "存在偏旧样本",
        "freshness_tone": "outline",
    }


def _build_recent_message_flow_side_judgement(
    *,
    kind: str,
    has_sample: bool,
    freshness: dict[str, str],
) -> dict[str, str | int]:
    side_label = "接收" if kind == "receive" else "发送"
    freshness_key = str(freshness.get("key") or "no_sample")

    if not has_sample:
        return {
            "key": "no_sample",
            "label": f"{side_label}缺样本",
            "tone": "muted",
            "hint": f"{side_label}侧还没有最近样本，当前无法确认这侧链路是否真的在跑。",
            "priority": 100,
        }
    if freshness_key == "expired":
        return {
            "key": "expired",
            "label": f"{side_label}样本过旧",
            "tone": "outline",
            "hint": f"{side_label}侧样本已过旧，建议优先刷新这一侧。",
            "priority": 80,
        }
    if freshness_key == "stale":
        return {
            "key": "stale",
            "label": f"{side_label}样本偏旧",
            "tone": "outline",
            "hint": f"{side_label}侧样本已偏旧，建议尽快补一条新样本确认当前状态。",
            "priority": 40,
        }
    return {
        "key": "healthy",
        "label": f"{side_label}正常",
        "tone": "success",
        "hint": f"{side_label}侧已有新鲜样本，当前没有明显拖后腿迹象。",
        "priority": 0,
    }


def _select_recent_message_flow_focus(
    *,
    receive_view: dict[str, Any],
    send_view: dict[str, Any],
) -> dict[str, str | int]:
    candidates = [receive_view, send_view]
    ranked = sorted(
        candidates,
        key=lambda item: (
            -int(item.get("priority") or 0),
            0 if item.get("kind") == "receive" else 1,
        ),
    )
    top = ranked[0]
    second = ranked[1]
    top_priority = int(top.get("priority") or 0)
    second_priority = int(second.get("priority") or 0)

    if top_priority <= 0:
        return {
            "side": "both",
            "target_label": "双侧",
            "side_label": "双侧都正常",
            "reason": "接收和发送两侧当前都没有明显拖后腿迹象。",
            "priority": 0,
        }
    if top_priority == second_priority:
        return {
            "side": "both",
            "target_label": "双侧",
            "side_label": "双侧都需处理",
            "reason": f"接收和发送两侧当前同档异常：{receive_view['judgement_label']} / {send_view['judgement_label']}。",
            "priority": top_priority,
        }
    return {
        "side": str(top.get("kind") or "receive"),
        "target_label": str(top.get("side_label") or "接收"),
        "side_label": str(top.get("judgement_label") or "待判断"),
        "reason": str(top.get("judgement_hint") or "该侧当前更值得优先处理。"),
        "priority": top_priority,
    }


def _parse_protocol_summary_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _classify_sync_state(
    *,
    runtime_info: dict[str, Any],
    sync_key: str,
    continue_flag: Any,
    message_stats: dict[str, Any],
    summaries: dict[str, Any],
    last_error: str,
) -> dict[str, str]:
    last_sync_at = runtime_info.get("last_message_sync_at") or runtime_info.get("last_sync_at")
    has_protocol_summary = any(summaries.get(key) not in (None, "", {}) for key in ("pull", "inbound", "outbound"))
    has_sync_activity = bool(
        last_sync_at
        or sync_key
        or has_protocol_summary
        or int(message_stats.get("last_sync_count") or 0)
        or int(message_stats.get("last_sync_ingested") or 0)
        or int(message_stats.get("last_sync_deduplicated") or 0)
    )
    error_stage = str((_normalize_error_context(runtime_info.get("last_error_context")) or {}).get("stage") or "").strip()

    if not has_sync_activity:
        return {
            "state": "empty",
            "label": "尚未形成同步样本",
            "hint": "还没有最近同步记录；先执行状态同步或消息拉取，确认 sync_key 与协议摘要开始落库。",
        }
    if last_error != "-" and ("sync" in error_stage or error_stage == ""):
        return {
            "state": "warning",
            "label": "最近同步异常",
            "hint": f"最近一次同步相关错误为：{last_error}；建议先核对 message sync 接口回包与 sync_key 是否持续推进。",
        }
    if str(continue_flag) == "1":
        return {
            "state": "warning",
            "label": "仍有待续拉取",
            "hint": "continue_flag=1，表示服务端仍有积压消息；建议继续执行 Msg/Sync，直到 continue_flag 归零。",
        }
    if int(message_stats.get("last_sync_count") or 0) > 0 and int(message_stats.get("last_sync_ingested") or 0) <= 0:
        return {
            "state": "healthy",
            "label": "最近同步成功但无新增",
            "hint": "最近一次同步已完成，但本轮没有新增入库消息；更像是空拉取或重复消息回放，可继续观察 sync_key 是否推进。",
        }
    return {
        "state": "healthy",
        "label": "最近同步正常推进",
        "hint": "最近同步链路已有摘要，可结合 sync_key、协议摘要与计数继续排查。",
    }


def _build_sync_operator_summary(
    *,
    sync_key: str,
    continue_flag: Any,
    message_stats: dict[str, Any],
    last_error: str,
    state_label: str,
    last_sync_at_display: str,
    error_context: Any,
) -> str:
    parts = [f"分类={state_label}"]
    if last_sync_at_display and last_sync_at_display != "-":
        parts.append(f"最近拉取={last_sync_at_display}")
    if sync_key:
        parts.append(f"sync_key={sync_key}")
    formatted_continue_flag = _format_continue_flag(continue_flag)
    if formatted_continue_flag != "-":
        parts.append(f"continue_flag={formatted_continue_flag}")

    last_sync_count = int(message_stats.get("last_sync_count") or 0)
    last_sync_ingested = int(message_stats.get("last_sync_ingested") or 0)
    last_sync_deduplicated = int(message_stats.get("last_sync_deduplicated") or 0)
    parts.append(f"拉取/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}")

    if last_error != "-":
        stage = _format_error_stage_label(_normalize_error_context(error_context).get("stage"))
        if stage != "-":
            parts.append(f"异常阶段={stage}")
        parts.append(f"异常={last_error}")
    return "；".join(parts)


def _build_sync_operator_snapshot(
    *,
    sync_state: dict[str, str],
    sync_key: str,
    continue_flag: Any,
    message_stats: dict[str, Any],
    last_error: str,
    last_sync_at_display: str,
    error_context: Any,
) -> dict[str, str]:
    last_sync_count = int(message_stats.get("last_sync_count") or 0)
    last_sync_ingested = int(message_stats.get("last_sync_ingested") or 0)
    last_sync_deduplicated = int(message_stats.get("last_sync_deduplicated") or 0)
    context = _normalize_error_context(error_context)
    stage_label = _format_error_stage_label(context.get("stage"))
    continue_flag_display = _format_continue_flag(continue_flag)

    if sync_state["label"] == "尚未形成同步样本":
        return {
            "status_key": "no_sample",
            "status_label": "尚未形成同步样本",
            "status_tone": "muted",
            "headline": "当前同步档位：尚未形成同步样本",
            "summary": "还没有形成最近同步样本，当前更像是尚未完成首次状态同步或消息拉取。",
            "detail": "关键信息：sync_key=-；最近时间=-；当前还没有同步计数、新增计数或异常阶段可供判断。",
        }
    if sync_state["label"] == "最近同步异常":
        detail_parts = ["关键信息："]
        if sync_key:
            detail_parts.append(f"sync_key={sync_key}")
        if last_sync_at_display and last_sync_at_display != "-":
            detail_parts.append(f"最近时间={last_sync_at_display}")
        if stage_label != "-":
            detail_parts.append(f"异常阶段={stage_label}")
        detail_parts.append(f"最近同步条数={last_sync_count}")
        return {
            "status_key": "recent_error",
            "status_label": "最近同步异常",
            "status_tone": "outline",
            "headline": "当前同步档位：最近同步异常",
            "summary": "最近一次同步链路出现异常，建议先按异常阶段核对最近回包和 sync_key 推进情况。",
            "detail": "；".join(detail_parts) + (f"；最近错误={last_error}" if last_error != "-" else ""),
        }
    if str(continue_flag) == "1":
        return {
            "status_key": "pending_continue",
            "status_label": "待续拉取",
            "status_tone": "outline",
            "headline": "当前同步档位：待续拉取",
            "summary": "continue_flag=1，说明这一轮还没拉到末尾，服务端仍可能有积压消息等待继续同步。",
            "detail": (
                f"关键信息：sync_key={sync_key or '-'}；最近时间={last_sync_at_display or '-'}；"
                f"continue_flag={continue_flag_display}；最近同步/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}。"
            ),
        }
    if last_sync_count > 0 and last_sync_ingested <= 0:
        return {
            "status_key": "success_no_new",
            "status_label": "最近同步成功但无新增",
            "status_tone": "success",
            "headline": "当前同步档位：最近同步成功但无新增",
            "summary": "最近一次同步成功收口，但本轮没有新增入库，现阶段更像是空拉取或重复消息回放。",
            "detail": (
                f"关键信息：sync_key={sync_key or '-'}；最近时间={last_sync_at_display or '-'}；"
                f"最近同步/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}；"
                f"continue_flag={continue_flag_display}。"
            ),
        }
    return {
        "status_key": "progressing",
        "status_label": "最近同步正常推进",
        "status_tone": "success",
        "headline": "当前同步档位：最近同步正常推进",
        "summary": "最近同步链路已有新增或持续推进迹象，当前更像是正常收口中的运行样本。",
        "detail": (
            f"关键信息：sync_key={sync_key or '-'}；最近时间={last_sync_at_display or '-'}；"
            f"最近同步/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}；"
            f"continue_flag={continue_flag_display}。"
        ),
    }


def _format_continue_flag(value: Any) -> str:
    if value in (None, ""):
        return "-"
    if str(value) == "1":
        return "需继续拉取 (1)"
    if str(value) == "0":
        return "已到末尾 (0)"
    return str(value)


def _format_protocol_summary(value: Any) -> str:
    if isinstance(value, dict):
        ordered_parts: list[str] = []
        label_map = {
            "message_count": "消息数",
            "continue_flag": "continue_flag",
            "sync_key": "sync_key",
            "first_event": "首条事件",
            "source": "来源",
            "event_key": "event_key",
            "mapped": "已入链",
            "deduplicated": "命中去重",
            "direction": "方向",
            "session_id": "session_id",
            "receiver_id": "接收方",
            "endpoint": "endpoint",
            "external_message_id": "外部消息 ID",
            "client_msg_id": "client_msg_id",
            "new_msg_id": "new_msg_id",
        }
        for key in (
            "message_count",
            "continue_flag",
            "sync_key",
            "first_event",
            "source",
            "event_key",
            "mapped",
            "deduplicated",
            "direction",
            "receiver_id",
            "external_message_id",
            "new_msg_id",
            "endpoint",
        ):
            if value.get(key) in (None, ""):
                continue
            current = value.get(key)
            if isinstance(current, bool):
                current = "是" if current else "否"
            ordered_parts.append(f"{label_map.get(key, key)}={current}")
        return "；".join(ordered_parts) if ordered_parts else "-"
    text = str(value or "").strip()
    return text or "-"


def _build_qr_status_summary(qr_result: dict[str, Any]) -> str:
    if not qr_result:
        return "尚未获取二维码状态。"
    if _qr_login_succeeded(qr_result):
        return "CheckQR 返回登陆成功。"
    if qr_result["ok"]:
        return f"CheckQR 返回 {qr_result['message'] or '待确认状态'}。"
    return f"CheckQR 调用失败：{qr_result['error']}"


def _build_recent_call_view(
    value: Any,
    *,
    fallback_message: Any = None,
    called_at: Any = None,
    now: datetime | None = None,
) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    message = (
        WechatIpadApiClient.extract_message(payload)
        or str(fallback_message or "").strip()
        or "-"
    )
    code = WechatIpadApiClient.extract_code(payload)
    http_status = payload.get("http_status")
    has_payload = bool(payload)
    # 874 的成功取码回包是 Success=true、Code=1；结构化成功标记优先于不同接口各自的返回码约定。
    reported_success = payload.get("success")
    is_success = (
        has_payload
        and (
            bool(reported_success)
            if isinstance(reported_success, bool)
            else WechatIpadApiClient.is_success(payload)
        )
        and http_status in (None, "", 200, "200")
    )
    has_structured_summary = message != "-"
    if is_success and has_structured_summary:
        state = "healthy"
        state_label = "调用成功"
        operator_hint = "最近一次调用返回成功回包，可继续结合消息和时间判断链路是否持续推进。"
    elif is_success:
        state = "blank"
        state_label = "返回空摘要"
        operator_hint = "最近一次调用没有硬错误，但返回摘要仍为空；建议回看原始回包结构是否发生漂移。"
    elif has_payload:
        state = "warning"
        state_label = "调用失败"
        operator_hint = f"最近一次调用返回异常：{message}；建议先核对 HTTP 状态、返回码和接口回包。"
    elif message != "-":
        state = "pending"
        state_label = "等待结果"
        operator_hint = f"最近一次还没有结构化回包，可先结合当前提示“{message}”继续判断流程阶段。"
    else:
        state = "empty"
        state_label = "尚未调用"
        operator_hint = "还没有最近调用记录；执行对应动作后，这里会显示结构化结果。"
    freshness = _build_recent_call_freshness(called_at, now=now)
    called_at_display = _format_beijing_time(called_at)
    operator_summary = (
        f"分档={state_label}；时效={freshness['label']}；最近调用={called_at_display}；消息={message}；"
        f"HTTP={http_status if http_status not in (None, '') else '-'}；Code={code if code not in (None, '') else '-'}"
    )
    return {
        "state": state,
        "state_label": state_label,
        "state_tone": _map_recent_call_state_tone(state),
        "operator_hint": f"{operator_hint} 当前时效分档：{freshness['hint']}",
        "operator_summary": operator_summary,
        "called_at_display": called_at_display,
        "message": message,
        "code": str(code) if code not in (None, "") else "-",
        "http_status": str(http_status) if http_status not in (None, "") else "-",
        "freshness_key": freshness["key"],
        "freshness_label": freshness["label"],
        "freshness_tone": freshness["tone"],
    }


def _build_recent_protocol_operator_snapshot(protocol_state: dict[str, Any]) -> dict[str, Any]:
    probe_specs = (
        ("CheckQR", "last_check_qr_result"),
        ("HeartBeat", "last_heartbeat_result"),
        ("HeartBeatLong", "last_heartbeat_long_result"),
        ("CacheInfo", "last_cache_info_result"),
    )
    items: list[dict[str, str]] = []
    failed_labels: list[str] = []
    expired_labels: list[str] = []
    stale_labels: list[str] = []
    missing_labels: list[str] = []
    healthy_labels: list[str] = []
    available_sample_count = 0

    for probe_label, key in probe_specs:
        result = protocol_state.get(key) if isinstance(protocol_state.get(key), dict) else {}
        state = str(result.get("state") or "empty")
        freshness_key = str(result.get("freshness_key") or "no_sample")
        state_label = str(result.get("state_label") or "尚未调用")
        freshness_label = str(result.get("freshness_label") or "尚无时间样本")
        called_at_display = str(result.get("called_at_display") or "-")
        message = str(result.get("message") or "-")

        if state == "warning":
            failed_labels.append(probe_label)
        elif state == "healthy" and freshness_key == "fresh":
            healthy_labels.append(probe_label)

        if state in {"empty", "pending"} or freshness_key == "no_sample":
            missing_labels.append(probe_label)
        else:
            available_sample_count += 1

        if freshness_key == "expired":
            expired_labels.append(probe_label)
        elif freshness_key == "stale":
            stale_labels.append(probe_label)

        items.append(
            {
                "label": probe_label,
                "state_label": state_label,
                "state_tone": str(result.get("state_tone") or _map_recent_call_state_tone(state)),
                "freshness_label": freshness_label,
                "freshness_tone": str(result.get("freshness_tone") or "muted"),
                "summary": f"{state_label} / {freshness_label}",
                "detail": f"最近调用={called_at_display}；消息={message}",
                "operator_summary": f"{probe_label}={state_label}；时效={freshness_label}；最近调用={called_at_display}；消息={message}",
            }
        )

    if failed_labels:
        status_key = "failed"
        status_label = "存在失败"
        status_tone = "outline"
        headline = "最近协议调用：部分失败"
        summary = (
            f"失败探针：{_join_probe_labels(failed_labels)}；"
            f"新鲜探针：{_join_probe_labels(healthy_labels)}；"
            f"下一眼先看失败探针和最近错误卡片。"
        )
        detail = (
            f"失败探针：{'、'.join(failed_labels)}；"
            f"偏旧/过旧：{_join_probe_labels(expired_labels + stale_labels)}；"
            f"样本不足：{_join_probe_labels(missing_labels)}。"
        )
        action = "先查看失败探针的 HTTP 状态、返回码和最近错误上下文，再决定是否补刷其余探针。"
        focus_label = "先看失败探针"
        focus_tone = "outline"
    elif available_sample_count <= 0:
        status_key = "no_sample"
        status_label = "样本不足"
        status_tone = "muted"
        headline = "最近协议调用：样本不足"
        summary = "四类探针都还没有形成可判断的最近样本，当前不能误判为正常。"
        detail = "当前四类探针缺少最近成功/失败样本；建议先触发状态同步或刷新二维码后再看总览。"
        action = "先补最近样本，优先执行状态同步，必要时补刷二维码或重连。"
        focus_label = "先补样本"
        focus_tone = "muted"
    elif expired_labels:
        status_key = "expired"
        status_label = "存在过旧样本"
        status_tone = "outline"
        headline = "最近协议调用：存在过旧样本"
        summary = (
            f"过旧探针：{_join_probe_labels(expired_labels)}；"
            f"新鲜探针：{_join_probe_labels(healthy_labels)}；"
            f"建议先刷新过旧探针，再判断当前在线态。"
        )
        detail = (
            f"过旧探针：{'、'.join(expired_labels)}；"
            f"偏旧探针：{_join_probe_labels(stale_labels)}；"
            f"样本不足：{_join_probe_labels(missing_labels)}。"
        )
        action = "优先刷新过旧探针，确认是否仍在线，再决定是否继续按旧回包排查。"
        focus_label = "优先刷新过旧探针"
        focus_tone = "outline"
    elif stale_labels:
        status_key = "stale"
        status_label = "存在偏旧样本"
        status_tone = "outline"
        headline = "最近协议调用：存在偏旧样本"
        summary = (
            f"偏旧探针：{_join_probe_labels(stale_labels)}；"
            f"新鲜探针：{_join_probe_labels(healthy_labels)}；"
            f"建议尽快刷新偏旧探针，避免把旧状态当成当前状态。"
        )
        detail = f"偏旧探针：{'、'.join(stale_labels)}；样本不足：{_join_probe_labels(missing_labels)}；新鲜探针：{_join_probe_labels(healthy_labels)}。"
        action = "先补刷偏旧探针；如果刷新后仍稳定，再按当前总览继续排查。"
        focus_label = "建议刷新偏旧探针"
        focus_tone = "outline"
    elif missing_labels:
        status_key = "partial_sample"
        status_label = "样本待补齐"
        status_tone = "muted"
        headline = "最近协议调用：样本待补齐"
        summary = f"{_join_probe_labels(healthy_labels)} 已有新鲜样本，但 {_join_probe_labels(missing_labels)} 仍缺少稳定样本。"
        detail = f"样本不足：{'、'.join(missing_labels)}；其余探针当前无失败且结果较新。"
        action = "优先补齐缺样本探针，补齐后再统一判断登录态与缓存态。"
        focus_label = "先补缺样本探针"
        focus_tone = "muted"
    else:
        status_key = "all_fresh"
        status_label = "全部新鲜"
        status_tone = "success"
        headline = "最近协议调用：全部新鲜"
        summary = "四类探针最近结果都较新，当前没有失败或旧样本信号。"
        detail = f"新鲜探针：{_join_probe_labels(healthy_labels)}；可优先结合同步诊断和最近错误卡片继续排查。"
        action = "下一眼先看同步诊断和当前错误态，确认是否存在协议正常但同步链路异常的情况。"
        focus_label = "转看同步与错误"
        focus_tone = "success"

    operator_summary = (
        f"总览={status_label}；失败={_join_probe_labels(failed_labels)}；"
        f"过旧={_join_probe_labels(expired_labels)}；偏旧={_join_probe_labels(stale_labels)}；"
        f"样本不足={_join_probe_labels(missing_labels)}；新鲜={_join_probe_labels(healthy_labels)}；"
        f"下一眼={focus_label}"
    )
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": headline,
        "focus_label": focus_label,
        "focus_tone": focus_tone,
        "summary": summary,
        "detail": detail,
        "action": action,
        "operator_summary": operator_summary,
        "items": items,
    }


def _build_login_operator_snapshot(
    *,
    login_state: str,
    qrcode_status: str,
    qrcode_status_display: str,
    qrcode_is_expired: bool,
    qrcode_countdown_label: str,
    qrcode_expires_at_display: str,
    last_login_at: Any,
    qrcode_refresh_count: Any,
    qrcode_message: Any,
    qrcode_image_available: bool,
    check_qr_result: dict[str, Any],
) -> dict[str, Any]:
    normalized_login_state = str(login_state or "unknown")
    normalized_qrcode_status = str(qrcode_status or "idle")
    normalized_qrcode_display = str(qrcode_status_display or normalized_qrcode_status or "unknown")
    check_qr_state_label = str(check_qr_result.get("state_label") or "尚未调用")
    check_qr_freshness_label = str(check_qr_result.get("freshness_label") or "尚无时间样本")
    check_qr_called_at = str(check_qr_result.get("called_at_display") or "-")
    check_qr_message = str(check_qr_result.get("message") or "-")
    last_login_display = _format_beijing_time(last_login_at)
    refresh_count = int(qrcode_refresh_count or 0)
    qrcode_message_text = str(qrcode_message or "").strip() or "-"
    has_check_qr_sample = str(check_qr_result.get("freshness_key") or "no_sample") != "no_sample"

    if normalized_login_state == "online" or normalized_qrcode_status == "success":
        status_key = "online"
        status_label = "已登录"
        status_tone = "success"
        attention_label = "转看运行态"
        attention_tone = "success"
        summary = "当前登录链路已闭环，二维码阶段可暂时降级，先转看同步、收发和错误态。"
        next_action = "优先核对最近同步诊断、收发摘要和最近错误态，确认在线后链路是否继续推进。"
    elif qrcode_is_expired or normalized_qrcode_display == "expired":
        status_key = "expired"
        status_label = "二维码已过期"
        status_tone = "outline"
        attention_label = "刷新二维码"
        attention_tone = "outline"
        summary = "当前二维码样本已过期，继续盯旧二维码或旧 CheckQR 结果没有意义。"
        next_action = "先执行“刷新二维码”，拿到新码后重新扫码；若连续过期，再检查登录初始化链路。"
    elif normalized_qrcode_status in {"scanned", "confirmed"}:
        status_key = normalized_qrcode_status
        status_label = "等待手机确认" if normalized_qrcode_status == "confirmed" else "已扫码，等待确认"
        status_tone = "outline"
        attention_label = "盯确认结果"
        attention_tone = "outline"
        summary = "二维码链路已进入扫码后阶段，当前不用再先关注二维码图片本身。"
        next_action = "先看最近 CheckQR 是否继续刷新；若长时间停在该阶段，补一次状态同步或刷新二维码。"
    elif normalized_qrcode_status == "waiting_scan":
        status_key = "waiting_scan"
        status_label = "等待扫码"
        status_tone = "outline"
        attention_label = "先看二维码"
        attention_tone = "outline"
        summary = "当前已有可用二维码链路，下一眼先判断二维码是否还有效、CheckQR 是否持续更新。"
        next_action = "先扫码；若运维侧已扫码但页面仍不推进，马上核对最近 CheckQR 时间和返回消息。"
    elif qrcode_image_available or qrcode_message_text != "-" or has_check_qr_sample:
        status_key = "pending_result"
        status_label = "等待链路回包"
        status_tone = "muted"
        attention_label = "补状态样本"
        attention_tone = "muted"
        summary = "当前已出现部分登录链路信号，但还不足以直接判定已登录或二维码可继续使用。"
        next_action = "优先执行状态同步或等待下一次 CheckQR 结果，再判断是否需要刷新二维码。"
    else:
        status_key = "no_sample"
        status_label = "尚未形成样本"
        status_tone = "muted"
        attention_label = "初始化登录"
        attention_tone = "muted"
        summary = "当前还没有二维码或最近 CheckQR 样本，不能直接判断登录链路已开始。"
        next_action = "先执行“初始化登录”，拿到二维码和首个 CheckQR 样本后再判断当前阶段。"

    detail_items = [
        {
            "label": "登录态",
            "value": normalized_login_state,
            "tone": "success" if status_key == "online" else "muted" if normalized_login_state in {"unknown", "offline"} else "outline",
        },
        {"label": "二维码状态", "value": normalized_qrcode_display, "tone": status_tone},
        {"label": "是否过期", "value": "已过期" if qrcode_is_expired else "有效中", "tone": "outline" if qrcode_is_expired else "success"},
        {"label": "CheckQR", "value": f"{check_qr_state_label} / {check_qr_freshness_label}", "tone": str(check_qr_result.get('state_tone') or 'muted')},
    ]
    detail = (
        f"最近登录={last_login_display}；最近 CheckQR={check_qr_called_at}；"
        f"二维码过期时间={qrcode_expires_at_display}；剩余有效期={qrcode_countdown_label or '-'}；"
        f"二维码刷新次数={refresh_count}；CheckQR 消息={check_qr_message}"
    )
    if qrcode_message_text != "-":
        detail = f"{detail}；当前提示={qrcode_message_text}"
    operator_summary = (
        f"分类={status_label}；登录态={normalized_login_state}；二维码={normalized_qrcode_display}；"
        f"是否过期={'是' if qrcode_is_expired else '否'}；最近登录={last_login_display}；"
        f"最近CheckQR={check_qr_called_at}；CheckQR={check_qr_state_label}/{check_qr_freshness_label}；下一步={next_action}"
    )
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "attention_label": attention_label,
        "attention_tone": attention_tone,
        "headline": f"登录 / 二维码一眼判断：{status_label}",
        "summary": summary,
        "detail": detail,
        "next_action": next_action,
        "operator_summary": operator_summary,
        "detail_items": detail_items,
    }


def _build_current_ops_snapshot(
    *,
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
    group_plugin_governance_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error_freshness_key = str((error_context or {}).get("freshness_key") or "no_sample")
    error_attention_label = str(error_operator_snapshot.get("attention_label") or "")
    login_status_key = str(login_operator_snapshot.get("status_key") or "unknown")
    protocol_status_key = str(protocol_operator_snapshot.get("status_key") or "unknown")
    sync_status_key = str(sync_operator_snapshot.get("status_key") or "unknown")
    message_focus_label = str(message_operator_snapshot.get("focus_label") or "")
    group_status_key = str((group_stage_snapshot or {}).get("status_key") or "unknown")
    group_freshness_key = str((group_stage_snapshot or {}).get("freshness_key") or "no_sample")

    items = [
        _build_current_ops_item("错误", error_operator_snapshot.get("status_label"), error_operator_snapshot.get("status_tone"), error_operator_snapshot.get("attention_label")),
        _build_current_ops_item("登录", login_operator_snapshot.get("status_label"), login_operator_snapshot.get("status_tone"), login_operator_snapshot.get("attention_label")),
        _build_current_ops_item("协议", protocol_operator_snapshot.get("status_label"), protocol_operator_snapshot.get("status_tone"), protocol_operator_snapshot.get("focus_label")),
        _build_current_ops_item("同步", sync_operator_snapshot.get("status_label"), sync_operator_snapshot.get("status_tone"), None),
        _build_current_ops_item("收发", message_operator_snapshot.get("status_label"), message_operator_snapshot.get("status_tone"), message_operator_snapshot.get("focus_label")),
        _build_current_ops_item("群资料", (group_stage_snapshot or {}).get("status_label") or "未纳入本次判断", (group_stage_snapshot or {}).get("status_tone") or "muted", None),
        _build_current_ops_item("群级治理", (group_plugin_governance_snapshot or {}).get("status_label") or "未纳入本次判断", (group_plugin_governance_snapshot or {}).get("status_tone") or "muted", (group_plugin_governance_snapshot or {}).get("focus_label")),
    ]

    if error_attention_label == "优先处理" and error_freshness_key in {"fresh", "stale"}:
        status_key = "focus_error"
        status_label = "先看错误"
        status_tone = "outline"
        focus_label = "错误优先"
        focus_tone = "outline"
        summary = "当前总体状态：存在需要优先处理的最近错误，先不要把旧探针或旧同步样本误判成正常。"
        evidence = str(error_operator_snapshot.get("detail") or error_operator_snapshot.get("summary") or "最近错误样本提示需要优先处理。")
        action = str(error_operator_snapshot.get("attention_action") or "先核对最近错误接口、阶段和最新回包。")
    elif login_status_key in {"expired", "no_sample", "pending_result"}:
        status_key = "focus_login"
        status_label = "先看登录 / 二维码"
        status_tone = "outline" if login_status_key == "expired" else "muted"
        focus_label = str(login_operator_snapshot.get("attention_label") or "先看登录 / 二维码")
        focus_tone = str(login_operator_snapshot.get("attention_tone") or status_tone)
        summary = "当前总体状态：登录 / 二维码链路还没稳定闭环，其他摘要只能作为旁证，不能先判整体正常。"
        evidence = str(login_operator_snapshot.get("detail") or login_operator_snapshot.get("summary") or "登录 / 二维码链路仍待确认。")
        action = str(login_operator_snapshot.get("next_action") or "先补登录 / 二维码样本。")
    elif protocol_status_key in {"failed", "expired", "stale", "no_sample", "partial_sample"}:
        status_key = "focus_protocol"
        status_label = "先刷新协议探针"
        status_tone = "outline" if protocol_status_key in {"failed", "expired", "stale"} else "muted"
        focus_label = str(protocol_operator_snapshot.get("focus_label") or "先刷新协议探针")
        focus_tone = str(protocol_operator_snapshot.get("focus_tone") or status_tone)
        summary = "当前总体状态：协议探针还不足以稳定说明在线态，先补失败、过旧或缺样本探针。"
        evidence = str(protocol_operator_snapshot.get("detail") or protocol_operator_snapshot.get("summary") or "最近协议探针样本不足。")
        action = str(protocol_operator_snapshot.get("action") or "先刷新协议探针。")
    elif sync_status_key in {"no_sample", "recent_error", "pending_continue", "success_no_new"}:
        status_key = "focus_sync"
        status_label = "先看同步 / 补样本"
        status_tone = "outline" if sync_status_key in {"recent_error", "pending_continue"} else "muted"
        focus_label = "同步优先"
        focus_tone = status_tone
        summary = "当前总体状态：同步链路还没有完全收口，先确认是否异常、待续拉取或仍缺首批同步样本。"
        evidence = str(sync_operator_snapshot.get("detail") or sync_operator_snapshot.get("summary") or "最近同步摘要仍提示需要继续判断。")
        action = str(sync_operator_snapshot.get("action") or sync_operator_snapshot.get("operator_summary") or "先补同步样本。")
    elif message_focus_label != "持续观察":
        status_key = "focus_message"
        status_label = "先补收发样本"
        status_tone = "outline" if "刷新" in message_focus_label else "muted"
        focus_label = message_focus_label or "先补收发样本"
        focus_tone = str(message_operator_snapshot.get("focus_tone") or status_tone)
        summary = "当前总体状态：最近收发链路样本还不够完整或不够新鲜，先补单侧样本再判断整体运行。"
        evidence = str(message_operator_snapshot.get("evidence") or message_operator_snapshot.get("summary") or "最近收发样本不足。")
        action = str(message_operator_snapshot.get("action") or "先补一条最近收发样本。")
    elif group_stage_snapshot and (group_status_key != "member_records_available" or group_freshness_key in {"stale", "expired", "no_sample"}):
        status_key = "focus_group_sync"
        status_label = "先补群资料同步"
        status_tone = "outline" if group_status_key != "no_sample" else "muted"
        focus_label = "群资料待补"
        focus_tone = status_tone
        summary = "当前总体状态：基础运行样本大致正常，但群资料同步档位仍偏浅或偏旧，先补群资料/成员样本。"
        evidence = str(group_stage_snapshot.get("operator_summary") or group_stage_snapshot.get("evidence") or "群资料同步档位仍待提升。")
        action = str(group_stage_snapshot.get("action") or "先补群资料同步样本。")
    else:
        status_key = "healthy"
        status_label = "当前整体正常"
        status_tone = "success"
        focus_label = "继续观察"
        focus_tone = "success"
        summary = "当前总体状态：登录、协议、同步、收发都没有明显异常信号，可继续按常规观察。"
        evidence = (
            f"登录={login_operator_snapshot.get('status_label') or '-'}；"
            f"协议={protocol_operator_snapshot.get('status_label') or '-'}；"
            f"同步={sync_operator_snapshot.get('status_label') or '-'}；"
            f"收发={message_operator_snapshot.get('status_label') or '-'}。"
        )
        if group_stage_snapshot:
            evidence = f"{evidence}群资料={group_stage_snapshot.get('status_label') or '-'}。"
        action = "当前无需切换处理焦点，继续观察最近错误、同步和探针是否出现新变化。"

    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": status_tone,
        "headline": f"当前运维一眼判断：{status_label}",
        "focus_label": focus_label,
        "focus_tone": focus_tone,
        "summary": summary,
        "evidence": evidence,
        "action": action,
        "items": items,
        "operator_summary": f"当前总体状态={status_label}；关键证据={evidence}；下一步={action}",
    }


def _build_top_priority_snapshot(
    *,
    current_ops_snapshot: dict[str, Any],
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
    group_plugin_governance_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_key = str(current_ops_snapshot.get("status_key") or "healthy")
    status_label = str(current_ops_snapshot.get("status_label") or "当前整体正常")
    status_tone = str(current_ops_snapshot.get("status_tone") or "muted")

    if status_key == "focus_error":
        module_label = "最近错误"
        tone = str(error_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(error_operator_snapshot.get("status_label") or "先看错误")
        evidence = str(error_operator_snapshot.get("detail") or error_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(error_operator_snapshot.get("attention_action") or current_ops_snapshot.get("action") or "先核对最近错误。")
        summary = f"当前优先处理最近错误，先核对最近失败阶段与回包，再决定是否刷新其他卡片。"
    elif status_key == "focus_login":
        module_label = "登录 / 二维码"
        tone = str(login_operator_snapshot.get("attention_tone") or login_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(login_operator_snapshot.get("attention_label") or login_operator_snapshot.get("status_label") or "先看登录 / 二维码")
        evidence = str(login_operator_snapshot.get("detail") or login_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(login_operator_snapshot.get("next_action") or current_ops_snapshot.get("action") or "先补登录 / 二维码样本。")
        summary = "当前优先处理登录 / 二维码链路，在登录未稳定前不把其他摘要视为已确认正常。"
    elif status_key == "focus_protocol":
        module_label = "协议探针"
        tone = str(protocol_operator_snapshot.get("focus_tone") or protocol_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(protocol_operator_snapshot.get("focus_label") or protocol_operator_snapshot.get("status_label") or "先刷新协议探针")
        evidence = str(protocol_operator_snapshot.get("detail") or protocol_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(protocol_operator_snapshot.get("action") or current_ops_snapshot.get("action") or "先刷新协议探针。")
        summary = "当前优先补协议探针，先确认失败、过旧或缺样本探针，再判断在线态。"
    elif status_key == "focus_sync":
        module_label = "最近同步"
        tone = str(sync_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(sync_operator_snapshot.get("status_label") or "同步优先")
        evidence = str(sync_operator_snapshot.get("detail") or sync_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(sync_operator_snapshot.get("action") or current_ops_snapshot.get("action") or "先补同步样本。")
        summary = "当前优先处理最近同步，先确认是否存在异常、待续拉取或仍缺首批同步样本。"
    elif status_key == "focus_message":
        module_label = "最近收发"
        tone = str(message_operator_snapshot.get("focus_tone") or message_operator_snapshot.get("status_tone") or status_tone)
        issue_label = str(message_operator_snapshot.get("focus_label") or message_operator_snapshot.get("status_label") or "先补收发样本")
        evidence = str(message_operator_snapshot.get("evidence") or message_operator_snapshot.get("summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str(message_operator_snapshot.get("action") or current_ops_snapshot.get("action") or "先补最近收发样本。")
        summary = "当前优先补最近收发样本，避免单侧缺口把链路误判成整体正常。"
    elif status_key == "focus_group_sync":
        module_label = "群资料同步"
        tone = str((group_stage_snapshot or {}).get("status_tone") or status_tone)
        issue_label = str((group_stage_snapshot or {}).get("status_label") or "群资料待补")
        evidence = str((group_stage_snapshot or {}).get("operator_summary") or current_ops_snapshot.get("evidence") or "")
        next_action = str((group_stage_snapshot or {}).get("action") or current_ops_snapshot.get("action") or "先补群资料同步样本。")
        summary = "当前高优先异常不明显，优先补群资料同步深度与时效，再评估治理准备度。"
    else:
        governance_status = str((group_plugin_governance_snapshot or {}).get("status_label") or "")
        governance_focus = str((group_plugin_governance_snapshot or {}).get("focus_label") or "")
        module_label = "持续观察"
        tone = "success"
        issue_label = "无高优先异常"
        evidence = str(current_ops_snapshot.get("evidence") or "登录、协议、同步、收发当前没有明显高优先异常。")
        if governance_status and governance_status not in {"可进入群级权限建模", "未纳入判断"}:
            next_action = "当前无高优先异常，优先继续补真实样本并观察下一次同步。"
            summary = f"当前无高优先异常，可继续补真实样本并观察下一次同步；群级治理当前主要是{governance_focus or governance_status}。"
        else:
            next_action = "当前无高优先异常，优先继续补真实样本并观察下一次同步。"
            summary = "当前无高优先异常，优先继续补真实样本并观察下一次同步。"

    basis_parts = _build_top_priority_basis_parts(
        status_key=status_key,
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_operator_snapshot,
        sync_operator_snapshot=sync_operator_snapshot,
        message_operator_snapshot=message_operator_snapshot,
        error_operator_snapshot=error_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=group_stage_snapshot,
        group_plugin_governance_snapshot=group_plugin_governance_snapshot,
    )
    basis_summary = " / ".join(basis_parts)
    confidence_snapshot = _build_top_priority_confidence_snapshot(
        status_key=status_key,
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_operator_snapshot,
        sync_operator_snapshot=sync_operator_snapshot,
        message_operator_snapshot=message_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=group_stage_snapshot,
    )
    summary_line = f"当前首要处理项：{module_label}；{summary}"
    return {
        "status_key": status_key,
        "status_label": status_label,
        "status_tone": tone,
        "module_label": module_label,
        "issue_label": issue_label,
        "headline": summary_line,
        "summary": summary,
        "summary_line": summary_line,
        "evidence": evidence,
        "basis_summary": basis_summary,
        "basis_parts": basis_parts,
        "confidence_key": confidence_snapshot["key"],
        "confidence_label": confidence_snapshot["label"],
        "confidence_tone": confidence_snapshot["tone"],
        "confidence_summary": confidence_snapshot["summary"],
        "next_action": next_action,
        "operator_summary": (
            f"当前优先项={module_label}；"
            f"判断={issue_label}；"
            f"当前把握={confidence_snapshot['label']}；"
            f"口径={confidence_snapshot['summary']}；"
            f"判定依据={basis_summary}；"
            f"关键证据={evidence}；"
            f"下一步={next_action}"
        ),
    }


def _build_top_priority_confidence_snapshot(
    *,
    status_key: str,
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
) -> dict[str, str]:
    if status_key == "focus_error":
        freshness_key = str(error_context.get("freshness_key") or "no_sample")
        if freshness_key in {"fresh", "stale"}:
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "当前故障信号明确，建议优先处理。",
            }
        return {
            "key": "refresh_then_confirm",
            "label": "历史旧样本 / 先刷新",
            "tone": "muted",
            "summary": "最近错误样本偏旧，先刷新后再判断是否仍是当前故障。",
        }

    if status_key == "focus_login":
        login_status_key = str(login_operator_snapshot.get("status_key") or "unknown")
        if login_status_key == "expired":
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "登录异常信号明确，建议优先处理当前登录链路。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "登录链路样本还不完整，先补样本再判断是否为当前故障。",
        }

    if status_key == "focus_protocol":
        protocol_status_key = str(protocol_operator_snapshot.get("status_key") or "unknown")
        if protocol_status_key == "failed":
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "协议失败信号明确，建议优先处理当前故障。",
            }
        if protocol_status_key in {"expired", "stale"}:
            return {
                "key": "refresh_then_confirm",
                "label": "历史旧样本 / 先刷新",
                "tone": "muted",
                "summary": "协议样本偏旧，先刷新探针再判断当前状态。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "协议样本仍不完整，先补样本再判断。",
        }

    if status_key == "focus_sync":
        sync_status_key = str(sync_operator_snapshot.get("status_key") or "unknown")
        if sync_status_key == "recent_error":
            return {
                "key": "high_current_issue",
                "label": "高置信度当前故障",
                "tone": "outline",
                "summary": "最近同步异常较明确，建议优先处理当前同步故障。",
            }
        if sync_status_key == "pending_continue":
            return {
                "key": "observe_in_progress",
                "label": "继续观察",
                "tone": "muted",
                "summary": "当前轮次仍在继续，先完成本轮拉取再判断是否异常。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "同步样本仍不足，先补样本再判断。",
        }

    if status_key == "focus_message":
        focus_label = str(message_operator_snapshot.get("focus_label") or "")
        if "刷新" in focus_label or "过旧" in str(message_operator_snapshot.get("focus_side_label") or ""):
            return {
                "key": "refresh_then_confirm",
                "label": "历史旧样本 / 先刷新",
                "tone": "muted",
                "summary": "最近收发样本偏旧，先刷新后再判断当前链路。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "最近收发样本不完整，先补样本再判断。",
        }

    if status_key == "focus_group_sync":
        freshness_key = str((group_stage_snapshot or {}).get("freshness_key") or "no_sample")
        if freshness_key in {"stale", "expired"}:
            return {
                "key": "refresh_then_confirm",
                "label": "历史旧样本 / 先刷新",
                "tone": "muted",
                "summary": "群资料样本偏旧，先刷新后再判断。",
            }
        return {
            "key": "sample_gap_pending",
            "label": "缺样本待确认",
            "tone": "muted",
            "summary": "群资料样本仍不足，先补样本再判断。",
        }

    return {
        "key": "observe_in_progress",
        "label": "继续观察",
        "tone": "success",
        "summary": "当前无高优先异常，以继续观察为主。",
    }


def _build_top_priority_basis_parts(
    *,
    status_key: str,
    login_operator_snapshot: dict[str, Any],
    protocol_operator_snapshot: dict[str, Any],
    sync_operator_snapshot: dict[str, Any],
    message_operator_snapshot: dict[str, Any],
    error_operator_snapshot: dict[str, Any],
    error_context: dict[str, Any],
    group_stage_snapshot: dict[str, Any] | None = None,
    group_plugin_governance_snapshot: dict[str, Any] | None = None,
) -> list[str]:
    if status_key == "focus_error":
        stage_label = _format_error_stage_label(error_context.get("stage"))
        freshness_label = str(error_context.get("freshness_label") or "尚无时间样本")
        return _compact_priority_basis_parts(
            [
                f"最近错误时效={freshness_label}，当前按{error_operator_snapshot.get('attention_label') or '优先处理'}看待",
                f"错误分类偏向{error_operator_snapshot.get('status_label') or '待归类异常'}",
                f"{stage_label}阶段已出现失败样本" if stage_label != "-" else "",
            ]
        )
    if status_key == "focus_login":
        login_status_key = str(login_operator_snapshot.get("status_key") or "unknown")
        login_basis_map = {
            "expired": "二维码样本已过期，旧登录信号不再可靠",
            "no_sample": "登录 / CheckQR 仍缺首批样本",
            "pending_result": "登录链路只有局部信号，尚未形成稳定回包",
            "waiting_scan": "二维码可见但当前仍停留在等待扫码阶段",
            "scanned": "已扫码但还在等待确认结果继续推进",
            "confirmed": "已扫码但还在等待确认结果继续推进",
        }
        return _compact_priority_basis_parts(
            [
                login_basis_map.get(login_status_key, "登录 / 二维码链路仍未形成稳定闭环"),
                f"当前关注点仍是{login_operator_snapshot.get('attention_label') or login_operator_snapshot.get('status_label') or '登录 / 二维码'}",
            ]
        )
    if status_key == "focus_protocol":
        protocol_status_key = str(protocol_operator_snapshot.get("status_key") or "unknown")
        protocol_basis_map = {
            "failed": "最近协议探针里已有失败样本",
            "expired": "协议探针样本已过旧，不能直接当作当前在线态",
            "stale": "协议探针样本偏旧，当前在线态仍待刷新确认",
            "no_sample": "四类协议探针仍缺最近样本",
            "partial_sample": "协议探针只有部分样本，结论还不完整",
        }
        return _compact_priority_basis_parts(
            [
                protocol_basis_map.get(protocol_status_key, "协议探针当前仍待判断"),
                f"当前更先看{protocol_operator_snapshot.get('focus_label') or protocol_operator_snapshot.get('status_label') or '协议探针'}",
            ]
        )
    if status_key == "focus_sync":
        sync_status_key = str(sync_operator_snapshot.get("status_key") or "unknown")
        sync_basis_map = {
            "recent_error": "最近同步样本显示异常，优先级高于补旧样本",
            "pending_continue": "continue_flag 仍提示这一轮还没拉到末尾",
            "no_sample": "最近同步还没形成首批可判断样本",
            "success_no_new": "当前没有高优先故障，更像同步空拉取或继续观察场景",
        }
        return _compact_priority_basis_parts(
            [
                sync_basis_map.get(sync_status_key, "最近同步链路仍待继续判断"),
                f"当前同步档位={sync_operator_snapshot.get('status_label') or '待判断'}",
            ]
        )
    if status_key == "focus_message":
        return _compact_priority_basis_parts(
            [
                f"最近收发的主要拖后腿侧是{message_operator_snapshot.get('focus_side_label') or message_operator_snapshot.get('focus_label') or '待判断'}",
                f"当前关注点={message_operator_snapshot.get('focus_label') or '先补收发样本'}",
                f"整体收发判断={message_operator_snapshot.get('status_label') or '待判断'}",
            ]
        )
    if status_key == "focus_group_sync":
        freshness_label = str((group_stage_snapshot or {}).get("freshness_label") or "尚无时间样本")
        return _compact_priority_basis_parts(
            [
                f"当前高优先异常不明显，主要剩群资料档位={((group_stage_snapshot or {}).get('status_label') or '待判断')}",
                f"群资料同步时效={freshness_label}",
            ]
        )

    governance_status = str((group_plugin_governance_snapshot or {}).get("status_label") or "")
    governance_focus = str((group_plugin_governance_snapshot or {}).get("focus_label") or "")
    parts = [
        "登录、协议、同步、收发当前都未见更高优先异常",
        "当前更适合继续补真实样本并观察下一次同步",
    ]
    if governance_status and governance_status not in {"可进入群级权限建模", "未纳入判断"}:
        parts.append(f"群级治理当前更像{governance_focus or governance_status}，先不按已确认故障处理")
    return _compact_priority_basis_parts(parts)


def _compact_priority_basis_parts(parts: list[str]) -> list[str]:
    compacted: list[str] = []
    for part in parts:
        text = str(part or "").strip().rstrip("。")
        if text:
            compacted.append(text)
        if len(compacted) >= 3:
            break
    return compacted


def _build_current_ops_item(label: str, state_label: Any, tone: Any, hint: Any) -> dict[str, str]:
    normalized_state = str(state_label or "-")
    normalized_hint = str(hint or "").strip()
    summary = normalized_state if not normalized_hint else f"{normalized_state} / {normalized_hint}"
    return {
        "label": label,
        "state_label": normalized_state,
        "tone": str(tone or "muted"),
        "summary": summary,
    }


def _build_cache_info_view(value: Any, summary: Any, *, user_profile: Any = None, called_at: Any = None, now: datetime | None = None) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    data = WechatIpadApiClient.extract_data(payload)
    cached = summary if isinstance(summary, dict) else {}
    user_profile_summary = user_profile if isinstance(user_profile, dict) else {}
    meta = _build_recent_call_view(payload, called_at=called_at, now=now)
    wxid = str(cached.get("wxid") or data.get("Wxid") or data.get("UserName") or user_profile_summary.get("wxid") or "-")
    nickname = str(cached.get("nickname") or data.get("NickName") or data.get("nickname") or user_profile_summary.get("nickname") or "-")
    device_id = str(cached.get("device_id") or data.get("DeviceID") or "-")
    has_account_summary = any(item != "-" for item in (wxid, nickname, device_id))
    if meta.get("state") == "blank" and has_account_summary:
        meta["state"] = "healthy"
        meta["state_label"] = "调用成功"
        meta["state_tone"] = _map_recent_call_state_tone("healthy")
        meta["operator_hint"] = "最近一次 CacheInfo 已返回账号缓存摘要，可直接核对 wxid、昵称和设备信息。"
    elif meta.get("state") == "healthy" and not has_account_summary:
        meta["state"] = "blank"
        meta["state_label"] = "返回空摘要"
        meta["state_tone"] = _map_recent_call_state_tone("blank")
        meta["operator_hint"] = "最近一次 CacheInfo 没有硬错误，但账号缓存摘要为空；建议关注缓存字段是否缺失或接口回包漂移。"
    meta["operator_summary"] = (
        f"分档={meta.get('state_label', '尚未调用')}；时效={meta.get('freshness_label', '尚无时间样本')}；"
        f"最近调用={meta.get('called_at_display', '-')}；消息={meta.get('message', '-')}；"
        f"wxid={wxid}；昵称={nickname}；device_id={device_id}"
    )
    return {
        "state": meta.get("state", "empty"),
        "state_label": meta.get("state_label", "尚未调用"),
        "state_tone": meta.get("state_tone", _map_recent_call_state_tone("empty")),
        "operator_hint": meta.get("operator_hint", "还没有最近调用记录；执行对应动作后，这里会显示结构化结果。"),
        "operator_summary": meta.get("operator_summary", "-"),
        "called_at_display": meta.get("called_at_display", "-"),
        "message": meta.get("message", "-"),
        "code": meta.get("code", "-"),
        "http_status": meta.get("http_status", "-"),
        "wxid": wxid,
        "nickname": nickname,
        "device_id": device_id,
        "freshness_key": meta.get("freshness_key", "no_sample"),
        "freshness_label": meta.get("freshness_label", "尚无时间样本"),
        "freshness_tone": meta.get("freshness_tone", "muted"),
        "payload_json": meta.get("payload_json", "{}"),
        "has_payload": meta.get("has_payload", "false"),
    }


def _extract_user_profile_summary(profile_data: dict[str, Any], profile_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "wxid": profile_fields.get("wxid") or profile_data.get("Wxid") or profile_data.get("UserName"),
        "nickname": profile_fields.get("nickname") or profile_data.get("NickName") or profile_data.get("nickname"),
        "alias": profile_fields.get("alias") or profile_data.get("Alias") or profile_data.get("alias"),
        "bind_mobile": profile_fields.get("bind_mobile") or profile_data.get("BindMobile") or profile_data.get("bind_mobile"),
    }


def _build_user_profile_view(value: Any, summary: Any, *, called_at: Any = None, now: datetime | None = None) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    data = WechatIpadApiClient.extract_data(payload)
    profile_fields = WechatIpadApiClient.extract_account_fields(payload) if payload else {}
    cached = summary if isinstance(summary, dict) else {}
    meta = _build_recent_call_view(payload, called_at=called_at, now=now)
    wxid = str(cached.get("wxid") or profile_fields.get("wxid") or data.get("Wxid") or data.get("UserName") or "-")
    nickname = str(cached.get("nickname") or profile_fields.get("nickname") or data.get("NickName") or data.get("nickname") or "-")
    alias = str(cached.get("alias") or profile_fields.get("alias") or data.get("Alias") or data.get("alias") or "-")
    bind_mobile = str(cached.get("bind_mobile") or profile_fields.get("bind_mobile") or data.get("BindMobile") or data.get("bind_mobile") or "-")
    meta["operator_summary"] = (
        f"分档={meta.get('state_label', '尚未调用')}；时效={meta.get('freshness_label', '尚无时间样本')}；"
        f"最近调用={meta.get('called_at_display', '-')}；消息={meta.get('message', '-')}；"
        f"wxid={wxid}；昵称={nickname}；微信号={alias}；手机号={bind_mobile}"
    )
    return {
        "state": meta.get("state", "empty"),
        "state_label": meta.get("state_label", "尚未调用"),
        "state_tone": meta.get("state_tone", _map_recent_call_state_tone("empty")),
        "operator_hint": meta.get("operator_hint", "还没有最近调用记录；执行对应动作后，这里会显示结构化结果。"),
        "operator_summary": meta.get("operator_summary", "-"),
        "called_at_display": meta.get("called_at_display", "-"),
        "message": meta.get("message", "-"),
        "code": meta.get("code", "-"),
        "http_status": meta.get("http_status", "-"),
        "wxid": wxid,
        "nickname": nickname,
        "alias": alias,
        "bind_mobile": bind_mobile,
        "freshness_key": meta.get("freshness_key", "no_sample"),
        "freshness_label": meta.get("freshness_label", "尚无时间样本"),
        "freshness_tone": meta.get("freshness_tone", "muted"),
        "payload_json": meta.get("payload_json", "{}"),
        "has_payload": meta.get("has_payload", "false"),
    }


def _format_json_preview(value: Any) -> str:
    if value in (None, "", [], {}):
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _map_recent_call_state_tone(state: str) -> str:
    if state == "healthy":
        return "success"
    if state in {"pending", "empty"}:
        return "muted"
    return "outline"


def _join_probe_labels(labels: list[str]) -> str:
    return "、".join(labels) if labels else "-"


def _build_recent_call_freshness(called_at: Any, *, now: datetime | None = None) -> dict[str, str]:
    parsed_called_at = _parse_datetime_like(called_at)
    if not parsed_called_at:
        return {
            "key": "no_sample",
            "label": "尚无时间样本",
            "tone": "muted",
            "hint": "尚无时间样本，当前无法判断最近结果是否过旧。",
        }

    reference_time = now or now_shanghai()
    if reference_time.tzinfo is None:
        reference_time = ensure_shanghai_datetime(reference_time)
    age_seconds = max(int((reference_time - parsed_called_at).total_seconds()), 0)

    if age_seconds <= RECENT_CALL_FRESH_SECONDS:
        return {
            "key": "fresh",
            "label": "新鲜",
            "tone": "success",
            "hint": "结果较新鲜，可直接按当前样本继续排查。",
        }
    if age_seconds <= RECENT_CALL_STALE_SECONDS:
        return {
            "key": "stale",
            "label": "偏旧",
            "tone": "outline",
            "hint": "结果已偏旧，建议结合最近动作确认探针是否还在持续刷新。",
        }
    return {
        "key": "expired",
        "label": "过旧",
        "tone": "outline",
        "hint": "结果已过旧，建议优先重新触发探针，避免继续参考陈旧样本。",
    }


def _build_status_summary(
    qr_result: dict[str, Any] | None,
    heartbeat: dict[str, Any],
    user_profile: dict[str, Any],
) -> str:
    parts = []
    if qr_result:
        parts.append(_build_qr_status_summary(qr_result))
    parts.append("HeartBeat 成功" if heartbeat["ok"] else f"HeartBeat 失败：{heartbeat['error']}")
    if user_profile["ok"]:
        profile_data = user_profile["data"] if isinstance(user_profile.get("data"), dict) else {}
        profile_fields = WechatIpadApiClient.extract_account_fields(user_profile.get("payload") or {})
        nickname = profile_fields.get("nickname") or profile_data.get("NickName") or profile_data.get("nickname")
        parts.append(f"GetContractProfile 成功{f'，昵称 {nickname}' if nickname else ''}")
    elif user_profile["payload"]:
        parts.append(f"GetContractProfile 失败：{user_profile['error']}")
    return "；".join(parts)


def _derive_login_state_from_qr(qr_result: dict[str, Any] | None) -> str | None:
    if not qr_result:
        return None
    status_code = _extract_qr_status_code(qr_result)
    message = qr_result.get("message") or ""
    if status_code in {2, 3}:
        return "online"
    if status_code == 1:
        return "qrcode_scanned"
    if status_code in {-1, 4}:
        return "qrcode_expired"
    if qr_result["ok"] and message in {"登陆成功", "登录成功"}:
        return "online"
    if "扫码" in message:
        return "qrcode_scanned"
    if "确认" in message:
        return "qrcode_confirmed"
    if "过期" in message:
        return "qrcode_expired"
    return "qrcode_ready" if qr_result["ok"] else "offline"


def _derive_qrcode_status(qr_result: dict[str, Any] | None) -> str | None:
    if not qr_result:
        return None
    status_code = _extract_qr_status_code(qr_result)
    message = qr_result.get("message") or ""
    if status_code in {2, 3}:
        return "success"
    if status_code == 1:
        return "scanned"
    if status_code in {-1, 4}:
        return "expired"
    if qr_result["ok"] and message in {"登陆成功", "登录成功"}:
        return "success"
    if "扫码" in message:
        return "scanned"
    if "确认" in message:
        return "confirmed"
    if "过期" in message:
        return "expired"
    return "waiting_scan" if qr_result["ok"] else "failed"


def _derive_display_qrcode_status(qrcode_status: str, is_expired: bool) -> str:
    if is_expired and qrcode_status not in {"success", "expired"}:
        return "已过期"
    labels = {
        "idle": "未初始化",
        "unknown": "未知",
        "waiting_scan": "等待扫码",
        "scanned": "已扫码，等待确认",
        "confirmed": "已确认，等待登录完成",
        "success": "登录成功",
        "expired": "已过期",
        "failed": "失败",
    }
    normalized = str(qrcode_status or "").strip()
    return labels.get(normalized, normalized or "-")


def _derive_display_login_state(login_state: str) -> str:
    labels = {
        "unknown": "未知",
        "idle": "未开始",
        "offline": "离线",
        "qrcode_ready": "二维码已就绪",
        "qrcode_scanned": "已扫码",
        "qrcode_confirmed": "已确认",
        "qrcode_expired": "二维码已过期",
        "online": "在线",
        "error": "异常",
        "reconnecting": "重连中",
        "reconnect_failed": "重连失败",
        "reconnect_blocked": "重连受阻",
    }
    normalized = str(login_state or "").strip()
    return labels.get(normalized, normalized or "-")


def _qr_login_succeeded(qr_result: dict[str, Any] | None) -> bool:
    if not qr_result or not qr_result.get("ok"):
        return False
    if _extract_qr_status_code(qr_result) in {2, 3}:
        return True
    message = str(qr_result.get("message") or "").strip()
    return message in {"登陆成功", "登录成功"}


def _extract_qr_status_code(qr_result: dict[str, Any] | None) -> int | None:
    if not qr_result:
        return None
    payload = qr_result.get("payload")
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("status")]
    data_payload = payload.get("Data")
    if isinstance(data_payload, dict):
        candidates.extend([data_payload.get("status"), data_payload.get("Status")])
    lower_data_payload = payload.get("data")
    if isinstance(lower_data_payload, dict):
        candidates.extend([lower_data_payload.get("status"), lower_data_payload.get("Status")])
    for value in candidates:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_polling_active(*, login_state: str, qrcode_status: str, is_expired: bool) -> bool:
    if is_expired:
        return False
    if login_state == "online" or qrcode_status == "success":
        return False
    return qrcode_status in {"waiting_scan", "scanned", "confirmed"}


def _resolve_wxid(channel: Channel, runtime_info: dict[str, Any], adapter_config: dict[str, Any]) -> str:
    return str(runtime_info.get("active_wxid") or adapter_config.get("wxid") or "").strip()


def resolve_wechat_ipad_qrcode_image(channel: Channel) -> dict[str, Any]:
    """返回当前二维码短链接，图片内容不再从渠道运行状态中解码。"""

    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    resolved = _resolve_qrcode_image(runtime_info)
    if not resolved:
        return {"available": False}
    return {"available": True, "kind": "url", "url": resolved["url"]}


def _extract_contact_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, list[Any]]] = []
    for key, value in data.items():
        if isinstance(value, list):
            score = 1 if any(token in str(key).lower() for token in ("contact", "friend", "wxcontact", "wx_friend")) else 0
            candidates.append((score, len(value), value))
            continue
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if not isinstance(nested_value, list):
                    continue
                compound = f"{key}.{nested_key}".lower()
                score = 1 if any(token in compound for token in ("contact", "friend", "wxcontact", "wx_friend")) else 0
                candidates.append((score, len(nested_value), nested_value))
    if not candidates:
        return []
    selected = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[0][2]
    return [item for item in selected if isinstance(item, dict)]


def _extract_contact_seq_fields(data: dict[str, Any]) -> dict[str, int]:
    wx_seq = _pick_int(data, ("CurrentWxcontactSeq", "WxcontactSeq", "ContactSeq", "CurrentContactSeq"))
    chatroom_seq = _pick_int(
        data,
        ("CurrentChatroomContactSeq", "ChatroomContactSeq", "CurrentChatroomSeq", "ChatroomSeq"),
    )
    return {
        "current_wxcontact_seq": wx_seq,
        "current_chatroom_contact_seq": chatroom_seq,
    }


def _pick_int(data: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    for value in data.values():
        if not isinstance(value, dict):
            continue
        for key in keys:
            raw = value.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return 0


def _normalize_wechat_contact_item(item: dict[str, Any]) -> dict[str, Any]:
    wxid = str(
        item.get("Wxid")
        or item.get("wxid")
        or item.get("UserName")
        or item.get("user_name")
        or item.get("username")
        or item.get("UserId")
        or ""
    ).strip()
    nickname = str(item.get("NickName") or item.get("nickname") or item.get("Nick") or "").strip()
    remark = str(item.get("Remark") or item.get("remark") or item.get("RemarkName") or item.get("DisplayName") or "").strip()
    alias = str(item.get("Alias") or item.get("alias") or item.get("WxNumber") or item.get("wx_number") or "").strip()
    gender_raw = item.get("Sex") if item.get("Sex") is not None else item.get("sex")
    gender = _normalize_contact_gender(gender_raw)
    province = str(item.get("Province") or item.get("province") or "").strip()
    city = str(item.get("City") or item.get("city") or "").strip()
    country = str(item.get("Country") or item.get("country") or "").strip()
    signature = str(item.get("Signature") or item.get("signature") or "").strip()
    avatar = str(
        item.get("BigHeadImgUrl")
        or item.get("HeadImgUrl")
        or item.get("avatar")
        or item.get("Avatar")
        or item.get("smallHeadImgUrl")
        or ""
    ).strip()
    is_chatroom = wxid.endswith("@chatroom")
    display_name = remark or nickname or alias or wxid or "-"
    region_parts = [part for part in (country, province, city) if part]
    return {
        "wxid": wxid,
        "display_name": display_name,
        "nickname": nickname or "-",
        "remark": remark or "-",
        "alias": alias or "-",
        "gender": gender,
        "region": " / ".join(region_parts) if region_parts else "-",
        "signature": signature or "-",
        "avatar": avatar,
        "is_chatroom": is_chatroom,
        "is_personal_contact": bool(wxid) and not is_chatroom,
    }


def _normalize_contact_gender(value: Any) -> str:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return str(value).strip() or "-"
    if normalized == 1:
        return "男"
    if normalized == 2:
        return "女"
    return "未知"


def _merge_contact_fields(primary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for field in ("display_name", "nickname", "remark", "alias", "gender", "region", "signature", "avatar"):
        detail_value = str(detail.get(field) or "").strip()
        if detail_value and detail_value != "-":
            merged[field] = detail_value
    return merged


def _parse_json(raw: str | None) -> dict[str, Any]:
    normalized = (raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utcnow_iso() -> str:
    return now_shanghai().isoformat()


def _parse_datetime_like(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return ensure_shanghai_datetime(parsed)


def _normalize_qrcode_expires_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = _parse_datetime_like(value)
    if parsed:
        return ensure_shanghai_datetime(parsed).isoformat()
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return now_shanghai().isoformat()
    return (now_shanghai() + timedelta(seconds=seconds)).isoformat()


def _build_qrcode_expiry_view(runtime_info: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    expires_at = _parse_datetime_like(runtime_info.get("qrcode_expires_at"))
    if not expires_at:
        return {
            "expires_at": None,
            "expires_at_display": "-",
            "remaining_seconds": None,
            "countdown_label": "-",
            "is_expired": False,
        }
    reference_time = now or now_shanghai()
    if reference_time.tzinfo is None:
        reference_time = ensure_shanghai_datetime(reference_time)
    remaining_seconds = int((expires_at - reference_time).total_seconds())
    is_expired = remaining_seconds <= 0
    seconds = max(remaining_seconds, 0)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        countdown_label = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        countdown_label = f"{minutes:02d}:{secs:02d}"
    return {
        "expires_at": expires_at.isoformat(),
        "expires_at_display": _format_beijing_time(expires_at),
        "remaining_seconds": seconds,
        "countdown_label": "已过期" if is_expired else countdown_label,
        "is_expired": is_expired,
    }


def _build_effective_group_config(
    *,
    plugin_code: str | None,
    binding_config: dict[str, Any],
    scope_config: dict[str, Any],
) -> dict[str, Any]:
    base: dict[str, Any] = {}
    schema = get_plugin_schema(plugin_code)
    if schema:
        for field in schema.fields:
            if field.default not in (None, "", []):
                base[field.name] = field.default
    return deep_merge_config(deep_merge_config(base, binding_config), scope_config)


def _format_config_for_editor(config: dict[str, Any]) -> str:
    if not config:
        return ""
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True)


def _format_beijing_time(value: Any) -> str:
    parsed = _parse_datetime_like(value)
    if not parsed:
        return "-"
    return parsed.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def _resolve_qrcode_link_url(runtime_info: dict[str, Any]) -> str | None:
    resolved = _resolve_qrcode_image(runtime_info)
    if not resolved or resolved["kind"] != "url":
        return None
    return resolved["url"]


def _resolve_qrcode_image(runtime_info: dict[str, Any]) -> dict[str, str] | None:
    """只解析 874 当前提供的二维码 URL，不接受历史 Base64 运行字段。"""

    normalized = WechatIpadApiClient.normalize_qr_image_source(runtime_info.get("qrcode_url"))
    if normalized["url"]:
        return {"kind": "url", "url": normalized["url"]}
    return None
