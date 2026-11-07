"""租户账号连接草稿、身份验证和原子启用服务。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.models.entities import Bot, Channel
from app.models.saas import BotBinding
from app.schemas.forms import BotCreate, ChannelCreate
from app.services.bot_entitlements import (
    BotEntitlementError,
    add_verified_bot_binding,
    get_bot_quota,
    hash_device_fingerprint,
)
from app.services.channel_catalog import get_channel_type_definition
from app.services.crud import add_bot, add_channel
from app.services.platform_protocol_services import (
    PlatformProtocolServiceError,
    require_enabled_platform_protocol_service,
)

from .contracts import AccountConnectionError, AccountConnectionResult
from .registry import get_account_connector


@dataclass(frozen=True)
class FinalizedAccountConnection:
    """最终启用后返回给页面的真实业务对象。"""

    bot: Bot
    channel: Channel
    binding: BotBinding


def _load_runtime_info(channel: Channel) -> dict[str, object]:
    """读取渠道运行信息，损坏内容按空对象处理且不参与身份判断。"""

    try:
        payload = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_connection_state(channel: Channel, payload: Mapping[str, object]) -> None:
    """只更新账号连接命名空间，保留渠道其他运行信息。"""

    runtime_info = _load_runtime_info(channel)
    runtime_info["account_connection"] = dict(payload)
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)


def _require_owned_channel(db: Session, channel_id: int, *, for_update: bool = False) -> Channel:
    """按可信租户读取连接草稿，禁止跨租户主键探测。"""

    tenant_id = require_tenant_id()
    query = select(Channel).where(Channel.id == int(channel_id), Channel.tenant_id == tenant_id)
    if for_update:
        query = query.with_for_update()
    channel = db.scalar(query)
    if channel is None:
        raise AccountConnectionError("connection_not_found")
    return channel


def _unique_bot_name(db: Session, requested_name: str) -> str:
    """在当前租户内生成稳定可读名称，不依赖历史 slug 或兼容字段。"""

    tenant_id = require_tenant_id()
    base_name = str(requested_name or "新接入账号").strip()[:100] or "新接入账号"
    candidate = base_name
    suffix = 2
    while db.scalar(select(Bot.id).where(Bot.tenant_id == tenant_id, Bot.name == candidate)) is not None:
        suffix_text = f" {suffix}"
        candidate = f"{base_name[: max(1, 100 - len(suffix_text))]}{suffix_text}"
        suffix += 1
    return candidate


def _resolve_connection_api_base(
    db: Session,
    *,
    channel_type: str,
    submitted_api_base: object,
) -> str:
    """解析连接草稿使用的服务地址，平台托管渠道只信任平台级配置。"""

    definition = get_channel_type_definition(channel_type)
    if not definition.platform_managed_endpoint:
        return str(submitted_api_base or "").strip()
    try:
        # 平台托管地址必须在创建 Bot 之前解析，缺失或停用时不能留下孤立草稿。
        service = require_enabled_platform_protocol_service(db, definition.code)
    except PlatformProtocolServiceError as exc:
        raise AccountConnectionError(str(exc)) from exc
    return service.api_base


def begin_account_connection(
    db: Session,
    *,
    channel_type: str,
    display_name: str,
    connection_values: Mapping[str, object],
) -> Channel:
    """创建不可调度且不占用额度的账号连接草稿。"""

    definition = get_channel_type_definition(channel_type)
    quota = get_bot_quota(db)
    if quota.remaining <= 0:
        raise AccountConnectionError("bot_quota_exceeded")
    values = dict(connection_values)
    resolved_api_base = _resolve_connection_api_base(
        db,
        channel_type=definition.code,
        submitted_api_base=values.get("api_base"),
    )
    bot_name = _unique_bot_name(db, display_name or definition.label)
    bot = add_bot(
        db,
        BotCreate(name=bot_name),
        is_enabled=False,
        status="stopped",
    )
    provider_node_id = int(values.get("provider_node_id") or 0) or None
    provider_account_id = int(values.get("provider_account_id") or 0) or None
    if not definition.requires_executor_binding:
        # 执行端主键是企业微信专项配置，其他渠道即使收到伪造表单值也必须清空。
        provider_node_id = None
        provider_account_id = None
    channel = add_channel(
        db,
        ChannelCreate(
            bot_id=bot.id,
            name=bot_name,
            channel_type=definition.code,
            api_base=resolved_api_base,
            bot_token=str(values.get("bot_token") or ""),
            webhook_path="",
            dm_policy="allowlist",
            allow_from="",
            adapter_config_json=str(values.get("adapter_config_json") or ""),
            provider_node_id=provider_node_id,
            provider_account_id=provider_account_id,
            is_enabled=False,
        ),
        status="draft",
    )
    _save_connection_state(
        channel,
        {
            "state": "draft",
            "next_action": "verify",
            "message": "连接草稿已创建",
        },
    )
    try:
        db.commit()
        db.refresh(channel)
        return channel
    except IntegrityError as exc:
        db.rollback()
        raise AccountConnectionError("connection_draft_conflict") from exc


def verify_account_connection(db: Session, *, channel_id: int) -> AccountConnectionResult:
    """调用具体连接器验证身份，并只持久化账号身份和设备摘要。"""

    channel = _require_owned_channel(db, channel_id)
    if channel.is_enabled:
        raise AccountConnectionError("connection_already_active")
    connector = get_account_connector(channel.channel_type)
    result = connector.verify(db, channel)
    state_payload: dict[str, object] = {
        "state": result.state,
        "next_action": result.next_action,
        "message": result.message,
        "error_code": result.error_code,
    }
    if result.identity is not None:
        # 原始设备键只在当前调用栈参与 HMAC，数据库和页面均只接触摘要。
        state_payload.update(
            adapter_type=result.identity.adapter_type,
            account_id=result.identity.account_id,
            display_name=result.identity.display_name,
            device_fingerprint_hash=hash_device_fingerprint(result.identity.device_key),
        )
    _save_connection_state(channel, state_payload)
    channel.status = result.state
    db.commit()
    db.refresh(channel)
    return result


def finalize_account_connection(
    db: Session,
    *,
    channel_id: int,
    display_name: str,
) -> FinalizedAccountConnection:
    """在一个事务中完成机器人、渠道和商业额度绑定的最终启用。"""

    channel = _require_owned_channel(db, channel_id, for_update=True)
    if channel.is_enabled:
        raise AccountConnectionError("connection_already_active")
    runtime_info = _load_runtime_info(channel)
    connection = runtime_info.get("account_connection")
    if not isinstance(connection, dict) or connection.get("state") != "verified":
        raise AccountConnectionError("connection_not_verified")
    tenant_id = require_tenant_id()
    bot = db.scalar(select(Bot).where(Bot.id == channel.bot_id, Bot.tenant_id == tenant_id).with_for_update())
    if bot is None:
        raise AccountConnectionError("connection_bot_missing")
    normalized_name = _unique_bot_name(db, display_name or str(connection.get("display_name") or channel.name))
    # 当前草稿机器人本身占用了原名称，最终确认相同名称时无需追加序号。
    if normalized_name != bot.name and (display_name or "").strip() == bot.name:
        normalized_name = bot.name
    try:
        binding = add_verified_bot_binding(
            db,
            adapter_type=str(connection.get("adapter_type") or ""),
            bot_account_id=str(connection.get("account_id") or ""),
            device_fingerprint_hash=str(connection.get("device_fingerprint_hash") or ""),
            display_name=(display_name or str(connection.get("display_name") or "")).strip(),
            bot_id=bot.id,
            channel_id=channel.id,
        )
        bot.name = normalized_name
        bot.is_enabled = True
        bot.status = "running"
        channel.name = normalized_name
        channel.is_enabled = True
        channel.status = "online"
        connection.update(
            state="active",
            next_action="manage",
            message="账号已接入",
            binding_id=binding.id,
        )
        runtime_info["account_connection"] = connection
        channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
        db.commit()
        db.refresh(bot)
        db.refresh(channel)
        db.refresh(binding)
        return FinalizedAccountConnection(bot=bot, channel=channel, binding=binding)
    except BotEntitlementError as exc:
        db.rollback()
        raise AccountConnectionError(str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise AccountConnectionError("connection_finalize_conflict") from exc
