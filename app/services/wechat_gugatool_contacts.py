"""咕嘎助手好友同步：把手机通讯录写入通用联系人表。

手机端 ``Friend/GetContractList`` 只返回通讯录里的联系人，字段固定为
``Wxid/Name/NickName/Remark/Alias``，不含头像。平台按 wxid 幂等写入
``workwechat_contacts``，好友页与昵称解析直接复用现有查询。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel, WorkWechatContact

logger = get_logger(__name__)

# 微信内置的系统联系人不是好友，不进入好友列表。
SYSTEM_CONTACT_IDS = frozenset(
    {
        "filehelper",
        "weixin",
        "floatbottle",
        "fmessage",
        "medianote",
        "qqmail",
        "tmessage",
        "newsapp",
        "notifymessage",
        "voip",
        "exmail_tool",
    }
)


def apply_wechat_gugatool_friend_list(
    db: Session,
    channel: Channel,
    result: dict[str, Any],
) -> dict[str, Any]:
    """消费手机 get_friend_list 的结果，按 wxid 幂等落好友资料。"""

    payload = result.get("Data") if isinstance(result, dict) and isinstance(result.get("Data"), dict) else result
    items = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {"synced_contacts": 0}

    # 当前登录账号自己的 wxid 不是好友，避免通讯录里混进自己。
    runtime: dict[str, Any] = {}
    try:
        runtime = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        runtime = {}
    connection = runtime.get("account_connection") if isinstance(runtime.get("account_connection"), dict) else {}
    self_account = str(connection.get("account_id") or "").strip()

    seen_at = now_shanghai()
    synced = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        wxid = _text(item.get("Wxid") or item.get("wxid"))
        if not wxid or wxid in SYSTEM_CONTACT_IDS or wxid.endswith("@chatroom") or wxid == self_account:
            continue

        nickname = _text(item.get("NickName") or item.get("nickname"))
        name = _text(item.get("Name") or item.get("name"))
        remark = _text(item.get("Remark") or item.get("remark"))
        alias = _text(item.get("Alias") or item.get("alias"))

        contact = db.scalar(
            select(WorkWechatContact).where(
                WorkWechatContact.channel_id == channel.id,
                WorkWechatContact.contact_external_id == wxid,
            )
        )
        if contact is None:
            contact = WorkWechatContact(
                channel_id=channel.id,
                contact_external_id=wxid,
                first_seen_at=seen_at,
            )
            db.add(contact)

        contact.contact_kind = "friend"
        contact.name = (nickname or name or remark or wxid)[:255]
        contact.display_name = (remark or name or nickname or wxid)[:255]
        contact.alias = alias[:128] or None
        contact.source = "provider_sync"
        contact.sync_status = "active"
        contact.deleted_at = None
        contact.delete_reason = None
        contact.delete_event_key = None
        contact.last_seen_at = seen_at
        contact.last_synced_at = seen_at
        contact.raw_contact_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
        synced += 1

    db.flush()
    logger.info(
        "wechat gugatool friend list applied",
        extra={"channel_id": channel.id, "synced_contacts": synced},
    )
    return {"synced_contacts": synced}


def apply_wechat_gugatool_friend_detail(
    db: Session,
    channel: Channel,
    result: dict[str, Any],
) -> dict[str, Any]:
    """消费手机 get_friend_detail 的结果，补齐好友昵称、备注和头像。"""

    payload = result.get("Data") if isinstance(result, dict) and isinstance(result.get("Data"), dict) else result
    items = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {"updated_contacts": 0}

    seen_at = now_shanghai()
    updated = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        wxid = _text(item.get("Wxid") or item.get("wxid"))
        if not wxid or wxid in SYSTEM_CONTACT_IDS or wxid.endswith("@chatroom"):
            continue

        nickname = _text(item.get("NickName") or item.get("nickname"))
        name = _text(item.get("Name") or item.get("name"))
        display_name = _text(item.get("DisplayName") or item.get("display_name"))
        remark = _text(item.get("Remark") or item.get("remark"))
        alias = _text(item.get("Alias") or item.get("alias"))

        contact = db.scalar(
            select(WorkWechatContact).where(
                WorkWechatContact.channel_id == channel.id,
                WorkWechatContact.contact_external_id == wxid,
            )
        )
        if contact is None:
            contact = WorkWechatContact(
                channel_id=channel.id,
                contact_external_id=wxid,
                first_seen_at=seen_at,
            )
            db.add(contact)

        contact.contact_kind = "friend"
        contact.name = (nickname or name or remark or wxid)[:255]
        contact.display_name = (display_name or remark or name or nickname or wxid)[:255]
        contact.alias = alias[:128] or None
        avatar_url = _text(item.get("AvatarDownloadURL") or item.get("avatar_url"))
        # 手机回传的头像地址可能是本机回环地址，服务端和浏览器都访问不到，直接丢弃。
        if avatar_url.startswith(("http://", "https://")) and not _is_loopback_url(avatar_url):
            contact.avatar_url = avatar_url
        contact.source = "provider_sync"
        contact.sync_status = "active"
        contact.deleted_at = None
        contact.delete_reason = None
        contact.delete_event_key = None
        contact.last_seen_at = seen_at
        contact.last_synced_at = seen_at
        contact.raw_contact_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
        updated += 1

    db.flush()
    logger.info(
        "wechat gugatool friend detail applied",
        extra={"channel_id": channel.id, "updated_contacts": updated},
    )
    return {"updated_contacts": updated}


def _is_loopback_url(value: str) -> bool:
    return "://127.0.0.1" in value or "://localhost" in value or "://[::1]" in value


def _text(value: Any) -> str:
    return str(value or "").strip()
