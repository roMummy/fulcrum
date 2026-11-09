from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Conversation, Message
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateError, PluginStateStore


PLUGIN_CODE = "codex_quota_allocator"
QUERY_COMMAND = "额度查询"
ADJUST_COMMAND = "额度调整"
RESET_COMMAND = "周额重置"
COMMANDS = (QUERY_COMMAND, ADJUST_COMMAND, RESET_COMMAND)
CPA_API_VERSION = "v0"
SUB2API_API_VERSION = "v1"
CODEX_WEEK_WINDOW_SECONDS = 7 * 24 * 60 * 60
CONFIRMATION_NAMESPACE = "codex_quota.confirmation"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SUBSCRIPTION_PAGES = 1_000


class QuotaServiceError(RuntimeError):
    """表示已脱敏的外部服务或额度业务错误。"""


@dataclass(frozen=True)
class QuotaConfig:
    """保存当前租户绑定唯一生效的额度服务、管理员和风控配置。"""

    admin_user_ids: tuple[str, ...]
    cpa_base_url: str
    cpa_management_key: str
    cpa_api_version: str
    cpa_auth_name: str
    sub2api_base_url: str
    sub2api_admin_api_key: str
    sub2api_api_version: str
    sub2api_group_id: int
    minimum_used_percent: float
    safety_margin_percent: float
    currency_precision: int
    daily_sleep_hours: float
    report_timezone: str
    request_timeout_seconds: int
    subscription_page_size: int
    confirmation_ttl_seconds: int
    query_rate_limit: int
    query_rate_window_seconds: int
    write_rate_limit: int
    write_rate_window_seconds: int


