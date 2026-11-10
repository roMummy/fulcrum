from __future__ import annotations

import hashlib
import json
import re
import secrets
import smtplib
import ssl
import string
import time
from dataclasses import dataclass
from email.message import EmailMessage
from http.cookies import CookieError, SimpleCookie
from typing import Any, Mapping
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message
from app.models.plugin_business import CouponAccountBinding
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateError


PLUGIN_CODE = "newapi_coupon"
COMMAND = "领券"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)


class CouponError(RuntimeError):
    """表示不包含上游敏感信息的领券业务错误。"""


@dataclass(frozen=True)
class CouponConfig:
    """保存当前租户绑定唯一生效的 NewAPI、套餐、限流和通知配置。"""

    base_url: str
    admin_access_token: str
    admin_user_id: int
    subscription_plan_id: int
    auto_create_user: bool
    username_prefix: str
    request_timeout_seconds: int
    claim_rate_limit: int
    claim_rate_window_seconds: int
    notify_mode: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from_email: str
    smtp_use_ssl: bool
    email_subject: str


@dataclass(frozen=True)
class TrustedCouponMessage:
    """保存经过数据库交叉验证的群、成员和入站领券消息。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    chatroom: Chatroom
    member: ChatroomMember
    message: Message
    sender_id: str
    email: str


@dataclass(frozen=True)
class NewAPIUser:
    """保存 NewAPI 用户的有限身份字段。"""

    user_id: int
    username: str
    email: str


@dataclass(frozen=True)
class NewAPIPlan:
    """保存租户配置的目标订阅套餐。"""

    plan_id: int
    title: str


def matches_event(context) -> dict[str, object]:
    """只匹配群聊中的“领券 邮箱”文本。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    raw_config = getattr(context, "binding_config", {}) or {}
    command, _email = _parse_command(content)
    matched = (
        chat_type == "group"
        and message_type == "text"
        and command == COMMAND
    )
    return {
        "matched": matched,
        "reason": "newapi_coupon_hit" if matched else "newapi_coupon_command_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只报告配置健康，领券必须由真实群消息触发。"""

    try:
        config = _load_config(request.config)
    except (TypeError, ValueError) as exc:
        return _error_result(f"插件配置无效：{exc}", "config_invalid")
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="NewAPI 领券插件运行正常。",
            output={
                "newapi_configured": _newapi_configured(config),
                "auto_create_user": config.auto_create_user,
                "notification_configured": _notification_configured(config),
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在群聊中发送“领券 邮箱”。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """预留唯一领取关系后创建或复用 NewAPI 用户，并幂等绑定一个订阅套餐。"""

    if request.action != "execute":
        return execute_action(request)
    try:
        config = _load_config(request.config)
    except (TypeError, ValueError) as exc:
        return _error_result(f"插件配置无效：{exc}", "config_invalid")

    trusted, context_error = _require_trusted_group_member(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _error_result("当前群成员上下文无效。", "trusted_context_required")
    if not _newapi_configured(config) or not _notification_configured(config):
        return _error_result("请先完整配置当前租户的 NewAPI 和自动开户通知信息。", "not_configured")

    try:
        decision = PluginRateLimiter(db, trusted.execution).consume(
            "user",
            identity=trusted.sender_id,
            limit=config.claim_rate_limit,
            window_seconds=config.claim_rate_window_seconds,
        )
        if not decision.allowed:
            return _error_result(
                f"领券请求过于频繁，请在 {decision.retry_after_seconds} 秒后重试。",
                "rate_limited",
            )

        reserved, reservation_result = _reserve_binding(db, trusted, config)
        if reservation_result is not None or reserved is None:
            return reservation_result or _error_result("领券预留失败。", "reservation_failed")
        if reserved.status == "active":
            return _already_issued_result(reserved)

        client = NewAPIClient(config, PluginHttpClient())
        result = _issue_subscription(db, trusted, reserved, config, client)
        return result
    except (CouponError, PluginStateError) as exc:
        return _error_result(f"领券失败：{exc}", "operation_failed")
    except Exception:
        return _error_result("领券失败，请稍后重试。", "operation_failed")


def _reserve_binding(
    db: Session,
    trusted: TrustedCouponMessage,
    config: CouponConfig,
) -> tuple[CouponAccountBinding | None, PluginExecutionResult | None]:
    """先用唯一约束预留用户和邮箱，避免并发请求重复创建上游账号。"""

    member_binding = db.scalar(
        select(CouponAccountBinding)
        .where(
            CouponAccountBinding.tenant_id == int(trusted.execution.tenant_id),
            CouponAccountBinding.bot_plugin_id == int(trusted.binding.id),
            CouponAccountBinding.channel_id == int(trusted.channel.id),
            CouponAccountBinding.member_external_id == trusted.sender_id,
        )
        .with_for_update()
    )
    email_binding = db.scalar(
        select(CouponAccountBinding)
        .where(
            CouponAccountBinding.tenant_id == int(trusted.execution.tenant_id),
            CouponAccountBinding.bot_plugin_id == int(trusted.binding.id),
            CouponAccountBinding.email_normalized == trusted.email,
        )
        .with_for_update()
    )
    if member_binding is not None and member_binding.email_normalized != trusted.email:
        return None, _error_result(
            f"当前账号已经绑定邮箱 {_mask_email(member_binding.email_normalized)}，不能自行更换。",
            "member_already_bound",
        )
    if email_binding is not None and (
        email_binding.channel_id != trusted.channel.id
        or email_binding.member_external_id != trusted.sender_id
    ):
        return None, _error_result("这个邮箱已经被其他账号领取。", "email_already_bound")
    if member_binding is not None:
        return member_binding, None

    username = _build_username(config.username_prefix, trusted.email, trusted.channel.id, trusted.sender_id)
    reservation = CouponAccountBinding(
        tenant_id=int(trusted.execution.tenant_id),
        bot_plugin_id=int(trusted.binding.id),
        channel_id=int(trusted.channel.id),
        conversation_id=int(trusted.conversation.id),
        member_external_id=trusted.sender_id,
        email_normalized=trusted.email,
        newapi_username=username,
        status="pending",
        notification_status="pending" if config.auto_create_user else "not_required",
        last_message_id=int(trusted.message.id),
    )
    try:
        with db.begin_nested():
            db.add(reservation)
            db.flush()
    except IntegrityError:
        # 并发事务已抢先预留时回读唯一记录，绝不创建第二条兼容或旁路绑定。
        existing = db.scalar(
            select(CouponAccountBinding).where(
                CouponAccountBinding.tenant_id == int(trusted.execution.tenant_id),
                CouponAccountBinding.bot_plugin_id == int(trusted.binding.id),
                CouponAccountBinding.channel_id == int(trusted.channel.id),
                CouponAccountBinding.member_external_id == trusted.sender_id,
            )
        )
        if existing is None:
            return None, _error_result("账号或邮箱正在被其他请求处理。", "claim_in_progress")
        if existing.email_normalized != trusted.email:
            return None, _error_result("当前账号已经绑定其他邮箱。", "member_already_bound")
        return existing, None

    reservation_id = int(reservation.id)
    # 外部开户注册不属于数据库事务。预留必须先独立提交，任务重试才能复用同一用户名，
    # 并且其他并发请求会被数据库唯一约束挡在 NewAPI 调用之前。
    db.commit()
    return db.get(CouponAccountBinding, reservation_id), None


def _issue_subscription(
    db: Session,
    trusted: TrustedCouponMessage,
    binding: CouponAccountBinding,
    config: CouponConfig,
    client: "NewAPIClient",
) -> PluginExecutionResult:
    """复用预留记录完成上游用户定位、套餐绑定、扣费偏好和安全通知。"""

    password = ""
    created = False
    user = None
    if binding.newapi_user_id:
        user = NewAPIUser(
            user_id=int(binding.newapi_user_id),
            username=str(binding.newapi_username),
            email=binding.email_normalized,
        )
    else:
        user = client.find_user(binding.email_normalized, binding.newapi_username)
    if user is None:
        if not config.auto_create_user:
            _mark_failed(db, binding.id, "user_not_found")
            return _error_result("没有找到该邮箱对应的 NewAPI 用户，请联系管理员开户。", "user_not_found")
        password = _generate_password()
        client.create_user(
            username=binding.newapi_username,
            password=password,
            display_name=_build_display_name(trusted.member, binding.email_normalized),
        )
        user = client.find_user(binding.email_normalized, binding.newapi_username)
        if user is None:
            _mark_failed(db, binding.id, "created_user_not_found")
            return _error_result("NewAPI 用户创建后无法确认，请联系管理员检查。", "created_user_not_found")
        created = True

    if not _claim_upstream_user(db, binding.id, user):
        return _error_result("这个 NewAPI 账号已经发放给其他用户。", "newapi_user_already_bound")
    binding = db.get(CouponAccountBinding, int(binding.id))
    if binding is None:
        raise CouponError("领取预留记录不存在")

    plan = client.resolve_plan(config.subscription_plan_id)
    already_active = client.has_active_subscription(user.user_id, plan.plan_id)
    if not already_active:
        client.bind_subscription(user.user_id, plan.plan_id)

    preference_updated = False
    if created:
        preference_updated = client.set_subscription_preference(
            username=user.username,
            password=password,
            user_id=user.user_id,
        )

    binding.plan_id = plan.plan_id
    binding.plan_title = plan.title
    binding.status = "active"
    binding.last_message_id = int(trusted.message.id)
    binding.last_error_code = None if preference_updated or not created else "billing_preference_failed"
    binding.issued_at = now_shanghai()
    binding.notification_status = "pending" if created else "not_required"
    db.commit()

    notification_sent = False
    if created:
        try:
            _send_account_email(
                config,
                recipient=binding.email_normalized,
                username=user.username,
                password=password,
                plan_title=plan.title,
            )
            notification_sent = True
        except Exception:
            notification_sent = False
        persisted = db.get(CouponAccountBinding, int(binding.id))
        if persisted is not None:
            persisted.notification_status = "sent" if notification_sent else "failed"
            persisted.last_error_code = (
                persisted.last_error_code if notification_sent else "notification_failed"
            )
            db.commit()

    if created and not notification_sent:
        return _text_result(
            "NewAPI 套餐已发放，但账号邮件发送失败。初始密码不会在群内展示，请联系管理员重置密码。",
            "issued_notification_failed",
            binding_id=int(binding.id),
        )
    if created:
        message = (
            f"NewAPI {plan.title} 已发放，账号信息已发送到 {_mask_email(binding.email_normalized)}。"
        )
        if not preference_updated:
            message += " 订阅扣费偏好未自动切换，请登录后确认。"
    elif already_active:
        message = f"账号 {user.username} 已有有效的 {plan.title}，本次已完成领取绑定。"
    else:
        message = f"已为账号 {user.username} 绑定 {plan.title}。"
    return _text_result(
        message,
        "issued",
        binding_id=int(binding.id),
        plan_id=plan.plan_id,
        already_active=already_active,
    )


def _claim_upstream_user(db: Session, binding_id: int, user: NewAPIUser) -> bool:
    """在继续绑定套餐前持久化上游用户 ID，让唯一约束阻止跨用户重复发放。"""

    binding = db.get(CouponAccountBinding, int(binding_id))
    if binding is None:
        raise CouponError("领取预留记录不存在")
    occupied = db.scalar(
        select(CouponAccountBinding).where(
            CouponAccountBinding.tenant_id == int(binding.tenant_id),
            CouponAccountBinding.bot_plugin_id == int(binding.bot_plugin_id),
            CouponAccountBinding.newapi_user_id == int(user.user_id),
            CouponAccountBinding.id != int(binding.id),
        )
    )
    if occupied is not None:
        binding.status = "failed"
        binding.last_error_code = "newapi_user_already_bound"
        db.commit()
        return False
    try:
        binding.newapi_user_id = int(user.user_id)
        binding.newapi_username = user.username
        binding.last_error_code = None
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _mark_failed(db: Session, binding_id: int, error_code: str) -> None:
    """只保存稳定错误码，不持久化第三方响应、令牌或初始密码。"""

    binding = db.get(CouponAccountBinding, int(binding_id))
    if binding is None:
        return
    binding.status = "failed"
    binding.last_error_code = str(error_code)[:80]
    db.commit()


def _already_issued_result(binding: CouponAccountBinding) -> PluginExecutionResult:
    """重复领取只返回既有业务结果，不再次查询或修改 NewAPI。"""

    if binding.notification_status == "failed":
        message = "当前账号已经领取过套餐，但账号邮件发送失败，请联系管理员处理。"
    else:
        message = f"当前账号已经领取过 {binding.plan_title or 'NewAPI 套餐'}，无需重复操作。"
    return _text_result(message, "already_issued", binding_id=int(binding.id))


class NewAPIClient:
    """通过平台受控 HTTP 客户端访问当前确认的 NewAPI 用户和订阅接口。"""

    def __init__(self, config: CouponConfig, http_client: PluginHttpClient):
        self.config = config
        self.http_client = http_client

    def find_user(self, email: str, username: str) -> NewAPIUser | None:
        """按邮箱和确定性用户名依次精确查找用户，不接受模糊结果。"""

        for keyword in (email, username):
            query = urlencode({"keyword": keyword})
            payload, _response = self._request(
                "GET",
                f"/api/user/search?{query}",
                admin=True,
                retry_count=1,
                label="NewAPI 用户查询",
            )
            data = _parse_success_data(payload, label="用户查询")
            if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
                raise CouponError("NewAPI 用户查询响应结构无效")
            for item in data["items"]:
                if not isinstance(item, Mapping):
                    raise CouponError("NewAPI 用户项结构无效")
                user_id = _positive_int(item.get("id"), label="NewAPI 用户 ID")
                item_username = str(item.get("username") or "").strip()
                item_email = str(item.get("email") or "").strip().lower()
                if item_email == email or item_username == username:
                    return NewAPIUser(user_id=user_id, username=item_username, email=item_email)
        return None

    def create_user(self, *, username: str, password: str, display_name: str) -> None:
        """创建普通用户，邮箱绑定关系只保存在本地唯一业务表。"""

        payload, _response = self._request(
            "POST",
            "/api/user/",
            admin=True,
            json_body={
                "username": username,
                "password": password,
                "display_name": display_name,
                "role": 1,
            },
            label="NewAPI 用户创建",
        )
        _parse_success_data(payload, label="用户创建", allow_null=True)

    def resolve_plan(self, plan_id: int) -> NewAPIPlan:
        """只按租户配置的明确套餐 ID 解析，不使用标题或首项回退。"""

        payload, _response = self._request(
            "GET",
            "/api/subscription/admin/plans",
            admin=True,
            retry_count=1,
            label="NewAPI 套餐查询",
        )
        data = _parse_success_data(payload, label="套餐查询")
        if not isinstance(data, list):
            raise CouponError("NewAPI 套餐响应结构无效")
        for item in data:
            if not isinstance(item, Mapping):
                raise CouponError("NewAPI 套餐项结构无效")
            current_id = _positive_int(item.get("id"), label="NewAPI 套餐 ID")
            if current_id == int(plan_id):
                title = str(item.get("title") or "").strip()
                if not title:
                    raise CouponError("NewAPI 套餐缺少标题")
                return NewAPIPlan(plan_id=current_id, title=title)
        raise CouponError("没有找到租户配置的 NewAPI 套餐")

    def has_active_subscription(self, user_id: int, plan_id: int) -> bool:
        """判断用户是否已有当前仍有效的目标套餐。"""

        payload, _response = self._request(
            "GET",
            f"/api/subscription/admin/users/{int(user_id)}/subscriptions",
            admin=True,
            retry_count=1,
            label="NewAPI 用户订阅查询",
        )
        data = _parse_success_data(payload, label="用户订阅查询")
        if not isinstance(data, list):
            raise CouponError("NewAPI 用户订阅响应结构无效")
        current_timestamp = int(time.time())
        for item in data:
            if not isinstance(item, Mapping):
                raise CouponError("NewAPI 用户订阅项结构无效")
            if (
                _positive_int(item.get("plan_id"), label="NewAPI 订阅套餐 ID") == int(plan_id)
                and str(item.get("status") or "").strip() == "active"
                and _positive_int(item.get("end_time"), label="NewAPI 订阅结束时间") > current_timestamp
            ):
                return True
        return False

    def bind_subscription(self, user_id: int, plan_id: int) -> None:
        """把一个明确用户绑定到一个明确套餐，写请求不自动重试。"""

        payload, _response = self._request(
            "POST",
            "/api/subscription/admin/bind",
            admin=True,
            json_body={"user_id": int(user_id), "plan_id": int(plan_id)},
            label="NewAPI 套餐绑定",
        )
        _parse_success_data(payload, label="套餐绑定", allow_null=True)

    def set_subscription_preference(self, *, username: str, password: str, user_id: int) -> bool:
        """使用新账号会话切换为订阅扣费；失败不回传凭证或第三方正文。"""

        try:
            login_payload, login_response = self._request(
                "POST",
                "/api/user/login",
                admin=False,
                json_body={"username": username, "password": password},
                label="NewAPI 用户登录",
            )
            _parse_success_data(login_payload, label="用户登录", allow_null=True)
            cookie = _extract_session_cookie(login_response.headers.get("set-cookie"))
            preference_payload, _response = self._request(
                "PUT",
                "/api/subscription/self/preference",
                admin=False,
                headers={"New-Api-User": str(int(user_id)), "Cookie": cookie},
                json_body={"billing_preference": "subscription"},
                label="NewAPI 扣费偏好更新",
            )
            _parse_success_data(preference_payload, label="扣费偏好更新", allow_null=True)
            return True
        except CouponError:
            return False

    def _request(
        self,
        method: str,
        path: str,
        *,
        admin: bool,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        retry_count: int = 0,
        label: str,
    ) -> tuple[dict[str, Any], PluginHttpResponse]:
        """发送有限大小请求并只接受 JSON 对象。"""

        request_headers = dict(headers or {})
        if admin:
            request_headers.update(
                {
                    "Authorization": self.config.admin_access_token,
                    "New-Api-User": str(self.config.admin_user_id),
                }
            )
        try:
            response = self.http_client.request(
                method,
                f"{self.config.base_url.rstrip('/')}{path}",
                headers=request_headers,
                json_body=json_body,
                connect_timeout_seconds=min(5, self.config.request_timeout_seconds),
                total_timeout_seconds=self.config.request_timeout_seconds,
                max_response_bytes=MAX_RESPONSE_BYTES,
                max_redirects=2,
                retry_count=retry_count,
            )
        except PluginHttpError as exc:
            raise CouponError(f"{label}暂时不可用") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise CouponError(f"{label}失败：HTTP {response.status_code}")
        if "json" not in str(response.headers.get("content-type") or "").lower():
            raise CouponError(f"{label}没有返回 JSON")
        try:
            payload = response.json()
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CouponError(f"{label}返回了无法解析的 JSON") from exc
        if not isinstance(payload, dict):
            raise CouponError(f"{label}返回结构不是 JSON 对象")
        return payload, response


def _require_trusted_group_member(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedCouponMessage | None, PluginExecutionResult | None]:
    """验证租户、绑定、渠道、群会话、入站消息和活跃群成员的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
        or context.message_type != "text"
    ):
        return None, _error_result("请在真实群聊中发送“领券 邮箱”。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
    command, email = _parse_command(str(message.content or "")) if message is not None else ("", "")
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
        or str(conversation.chat_type or "").strip() != "group"
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip() != "in"
        or str(message.message_type or "").strip() != "text"
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
        or command != COMMAND
    ):
        return None, _error_result("当前领券消息上下文无效。", "trusted_context_required")
    if not email:
        return None, _error_result("命令格式：领券 user@example.com", "email_invalid")

    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(channel.id),
            Chatroom.external_id == str(conversation.external_id),
            Chatroom.is_active.is_(True),
        )
    )
    member = None
    if chatroom is not None:
        member = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.tenant_id == int(context.tenant_id),
                ChatroomMember.chatroom_id == int(chatroom.id),
                ChatroomMember.channel_id == int(channel.id),
                ChatroomMember.member_external_id == sender_id,
                ChatroomMember.status == "active",
            )
        )
    if chatroom is None or member is None:
        return None, _error_result("当前无法确认你是本群活跃成员，请联系管理员刷新群成员。", "group_member_required")
    return (
        TrustedCouponMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            chatroom=chatroom,
            member=member,
            message=message,
            sender_id=sender_id,
            email=email,
        ),
        None,
    )


def _parse_success_data(payload: Mapping[str, Any], *, label: str, allow_null: bool = False) -> Any:
    """解析当前确认的 NewAPI success/message/data 响应，不兼容其他信封。"""

    if payload.get("success") is not True:
        raise CouponError(f"NewAPI {label}返回业务错误")
    if "data" not in payload:
        raise CouponError(f"NewAPI {label}响应缺少 data")
    data = payload["data"]
    if data is None and not allow_null:
        raise CouponError(f"NewAPI {label}响应 data 为空")
    return data


def _extract_session_cookie(raw_header: str | None) -> str:
    """只提取登录响应中的第一个会话 Cookie，不保存其他响应头。"""

    normalized = str(raw_header or "").strip()
    if not normalized:
        raise CouponError("NewAPI 登录响应缺少会话 Cookie")
    cookie = SimpleCookie()
    try:
        cookie.load(normalized)
    except CookieError as exc:
        raise CouponError("NewAPI 登录 Cookie 无法解析") from exc
    if not cookie:
        raise CouponError("NewAPI 登录 Cookie 为空")
    key = next(iter(cookie.keys()))
    value = cookie[key].value
    if not key or not value:
        raise CouponError("NewAPI 登录 Cookie 无效")
    return f"{key}={value}"


def _send_account_email(
    config: CouponConfig,
    *,
    recipient: str,
    username: str,
    password: str,
    plan_title: str,
) -> None:
    """通过租户自己的 SMTP 发送初始账号信息，不把密码写入群消息或数据库。"""

    message = EmailMessage()
    message["Subject"] = config.email_subject
    message["From"] = config.smtp_from_email
    message["To"] = recipient
    message.set_content(
        "NewAPI 套餐已开通。\n\n"
        f"登录地址：{config.base_url}\n"
        f"登录用户名：{username}\n"
        f"初始密码：{password}\n"
        f"订阅套餐：{plan_title}\n\n"
        "请登录后立即修改密码，不要转发本邮件。"
    )
    if config.smtp_use_ssl:
        with smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            timeout=config.request_timeout_seconds,
            context=ssl.create_default_context(),
        ) as client:
            client.login(config.smtp_username, config.smtp_password)
            client.send_message(message)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.request_timeout_seconds) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(config.smtp_username, config.smtp_password)
        client.send_message(message)


def _parse_command(content: str) -> tuple[str, str]:
    """解析唯一命令和一个邮箱参数，额外参数直接视为格式错误。"""

    parts = str(content or "").strip().split()
    if len(parts) != 2:
        return (parts[0] if parts else ""), ""
    command = parts[0].lstrip("/#")
    email = parts[1].strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return command, ""
    return command, email


def _build_username(prefix: str, email: str, channel_id: int, sender_id: str) -> str:
    """使用绑定内稳定身份生成不超过 20 位的确定性 NewAPI 用户名。"""

    digest = hashlib.sha256(
        f"{email}:{int(channel_id)}:{sender_id}".encode("utf-8")
    ).hexdigest()[: max(8, 20 - len(prefix))]
    return f"{prefix}{digest}"[:20]


def _build_display_name(member: ChatroomMember, email: str) -> str:
    """用群名片或昵称生成保守的 NewAPI 展示名。"""

    source = str(member.display_name or member.nickname or email.split("@", 1)[0]).strip()
    cleaned = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]", "", source)
    return (cleaned or "groupuser")[:20]


def _generate_password(length: int = 16) -> str:
    """生成包含大小写字母和数字的内存初始密码。"""

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(12, min(length, 20))))


def _mask_email(email: str) -> str:
    """群回复只保留邮箱首字符和域名。"""

    local, separator, domain = str(email or "").partition("@")
    if not separator or not local or not domain:
        return "已绑定邮箱"
    return f"{local[0]}***@{domain}"


def _newapi_configured(config: CouponConfig) -> bool:
    """检查 NewAPI 管理端和明确套餐 ID。"""

    return bool(
        config.base_url
        and config.admin_access_token
        and config.admin_user_id > 0
        and config.subscription_plan_id > 0
    )


def _notification_configured(config: CouponConfig) -> bool:
    """已有账号允许群通知；自动开户必须完整配置租户邮件发送。"""

    if not config.auto_create_user:
        return True
    return bool(
        config.notify_mode == "email"
        and config.smtp_host
        and config.smtp_port > 0
        and config.smtp_username
        and config.smtp_password
        and config.smtp_from_email
    )


def _load_config(raw: Mapping[str, Any]) -> CouponConfig:
    """读取并验证会产生外部账号和套餐成本的全部租户配置。"""

    notify_mode = str(raw.get("notify_mode") or "group").strip().lower()
    if notify_mode not in {"group", "email"}:
        raise ValueError("notify_mode 只能是 group 或 email")
    username_prefix = re.sub(r"[^A-Za-z0-9]", "", str(raw.get("username_prefix") or "mbs"))[:8]
    if not username_prefix:
        raise ValueError("username_prefix 必须包含字母或数字")
    return CouponConfig(
        base_url=str(raw.get("base_url") or "").strip(),
        admin_access_token=str(raw.get("admin_access_token") or "").strip(),
        admin_user_id=_bounded_int(raw, "admin_user_id", 0, minimum=0, maximum=2_147_483_647),
        subscription_plan_id=_bounded_int(
            raw,
            "subscription_plan_id",
            0,
            minimum=0,
            maximum=2_147_483_647,
        ),
        auto_create_user=bool(raw.get("auto_create_user", False)),
        username_prefix=username_prefix,
        request_timeout_seconds=_bounded_int(
            raw,
            "request_timeout_seconds",
            20,
            minimum=5,
            maximum=120,
        ),
        claim_rate_limit=_bounded_int(raw, "claim_rate_limit", 3, minimum=1, maximum=100),
        claim_rate_window_seconds=_bounded_int(
            raw,
            "claim_rate_window_seconds",
            3600,
            minimum=1,
            maximum=86_400,
        ),
        notify_mode=notify_mode,
        smtp_host=str(raw.get("smtp_host") or "").strip(),
        smtp_port=_bounded_int(raw, "smtp_port", 465, minimum=1, maximum=65_535),
        smtp_username=str(raw.get("smtp_username") or "").strip(),
        smtp_password=str(raw.get("smtp_password") or "").strip(),
        smtp_from_email=str(raw.get("smtp_from_email") or "").strip(),
        smtp_use_ssl=bool(raw.get("smtp_use_ssl", True)),
        email_subject=str(raw.get("email_subject") or "NewAPI 套餐已开通").strip()[:200],
    )


def _bounded_int(
    raw: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """读取有明确边界的整数配置。"""

    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _positive_int(value: Any, *, label: str) -> int:
    """严格解析上游正整数主键和时间戳。"""

    if isinstance(value, bool) or value is None:
        raise CouponError(f"{label}无效")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise CouponError(f"{label}无效") from exc
    if normalized <= 0:
        raise CouponError(f"{label}必须为正整数")
    return normalized


def _text_result(message: str, provider_status: str, **output: Any) -> PluginExecutionResult:
    """构造不包含初始密码和完整邮箱的标准成功回复。"""

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


def _error_result(message: str, provider_status: str, **output: Any) -> PluginExecutionResult:
    """构造稳定错误回复，不暴露第三方响应或租户凭证。"""

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