@dataclass(frozen=True)
class TrustedQuotaMessage:
    """保存经过数据库交叉验证的绑定、渠道、会话和真实入站消息。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message
    sender_id: str
    command: str


@dataclass(frozen=True)
class CPAAuthRecord:
    """保存 CPA 中唯一可用 Codex 认证的必要字段。"""

    name: str
    auth_index: str
    account_id: str
    plan_type: str


@dataclass(frozen=True)
class CodexWeeklyWindow:
    """保存 CPA 返回的七天主额度窗口。"""

    used_percent: float
    reset_at: int
    reset_after_seconds: int
    plan_type: str


@dataclass(frozen=True)
class SubscriptionUsage:
    """保存 Sub2API 当前有效订阅的原生周计数器。"""

    subscription_id: int
    user_id: int
    email: str
    weekly_usage_usd: float
    weekly_window_start: str


@dataclass(frozen=True)
class QuotaSnapshot:
    """保存一次查询或调整所需的同口径上游快照。"""

    window: CodexWeeklyWindow
    group: dict[str, Any]
    subscriptions: tuple[SubscriptionUsage, ...]


@dataclass(frozen=True)
class QuotaCalculation:
    """保存额度反推结果，未达到安全条件时不给出可写入限额。"""

    should_update: bool
    reason: str
    user_count: int
    total_usage_usd: float
    estimated_capacity_usd: float
    allocatable_capacity_usd: float
    per_user_limit_usd: float


def matches_event(context) -> dict[str, object]:
    """只匹配私聊或群聊中的三个精确额度业务命令。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    raw_config = getattr(context, "binding_config", {}) or {}
    command = _parse_command(content)
    matched = (
        chat_type in {"direct", "group"}
        and message_type == "text"
        and command in COMMANDS
    )
    return {
        "matched": matched,
        "reason": "codex_quota_command_hit" if matched else "codex_quota_command_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行入口只返回配置健康状态，额度业务必须来自可信入站消息。"""

    try:
        config = _load_config(request.config)
    except (TypeError, ValueError) as exc:
        return _error_result(f"插件配置无效：{exc}", "config_invalid")
    if request.action == "reload":
        contract_supported = (
            config.cpa_api_version == CPA_API_VERSION
            and config.sub2api_api_version == SUB2API_API_VERSION
        )
        return PluginExecutionResult(
            ok=contract_supported,
            status="running" if contract_supported else "error",
            message="Codex 额度管理插件运行正常。" if contract_supported else "额度服务接口版本不受支持。",
            output={
                "admin_count": len(config.admin_user_ids),
                "cpa_configured": bool(config.cpa_base_url and config.cpa_management_key),
                "sub2api_configured": bool(
                    config.sub2api_base_url
                    and config.sub2api_admin_api_key
                    and config.sub2api_group_id > 0
                ),
                "contract_supported": contract_supported,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在聊天中发送额度查询、额度调整或周额重置。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """执行可信额度命令，并在所有外部写操作前完成权限、限流和二次确认。"""

    if request.action != "execute":
        return execute_action(request)
    try:
        config = _load_config(request.config)
    except (TypeError, ValueError) as exc:
        return _error_result(f"插件配置无效：{exc}", "config_invalid")

    trusted, context_error = _require_trusted_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _error_result("当前消息上下文无效。", "trusted_context_required")

    contract_error = _validate_contract_versions(config)
    if contract_error is not None:
        return contract_error

    try:
        if trusted.command == QUERY_COMMAND:
            return _handle_query(db, trusted, config)
        if trusted.command in {ADJUST_COMMAND, RESET_COMMAND}:
            return _handle_write_command(db, trusted, config)
    except (QuotaServiceError, PluginStateError) as exc:
        # 异常文本由本插件或平台状态服务生成，不包含上游响应正文、地址或密钥。
        return _error_result(f"操作失败：{exc}", "operation_failed")
    except Exception:
        # 未知异常只返回稳定业务文案，防止第三方库异常把请求头和租户配置带入消息。
        return _error_result("操作失败，请稍后重试。", "operation_failed")
    return _error_result("不支持的额度命令。", "command_invalid")


def _handle_query(
    db: Session,
    trusted: TrustedQuotaMessage,
    config: QuotaConfig,
) -> PluginExecutionResult:
    """按真实发送人限流后读取 CPA 与 Sub2API 的同口径周额度快照。"""

    if not _provider_configured(config, require_cpa=True):
        return _error_result("请先配置当前租户自己的 CPA、Sub2API 地址和管理凭证。", "not_configured")
    decision = PluginRateLimiter(db, trusted.execution).consume(
        "user",
        identity=trusted.sender_id,
        limit=config.query_rate_limit,
        window_seconds=config.query_rate_window_seconds,
    )
    if not decision.allowed:
        return _error_result(
            f"额度查询过于频繁，请在 {decision.retry_after_seconds} 秒后重试。",
            "rate_limited",
        )

    snapshot = QuotaServiceClient(config, PluginHttpClient()).fetch_snapshot()
    return _text_result(
        _format_query_report(snapshot, config),
        "completed",
        active_subscriptions=len(snapshot.subscriptions),
        cpa_used_percent=snapshot.window.used_percent,
    )


def _handle_write_command(
    db: Session,
    trusted: TrustedQuotaMessage,
    config: QuotaConfig,
) -> PluginExecutionResult:
    """把写命令收敛为管理员校验、用户限流、快照确认和单次外部写入。"""

    if not config.admin_user_ids:
        return _error_result("请先在插件配置中设置管理员用户 ID。", "admin_not_configured")
    if trusted.sender_id not in config.admin_user_ids:
        return _error_result("只有插件管理员可以执行额度写操作。", "forbidden")
    if not _provider_configured(config, require_cpa=trusted.command == ADJUST_COMMAND):
        return _error_result("请先完整配置当前写操作需要的租户上游服务和管理凭证。", "not_configured")

    decision = PluginRateLimiter(db, trusted.execution).consume(
        "user",
        identity=trusted.sender_id,
        limit=config.write_rate_limit,
        window_seconds=config.write_rate_window_seconds,
    )
    if not decision.allowed:
        return _error_result(
            f"额度写操作过于频繁，请在 {decision.retry_after_seconds} 秒后重试。",
            "rate_limited",
        )

    store = PluginStateStore(db, trusted.execution)
    state_key = _confirmation_state_key(trusted)
    pending = store.get(CONFIRMATION_NAMESPACE, state_key)
    if (
        pending is not None
        and str(pending.get("action") or "") == trusted.command
        and int(pending.get("message_id") or 0) == int(trusted.message.id)
    ):
        # 异步任务可能重试同一条消息。相同消息只能重复得到确认提示，绝不能把
        # 队列重试当作用户主动发出的第二条确认消息。
        return _text_result(
            f"请在 {config.confirmation_ttl_seconds} 秒内发送一条新的“{trusted.command}”消息确认。",
            "confirmation_required",
            action=trusted.command,
        )

    client = QuotaServiceClient(config, PluginHttpClient())
    if trusted.command == ADJUST_COMMAND:
        prepared = _prepare_adjustment(client, config)
    else:
        prepared = _prepare_reset(client)
    if isinstance(prepared, PluginExecutionResult):
        return prepared

    fingerprint, preview, operation_data = prepared
    confirmed = (
        pending is not None
        and str(pending.get("action") or "") == trusted.command
        and str(pending.get("fingerprint") or "") == fingerprint
        and int(pending.get("message_id") or 0) != int(trusted.message.id)
    )
    if not confirmed:
        # 快照发生变化时覆盖旧确认，要求管理员基于新的目标和金额再次确认，避免
        # 首次预览与最终写入之间的订阅变化被静默接受。
        store.put(
            CONFIRMATION_NAMESPACE,
            state_key,
            {
                "action": trusted.command,
                "fingerprint": fingerprint,
                "message_id": int(trusted.message.id),
                "conversation_id": int(trusted.conversation.id),
            },
            ttl_seconds=config.confirmation_ttl_seconds,
        )
        return _text_result(
            f"{preview}\n请在 {config.confirmation_ttl_seconds} 秒内再次发送“{trusted.command}”确认。",
            "confirmation_required",
            action=trusted.command,
        )

    # 确认状态在发起外部写入前即被消费。即使上游写入失败，管理员也必须重新获取
    # 最新快照并再次确认，避免任务重试重复执行不可逆的管理操作。
    store.delete(CONFIRMATION_NAMESPACE, state_key)
    # 外部管理接口不参与本地数据库事务。这里必须先独立持久化“确认已消费”，再发起
    # 外部写入；否则外部已成功但消息任务后续回滚时，同一确认消息会在重试中再次写入。
    # 本次提交只包含当前插件的限流与确认状态，额度写入结果本身不在本地伪造成功记录。
    db.commit()
    if trusted.command == ADJUST_COMMAND:
        return _execute_adjustment(client, operation_data)
    return _execute_reset(client, operation_data)


def _prepare_adjustment(
    client: "QuotaServiceClient",
    config: QuotaConfig,
) -> tuple[str, str, dict[str, Any]] | PluginExecutionResult:
    """生成额度调整预览和稳定指纹，未达到反推条件时不创建确认状态。"""

    snapshot = client.fetch_snapshot()
    calculation = _calculate_quota(snapshot, config)
    if not calculation.should_update:
        return _text_result(f"未调整周限额：{calculation.reason}", "no_change")
    operation_data = {"snapshot": snapshot, "calculation": calculation}
    fingerprint = _operation_fingerprint(
        {
            "action": ADJUST_COMMAND,
            "group_id": int(snapshot.group["id"]),
            "daily_limit_usd": snapshot.group.get("daily_limit_usd"),
            "monthly_limit_usd": snapshot.group.get("monthly_limit_usd"),
            "cpa_used_percent": snapshot.window.used_percent,
            "subscriptions": [
                [item.subscription_id, item.weekly_usage_usd] for item in snapshot.subscriptions
            ],
            "per_user_limit_usd": calculation.per_user_limit_usd,
        }
    )
    preview = (
        "额度调整待确认\n"
        f"CPA 已用：{snapshot.window.used_percent:.2f}%\n"
        f"有效订阅：{calculation.user_count} 人\n"
        f"用户周用量：${calculation.total_usage_usd:.2f}\n"
        f"调整后每人周限额：${calculation.per_user_limit_usd:.2f}"
    )
    return fingerprint, preview, operation_data


def _prepare_reset(
    client: "QuotaServiceClient",
) -> tuple[str, str, dict[str, Any]] | PluginExecutionResult:
    """读取当前有效订阅并生成只针对 weekly 计数器的重置确认。"""

    subscriptions = client.fetch_active_subscriptions()
    if not subscriptions:
        return _text_result("当前没有有效订阅，无需重置周额。", "no_change")
    subscription_ids = [item.subscription_id for item in subscriptions]
    fingerprint = _operation_fingerprint(
        {"action": RESET_COMMAND, "subscription_ids": subscription_ids}
    )
    preview = f"周额重置待确认\n将重置 {len(subscription_ids)} 个有效订阅的 weekly 用量。"
    return fingerprint, preview, {"subscriptions": subscriptions}


def _execute_adjustment(
    client: "QuotaServiceClient",
    operation_data: Mapping[str, Any],
) -> PluginExecutionResult:
    """使用确认时重新计算出的快照更新一个目标分组的统一周限额。"""

    snapshot = operation_data.get("snapshot")
    calculation = operation_data.get("calculation")
    if not isinstance(snapshot, QuotaSnapshot) or not isinstance(calculation, QuotaCalculation):
        raise QuotaServiceError("额度调整确认数据无效")
    client.update_group_weekly_limit(snapshot.group, calculation.per_user_limit_usd)
    return _text_result(
        "Codex 周额度调整完成\n"
        f"有效订阅：{calculation.user_count} 人\n"
        f"反推总容量：${calculation.estimated_capacity_usd:.2f}\n"
        f"可分配容量：${calculation.allocatable_capacity_usd:.2f}\n"
        f"调整后每人周限额：${calculation.per_user_limit_usd:.2f}",
        "updated",
        active_subscriptions=calculation.user_count,
        weekly_limit_usd=calculation.per_user_limit_usd,
    )


def _execute_reset(
    client: "QuotaServiceClient",
    operation_data: Mapping[str, Any],
) -> PluginExecutionResult:
    """逐个重置确认快照中的有效订阅，单个失败不阻断剩余目标。"""

    raw_subscriptions = operation_data.get("subscriptions")
    if not isinstance(raw_subscriptions, tuple):
        raise QuotaServiceError("周额重置确认数据无效")
    failed_ids: list[int] = []
    success_count = 0
    for item in raw_subscriptions:
        if not isinstance(item, SubscriptionUsage):
            raise QuotaServiceError("周额重置目标无效")
        try:
            client.reset_subscription_weekly_usage(item.subscription_id)
            success_count += 1
        except QuotaServiceError:
            failed_ids.append(item.subscription_id)

    if failed_ids:
        return _error_result(
            "周额重置部分失败。\n"
            f"成功：{success_count}\n"
            f"失败订阅：{'、'.join(str(item) for item in failed_ids)}\n"
            "请检查 Sub2API 后重新发起并确认。",
            "partial_failure",
            success_count=success_count,
            failed_subscription_ids=failed_ids,
        )
    return _text_result(
        f"周额重置完成，已重置 {success_count} 个有效订阅。",
        "reset",
        reset_count=success_count,
    )


class QuotaServiceClient:
    """通过平台受控 HTTP 客户端访问固定 CPA v0 和 Sub2API v1 契约。"""

    def __init__(self, config: QuotaConfig, http_client: PluginHttpClient):
        self.config = config
        self.http_client = http_client

    def fetch_snapshot(self) -> QuotaSnapshot:
        """读取额度窗口、目标分组和有效订阅，不访问管理端用量明细。"""

        window = self.fetch_weekly_window()
        group = self.fetch_group()
        subscriptions = self.fetch_active_subscriptions()
        return QuotaSnapshot(window=window, group=group, subscriptions=subscriptions)

    def fetch_weekly_window(self) -> CodexWeeklyWindow:
        """由 CPA 代为访问 Codex 用量接口，平台不读取或保存 OAuth Token。"""

        management_headers = {"Authorization": f"Bearer {self.config.cpa_management_key}"}
        auth_payload = self._request_object(
            "GET",
            _join_url(self.config.cpa_base_url, "/v0/management/auth-files"),
            headers=management_headers,
            retry_count=1,
            label="CPA 认证查询",
        )
        auth = _select_cpa_auth(auth_payload, self.config.cpa_auth_name)
        api_call_payload = {
            "auth_index": auth.auth_index,
            "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "header": {
                "Authorization": "Bearer $TOKEN$",
                "Chatgpt-Account-Id": auth.account_id,
                "Content-Type": "application/json",
                "User-Agent": "codex_cli_rs/0.76.0",
            },
        }
        usage_payload = self._request_object(
            "POST",
            _join_url(self.config.cpa_base_url, "/v0/management/api-call"),
            headers=management_headers,
            json_body=api_call_payload,
            label="CPA 用量查询",
        )
        window = _parse_cpa_usage(usage_payload)
        if window.plan_type or not auth.plan_type:
            return window
        return CodexWeeklyWindow(
            used_percent=window.used_percent,
            reset_at=window.reset_at,
            reset_after_seconds=window.reset_after_seconds,
            plan_type=auth.plan_type,
        )

    def fetch_group(self) -> dict[str, Any]:
        """读取目标分组完整限额，以便更新时显式保留日限额和月限额。"""

        payload = self._request_object(
            "GET",
            _join_url(
                self.config.sub2api_base_url,
                f"/api/v1/admin/groups/{self.config.sub2api_group_id}",
            ),
            headers={"x-api-key": self.config.sub2api_admin_api_key},
            retry_count=1,
            label="Sub2API 分组查询",
        )
        data = _parse_sub2api_data(payload, label="分组")
        if not isinstance(data, dict):
            raise QuotaServiceError("Sub2API 分组响应结构无效")
        _validate_sub2api_group(data, self.config.sub2api_group_id)
        return dict(data)

    def fetch_active_subscriptions(self) -> tuple[SubscriptionUsage, ...]:
        """分页读取目标分组全部有效订阅，并拒绝混入其他分组或状态的数据。"""

        subscriptions: list[SubscriptionUsage] = []
        page = 1
        while page <= MAX_SUBSCRIPTION_PAGES:
            query = urlencode(
                {
                    "group_id": self.config.sub2api_group_id,
                    "status": "active",
                    "page": page,
                    "page_size": self.config.subscription_page_size,
                }
            )
            payload = self._request_object(
                "GET",
                _join_url(self.config.sub2api_base_url, f"/api/v1/admin/subscriptions?{query}"),
                headers={"x-api-key": self.config.sub2api_admin_api_key},
                retry_count=1,
                label="Sub2API 订阅查询",
            )
            data = _parse_sub2api_data(payload, label="订阅分页")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise QuotaServiceError("Sub2API 订阅分页响应结构无效")
            for raw_item in data["items"]:
                subscriptions.append(
                    _parse_subscription(raw_item, expected_group_id=self.config.sub2api_group_id)
                )
            pages = _strict_positive_int(data.get("pages"), label="Sub2API 总页数")
            if pages > MAX_SUBSCRIPTION_PAGES:
                raise QuotaServiceError("Sub2API 订阅分页超过平台上限")
            if page >= pages:
                return tuple(subscriptions)
            page += 1
        raise QuotaServiceError("Sub2API 订阅分页没有正常结束")

    def update_group_weekly_limit(self, group: Mapping[str, Any], weekly_limit_usd: float) -> None:
        """更新确认过的统一周限额，并保持同一分组的日/月限额不变。"""

        _validate_sub2api_group(group, self.config.sub2api_group_id)
        payload = self._request_object(
            "PUT",
            _join_url(
                self.config.sub2api_base_url,
                f"/api/v1/admin/groups/{self.config.sub2api_group_id}",
            ),
            headers={"x-api-key": self.config.sub2api_admin_api_key},
            json_body={
                "daily_limit_usd": group.get("daily_limit_usd"),
                "weekly_limit_usd": float(weekly_limit_usd),
                "monthly_limit_usd": group.get("monthly_limit_usd"),
            },
            label="Sub2API 分组更新",
        )
        data = _parse_sub2api_data(payload, label="分组更新")
        if not isinstance(data, dict):
            raise QuotaServiceError("Sub2API 分组更新响应结构无效")

    def reset_subscription_weekly_usage(self, subscription_id: int) -> None:
        """只重置一个订阅的 weekly 计数器，明确保留 daily 和 monthly。"""

        normalized_id = int(subscription_id)
        if normalized_id <= 0:
            raise QuotaServiceError("Sub2API 订阅 ID 无效")
        payload = self._request_object(
            "POST",
            _join_url(
                self.config.sub2api_base_url,
                f"/api/v1/admin/subscriptions/{normalized_id}/reset-quota",
            ),
            headers={"x-api-key": self.config.sub2api_admin_api_key},
            json_body={"daily": False, "weekly": True, "monthly": False},
            label="Sub2API 周额重置",
        )
        data = _parse_sub2api_data(payload, label="周额重置")
        if not isinstance(data, dict):
            raise QuotaServiceError("Sub2API 周额重置响应结构无效")

    def _request_object(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        retry_count: int = 0,
        label: str,
    ) -> dict[str, Any]:
        """执行受控请求并只接受有限大小的 JSON 对象响应。"""

        try:
            response = self.http_client.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                connect_timeout_seconds=min(5, self.config.request_timeout_seconds),
                total_timeout_seconds=self.config.request_timeout_seconds,
                max_response_bytes=MAX_RESPONSE_BYTES,
                max_redirects=2,
                retry_count=retry_count,
            )
        except PluginHttpError as exc:
            raise QuotaServiceError(f"{label}暂时不可用") from exc
        return _parse_json_response(response, label=label)


def _require_trusted_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedQuotaMessage | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、渠道、会话、消息和发送人的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type not in {"direct", "group"}
        or context.message_type != "text"
    ):
        return None, _error_result("请从真实聊天消息发起额度操作。", "trusted_context_required")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
    command = _parse_command(str(message.content or "")) if message is not None else ""
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or plugin_code != PLUGIN_CODE
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or not channel.is_enabled
        or str(channel.channel_type or "").strip() != str(context.channel_type or "").strip()
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "").strip() != str(context.chat_type or "").strip()
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip() != "in"
        or str(message.message_type or "").strip() != "text"
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
        or command not in COMMANDS
    ):
        return None, _error_result("当前消息上下文无效。", "trusted_context_required")
    return (
        TrustedQuotaMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
            sender_id=sender_id,
            command=command,
        ),
        None,
    )


def _validate_contract_versions(config: QuotaConfig) -> PluginExecutionResult | None:
    """只接受已经验证的接口版本，不猜测路由也不降级到其他契约。"""

    if config.cpa_api_version != CPA_API_VERSION:
        return _error_result("仅支持 CPA v0 管理接口，请修正插件配置。", "contract_unsupported")
    if config.sub2api_api_version != SUB2API_API_VERSION:
        return _error_result("仅支持 Sub2API v1 管理接口，请修正插件配置。", "contract_unsupported")
    return None


def _provider_configured(config: QuotaConfig, *, require_cpa: bool) -> bool:
    """按具体动作检查租户上游配置，周额重置不依赖 CPA 可用性。"""

    sub2_ready = bool(
        config.sub2api_base_url
        and config.sub2api_admin_api_key
        and config.sub2api_group_id > 0
    )
    cpa_ready = bool(config.cpa_base_url and config.cpa_management_key)
    return sub2_ready and (cpa_ready if require_cpa else True)


def _select_cpa_auth(payload: Mapping[str, Any], configured_name: str) -> CPAAuthRecord:
    """从 CPA v0 的 files 数组中选择唯一有效 Codex 认证。"""

    files = payload.get("files")
    if not isinstance(files, list):
        raise QuotaServiceError("CPA 认证响应缺少 files")
    candidates: list[Mapping[str, Any]] = []
    for item in files:
        if not isinstance(item, Mapping):
            raise QuotaServiceError("CPA 认证项结构无效")
        if (
            str(item.get("provider") or "").strip().lower() == "codex"
            and str(item.get("status") or "").strip().lower() == "active"
            and not bool(item.get("disabled"))
            and not bool(item.get("unavailable"))
        ):
            candidates.append(item)
    if configured_name:
        candidates = [item for item in candidates if str(item.get("name") or "") == configured_name]
    if not candidates:
        raise QuotaServiceError("CPA 中没有找到指定的有效 Codex 认证")
    if len(candidates) != 1:
        raise QuotaServiceError("CPA 存在多个有效 Codex 认证，请配置唯一认证文件名")
    selected = candidates[0]
    id_token = selected.get("id_token")
    if not isinstance(id_token, Mapping):
        raise QuotaServiceError("CPA Codex 认证缺少 id_token")
    auth_index = str(selected.get("auth_index") or "").strip()
    account_id = str(id_token.get("chatgpt_account_id") or "").strip()
    if not auth_index or not account_id:
        raise QuotaServiceError("CPA Codex 认证缺少必要身份字段")
    return CPAAuthRecord(
        name=str(selected.get("name") or "").strip(),
        auth_index=auth_index,
        account_id=account_id,
        plan_type=str(id_token.get("plan_type") or "").strip(),
    )


def _parse_cpa_usage(payload: Mapping[str, Any]) -> CodexWeeklyWindow:
    """解析 CPA api-call 响应，并只按七天窗口识别 Codex 主额度。"""

    status_code = _strict_int(payload.get("status_code"), label="CPA 代理状态码")
    if status_code < 200 or status_code >= 300:
        raise QuotaServiceError(f"CPA 查询 Codex 用量失败：HTTP {status_code}")
    body = payload.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise QuotaServiceError("CPA Codex 用量 body 不是有效 JSON") from exc
    if not isinstance(body, Mapping):
        raise QuotaServiceError("CPA Codex 用量 body 结构无效")
    rate_limit = body.get("rate_limit")
    if not isinstance(rate_limit, Mapping):
        raise QuotaServiceError("Codex 用量响应缺少主额度信息")
    for field_name in ("primary_window", "secondary_window"):
        candidate = rate_limit.get(field_name)
        if candidate is None:
            continue
        if not isinstance(candidate, Mapping):
            raise QuotaServiceError("Codex 额度窗口结构无效")
        duration = _strict_int(candidate.get("limit_window_seconds"), label="Codex 窗口时长")
        if duration != CODEX_WEEK_WINDOW_SECONDS:
            continue
        used_percent = _strict_float(candidate.get("used_percent"), label="Codex 周额度使用率")
        if used_percent < 0 or used_percent > 100:
            raise QuotaServiceError("Codex 周额度使用率超出范围")
        return CodexWeeklyWindow(
            used_percent=used_percent,
            reset_at=_strict_int(candidate.get("reset_at"), label="Codex 周重置时间"),
            reset_after_seconds=_strict_int(
                candidate.get("reset_after_seconds"),
                label="Codex 周重置倒计时",
            ),
            plan_type=str(body.get("plan_type") or "").strip(),
        )
    raise QuotaServiceError("Codex 用量响应没有七天额度窗口")


def _parse_sub2api_data(payload: Mapping[str, Any], *, label: str) -> Any:
    """解析 Sub2API v1 的 code/data 信封，不接受私有替代字段。"""

    if "code" not in payload or _strict_int(payload.get("code"), label="Sub2API 业务状态码") != 0:
        raise QuotaServiceError(f"Sub2API {label}返回业务错误")
    if "data" not in payload:
        raise QuotaServiceError(f"Sub2API {label}响应缺少 data")
    return payload["data"]


def _validate_sub2api_group(group: Mapping[str, Any], expected_group_id: int) -> None:
    """确认目标是配置中的 OpenAI 订阅分组，防止写入其他套餐。"""

    if _strict_int(group.get("id"), label="Sub2API 分组 ID") != int(expected_group_id):
        raise QuotaServiceError("Sub2API 返回的分组与租户配置不一致")
    if str(group.get("platform") or "").strip().lower() != "openai":
        raise QuotaServiceError("Sub2API 目标分组不是 OpenAI 分组")
    if str(group.get("subscription_type") or "").strip().lower() != "subscription":
        raise QuotaServiceError("Sub2API 目标分组不是订阅模式")


def _parse_subscription(raw_item: Any, *, expected_group_id: int) -> SubscriptionUsage:
    """严格解析一个有效订阅，接口混入其他分组或状态时整次拒绝。"""

    if not isinstance(raw_item, Mapping):
        raise QuotaServiceError("Sub2API 订阅项结构无效")
    subscription_id = _strict_positive_int(raw_item.get("id"), label="Sub2API 订阅 ID")
    user_id = _strict_positive_int(raw_item.get("user_id"), label="Sub2API 用户 ID")
    group_id = _strict_positive_int(raw_item.get("group_id"), label="Sub2API 订阅分组 ID")
    if group_id != int(expected_group_id):
        raise QuotaServiceError("Sub2API 订阅分页混入其他分组")
    if str(raw_item.get("status") or "").strip().lower() != "active":
        raise QuotaServiceError("Sub2API 订阅分页混入非有效订阅")
    user = raw_item.get("user")
    if not isinstance(user, Mapping):
        raise QuotaServiceError("Sub2API 订阅缺少用户信息")
    weekly_usage = _strict_float(raw_item.get("weekly_usage_usd"), label="Sub2API 周用量")
    if weekly_usage < 0:
        raise QuotaServiceError("Sub2API 周用量不能为负数")
    return SubscriptionUsage(
        subscription_id=subscription_id,
        user_id=user_id,
        email=str(user.get("email") or "").strip(),
        weekly_usage_usd=weekly_usage,
        weekly_window_start=str(raw_item.get("weekly_window_start") or "").strip(),
    )


def _calculate_quota(snapshot: QuotaSnapshot, config: QuotaConfig) -> QuotaCalculation:
    """使用有效订阅原生周用量和 CPA 已用比例反推每人统一周限额。"""

    user_count = len(snapshot.subscriptions)
    total_usage = Decimal(str(sum(item.weekly_usage_usd for item in snapshot.subscriptions)))
    cpa_percent = Decimal(str(snapshot.window.used_percent))
    if user_count <= 0:
        return _skipped_calculation("没有有效订阅用户", user_count, total_usage)
    if total_usage <= 0:
        return _skipped_calculation("有效订阅尚未产生周用量", user_count, total_usage)
    if cpa_percent <= 0 or cpa_percent > 100:
        return _skipped_calculation("CPA 周额度使用率无效", user_count, total_usage)
    if cpa_percent < Decimal(str(config.minimum_used_percent)):
        return _skipped_calculation("CPA 周额度使用率低于计算阈值", user_count, total_usage)

    estimated_capacity = total_usage / (cpa_percent / Decimal("100"))
    allocatable_capacity = estimated_capacity * (
        Decimal("1") - Decimal(str(config.safety_margin_percent)) / Decimal("100")
    )
    raw_per_user_limit = allocatable_capacity / Decimal(user_count)
    quantum = Decimal("1").scaleb(-config.currency_precision)
    # 金额必须向下截断；普通四舍五入可能让所有用户限额之和吃掉预留安全余量。
    per_user_limit = raw_per_user_limit.quantize(quantum, rounding=ROUND_DOWN)
    return QuotaCalculation(
        should_update=True,
        reason="计算成功",
        user_count=user_count,
        total_usage_usd=float(total_usage),
        estimated_capacity_usd=float(estimated_capacity),
        allocatable_capacity_usd=float(allocatable_capacity),
        per_user_limit_usd=float(per_user_limit),
    )


def _skipped_calculation(reason: str, user_count: int, total_usage: Decimal) -> QuotaCalculation:
    """构造明确不可写入的额度计算结果。"""

    return QuotaCalculation(
        should_update=False,
        reason=reason,
        user_count=user_count,
        total_usage_usd=float(total_usage),
        estimated_capacity_usd=0.0,
        allocatable_capacity_usd=0.0,
        per_user_limit_usd=0.0,
    )


def _format_query_report(snapshot: QuotaSnapshot, config: QuotaConfig) -> str:
    """生成紧凑额度报告，订阅邮箱只显示脱敏标识。"""

    total_usage = sum(item.weekly_usage_usd for item in snapshot.subscriptions)
    current_limit = _strict_float(snapshot.group.get("weekly_limit_usd"), label="Sub2API 当前周限额")
    lines = [
        "Codex 周额度",
        f"套餐：{snapshot.window.plan_type or '未知'}",
        f"CPA 已用：{snapshot.window.used_percent:.2f}%",
        f"周重置时间：{_format_timestamp(snapshot.window.reset_at, config.report_timezone)}",
        f"有效订阅：{len(snapshot.subscriptions)} 人",
        f"订阅周用量合计：${total_usage:.2f}",
        f"当前每人周限额：${current_limit:.2f}",
        _format_forecast(snapshot.window, config.daily_sleep_hours, config.report_timezone),
    ]
    if snapshot.subscriptions:
        lines.extend(["", "订阅明细："])
        for index, item in enumerate(snapshot.subscriptions, start=1):
            label = _mask_email(item.email) or f"用户 {item.user_id}"
            lines.append(f"{index}. {label}：${item.weekly_usage_usd:.2f}")
    return "\n".join(lines)


def _format_forecast(window: CodexWeeklyWindow, daily_sleep_hours: float, timezone_name: str) -> str:
    """按当前窗口平均速度和每日休息时间估算重置前是否会用完。"""

    current_timestamp = int(time.time())
    window_start = int(window.reset_at) - CODEX_WEEK_WINDOW_SECONDS
    if (
        window.used_percent <= 0
        or window.used_percent > 100
        or window.reset_at <= 0
        or current_timestamp <= window_start
        or current_timestamp >= window.reset_at
    ):
        return "预计用完时间：当前周窗口数据不足"
    active_ratio = (Decimal("24") - Decimal(str(daily_sleep_hours))) / Decimal("24")
    elapsed_seconds = Decimal(current_timestamp - window_start)
    remaining_active_seconds = max(
        elapsed_seconds * Decimal("100") / Decimal(str(window.used_percent)) - elapsed_seconds,
        Decimal("0"),
    )
    projected_exhaustion = current_timestamp + int(remaining_active_seconds / active_ratio)
    if projected_exhaustion < window.reset_at:
        return f"预计用完时间：{_format_timestamp(projected_exhaustion, timezone_name)}"
    remaining_window_seconds = Decimal(window.reset_at - current_timestamp)
    projected_percent = Decimal(str(window.used_percent)) + (
        Decimal(str(window.used_percent)) / elapsed_seconds * remaining_window_seconds * active_ratio
    )
    return f"预计重置时使用率：{float(projected_percent):.2f}%"


def _parse_json_response(response: PluginHttpResponse, *, label: str) -> dict[str, Any]:
    """只接受成功状态和 JSON 对象，错误页或私有文本格式不会进入业务解析。"""

    if response.status_code < 200 or response.status_code >= 300:
        raise QuotaServiceError(f"{label}失败：HTTP {response.status_code}")
    if "json" not in str(response.headers.get("content-type") or "").lower():
        raise QuotaServiceError(f"{label}没有返回 JSON")
    try:
        payload = response.json()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise QuotaServiceError(f"{label}返回了无法解析的 JSON") from exc
    if not isinstance(payload, dict):
        raise QuotaServiceError(f"{label}返回结构不是 JSON 对象")
    return payload


def _join_url(base_url: str, path: str) -> str:
    """把租户填写的服务根地址与固定契约路径拼接，不自动探测其他路由。"""

    return f"{str(base_url or '').strip().rstrip('/')}{path}"


def _confirmation_state_key(trusted: TrustedQuotaMessage) -> str:
    """哈希发送人和会话标识，状态表不直接保存外部账号 ID。"""

    raw = (
        f"tenant:{trusted.execution.tenant_id}:binding:{trusted.execution.binding_id}:"
        f"conversation:{trusted.conversation.id}:sender:{trusted.sender_id}"
    )
    return f"write:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _operation_fingerprint(payload: Mapping[str, Any]) -> str:
    """对确认目标和金额生成稳定摘要，防止变化后的快照沿用旧确认。"""

    serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _mask_email(email: str) -> str:
    """保留邮箱域用于管理员辨认，同时隐藏完整订阅账号。"""

    local, separator, domain = str(email or "").strip().partition("@")
    if not separator or not local or not domain:
        return ""
    return f"{local[0]}***@{domain}"


def _format_timestamp(timestamp: int, timezone_name: str) -> str:
    """按租户配置的报告时区格式化时间，无效时区明确使用上海时区。"""

    if int(timestamp or 0) <= 0:
        return "未知"
    try:
        timezone = ZoneInfo(str(timezone_name or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("Asia/Shanghai")
    return datetime.fromtimestamp(int(timestamp), timezone).strftime("%Y-%m-%d %H:%M:%S")


def _parse_command(content: str) -> str:
    """接受平台统一的裸命令、斜杠命令或井号命令表达，不增加业务别名。"""

    normalized = str(content or "").strip()
    if normalized[:1] in {"/", "#"}:
        normalized = normalized[1:].strip()
    return normalized


def _load_config(raw_config: Mapping[str, Any]) -> QuotaConfig:
    """读取并验证会影响真实额度写入的全部租户配置。"""

    raw_admins = raw_config.get("admin_user_ids")
    admin_user_ids = tuple(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw_admins
            if str(item or "").strip()
        )
    ) if isinstance(raw_admins, (list, tuple)) else ()
    minimum_used_percent = _config_float(raw_config, "minimum_used_percent", 5)
    safety_margin_percent = _config_float(raw_config, "safety_margin_percent", 1)
    currency_precision = _config_int(raw_config, "currency_precision", 2)
    daily_sleep_hours = _config_float(raw_config, "daily_sleep_hours", 8)
    if not 0 < minimum_used_percent <= 100:
        raise ValueError("minimum_used_percent 必须大于 0 且不超过 100")
    if not 0 <= safety_margin_percent < 100:
        raise ValueError("safety_margin_percent 必须大于等于 0 且小于 100")
    if not 0 <= currency_precision <= 6:
        raise ValueError("currency_precision 必须在 0 到 6 之间")
    if not 0 <= daily_sleep_hours < 24:
        raise ValueError("daily_sleep_hours 必须大于等于 0 且小于 24")
    return QuotaConfig(
        admin_user_ids=admin_user_ids,
        cpa_base_url=str(raw_config.get("cpa_base_url") or "").strip(),
        cpa_management_key=str(raw_config.get("cpa_management_key") or "").strip(),
        cpa_api_version=str(raw_config.get("cpa_api_version") or "").strip(),
        cpa_auth_name=str(raw_config.get("cpa_auth_name") or "").strip(),
        sub2api_base_url=str(raw_config.get("sub2api_base_url") or "").strip(),
        sub2api_admin_api_key=str(raw_config.get("sub2api_admin_api_key") or "").strip(),
        sub2api_api_version=str(raw_config.get("sub2api_api_version") or "").strip(),
        sub2api_group_id=_config_int(raw_config, "sub2api_group_id", 0),
        minimum_used_percent=minimum_used_percent,
        safety_margin_percent=safety_margin_percent,
        currency_precision=currency_precision,
        daily_sleep_hours=daily_sleep_hours,
        report_timezone=str(raw_config.get("report_timezone") or "Asia/Shanghai").strip(),
        request_timeout_seconds=_bounded_config_int(
            raw_config,
            "request_timeout_seconds",
            20,
            minimum=5,
            maximum=120,
        ),
        subscription_page_size=_bounded_config_int(
            raw_config,
            "subscription_page_size",
            100,
            minimum=1,
            maximum=100,
        ),
        confirmation_ttl_seconds=_bounded_config_int(
            raw_config,
            "confirmation_ttl_seconds",
            300,
            minimum=60,
            maximum=1_800,
        ),
        query_rate_limit=_bounded_config_int(
            raw_config,
            "query_rate_limit",
            2,
            minimum=1,
            maximum=100,
        ),
        query_rate_window_seconds=_bounded_config_int(
            raw_config,
            "query_rate_window_seconds",
            600,
            minimum=1,
            maximum=86_400,
        ),
        write_rate_limit=_bounded_config_int(
            raw_config,
            "write_rate_limit",
            4,
            minimum=1,
            maximum=100,
        ),
        write_rate_window_seconds=_bounded_config_int(
            raw_config,
            "write_rate_window_seconds",
            600,
            minimum=1,
            maximum=86_400,
        ),
    )


def _config_int(raw_config: Mapping[str, Any], key: str, default: int) -> int:
    """把整数配置错误在插件入口直接暴露，不使用隐式默认值掩盖。"""

    raw_value = raw_config.get(key, default)
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc


def _config_float(raw_config: Mapping[str, Any], key: str, default: float) -> float:
    """把浮点配置错误在插件入口直接暴露。"""

    raw_value = raw_config.get(key, default)
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是数字") from exc


def _bounded_config_int(
    raw_config: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """读取有明确安全边界的整数配置。"""

    value = _config_int(raw_config, key, default)
    if value < minimum or value > maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _strict_int(value: Any, *, label: str) -> int:
    """严格解析上游整数，不把缺失或非法值默认为零。"""

    if isinstance(value, bool) or value is None:
        raise QuotaServiceError(f"{label}无效")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise QuotaServiceError(f"{label}无效") from exc


def _strict_positive_int(value: Any, *, label: str) -> int:
    """严格解析正整数标识。"""

    normalized = _strict_int(value, label=label)
    if normalized <= 0:
        raise QuotaServiceError(f"{label}必须为正整数")
    return normalized


def _strict_float(value: Any, *, label: str) -> float:
    """严格解析上游数字，不接受布尔值或空值。"""

    if isinstance(value, bool) or value is None:
        raise QuotaServiceError(f"{label}无效")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise QuotaServiceError(f"{label}无效") from exc


def _text_result(
    message: str,
    provider_status: str,
    **output: Any,
) -> PluginExecutionResult:
    """构造成功业务结果并统一进入平台标准文本回复。"""

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": provider_status,
            **output,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _error_result(
    message: str,
    provider_status: str,
    **output: Any,
) -> PluginExecutionResult:
    """构造不含上游敏感详情的稳定错误回复。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            **output,
            "replies": standard_replies([text_reply(message)]),
        },
    )
