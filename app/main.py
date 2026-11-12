import json
import subprocess
import time as time_module
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.routing import Match

from app.api import channel_inbound_router, channel_outbound_router, wechat_webridge_router, customer_operations_router, guga_agent_router, open_api_router, open_media_router, open_messages_router, open_resources_router, payment_webhook_router, provider_router, tool_api_router, workwechat_provider_cloud_router
from app.api.local_proxy_connector import router as local_proxy_connector_router
from app.core.logging import configure_logging, get_logger
from app.core.settings import settings
from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import require_tenant_id, tenant_scope
from app.core.tenant_db import open_tenant_session, platform_session, tenant_session
from app.core.time import SHANGHAI_TZ, ensure_shanghai_datetime, format_shanghai_time, hour_bucket_shanghai, now_shanghai
from app.models.entities import MomentsMedia, MomentsPost, AlertRecord, AuditLog, Bot, BotPlugin, Channel, Chatroom, Conversation, ErrorLog, HealthCheckRecord, Message, MessageActivityHourly, MessageAttachment, Plugin, PluginActivityHourly, PluginExecutionLog, RawWebhookEvent, TaskJob, WorkWechatContact
from app.models.saas import BotBinding
from app.schemas.forms import BotPluginBindCreate, BotUpdate, ChannelUpdate
from app.services.auth import validate_password_change
from app.services.account_connections import (
    AccountConnectionError,
    begin_account_connection,
    finalize_account_connection,
    verify_account_connection,
)
from app.services.async_workers import start_background_workers, stop_background_workers
from app.services.bot_entitlements import BotEntitlementError, get_bot_quota
from app.services.channel_adapters import dispatch_outbound_message, get_channel_adapter
from app.services.channel_catalog import (
    ChannelConfigurationError,
    get_channel_type_definition,
    list_supported_channel_types,
    require_supported_channel_type,
    serialize_channel_type_definitions,
    validate_enabled_channel_configuration,
)
from app.services.channel_events import ingest_inbound_event
from app.services.request_metrics import record_request
from app.services.channel_labels import get_channel_type_label
from app.services.account_identity import account_identity, bot_identity
from app.services import ai_dispatch
from app.services.channel_deletion import ChannelDeletionError, delete_channel_account
from app.services.chatroom_registry import (
    GROUP_MODE_COLLECT_ONLY,
    GROUP_MODE_OFF,
    GROUP_MODE_OPTIONS,
    GROUP_MODE_RUN,
    is_collection_off,
    set_chatroom_management_status,
    upsert_manual_chatroom,
)
from app.services.login_proxy_pool import (
    LoginProxyPoolError,
    build_login_proxy_session_summary,
    create_login_proxy_session,
    get_active_login_proxy_session,
    list_available_proxy_regions,
)
from app.services.local_proxy_relay import local_proxy_relay_server
from app.services.local_proxy_connector import list_online_proxy_connectors
from app.services.crud import (
    bind_plugin,
    delete_bot,
    get_binding,
    get_bot,
    get_channel,
    toggle_binding,
    unbind_plugin,
    toggle_bot,
    toggle_channel,
    update_binding_config,
    update_bot,
    update_channel,
)
from app.services.ops import (
    create_alert,
    record_audit,
    record_health_check,
)
from app.services.group_features.registry import get_feature as get_group_feature
from app.services.group_features.view import build_feature_cards, save_from_form as save_group_feature_form, toggle_flag as toggle_group_feature_flag
from app.services.plugin_directory import GROUP_SCOPED_CHANNEL_TYPES, sync_plugins_from_directory
from app.services.plugin_governance import blocked_plugin_codes, tenant_visible_plugins
from app.services.plugin_schemas import (
    build_plugin_config_view,
    deep_merge_config,
    diff_config_override,
    dump_config_json,
    get_plugin_config_schema,
    merge_schema_config,
    parse_config_json,
)
from app.services.plugin_runtime import (
    PluginExecutionContext,
    execute_binding_action,
    execute_binding_message_action,
    get_plugin_runtime_meta,
)
from app.services.plugin_secrets import decrypt_plugin_secret_config
from app.services.wechat_ipad.login_flow import (
    WechatIpadLoginFlowError,
    cancel_login_with_proxy_session,
    poll_login_with_proxy_session,
    start_login_with_proxy_session,
)
from app.services.provider_nodes import (
    ProviderNodeError,
    list_provider_nodes,
    validate_channel_executor_binding,
)
from app.services.init_db import database_readiness, init_db_with_seed
from app.services.installer import (
    is_setup_required,
    provision_platform_admin,
)
from app.services.runtime import (
    build_binding_dispatch_view,
    build_binding_runtime_view,
    compute_binding_runtime,
    persist_binding_execution,
    reload_binding,
    run_bot_status_check,
    run_channel_health_check,
)
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job
from app.services.saas_auth import TenantPrincipal, has_tenant_role, load_platform_principal, load_tenant_principal
from app.services.tenant_admin import TenantPolicyError, require_tenant_mutation_allowed
from app.services.workwechat_cloud_poll import issue_channel_authorization_code, is_cloud_poll_enabled, load_cloud_poll_config, notify_cloud_job_enqueued, resolve_outbound_job_type
from app.services.security import (
    build_telegram_webhook_secret,
    decrypt_secret,
    mask_secret,
    verify_telegram_webhook_secret,
    verify_webhook_signature,
)
from app.services.wechat_ipad import (
    build_wechat_ipad_chatroom_detail,
    build_wechat_ipad_chatroom_list,
    build_wechat_ipad_contact_list,
    build_wechat_ipad_group_summary,
    build_wechat_ipad_overview,
    pull_wechat_ipad_messages,
    record_wechat_ipad_receive_event,
    record_wechat_ipad_send_event,
    run_wechat_ipad_action,
    reset_wechat_ipad_chatroom_scope,
    resolve_wechat_ipad_qrcode_image,
    sync_wechat_ipad_chatrooms,
    sync_wechat_ipad_contacts,
    update_wechat_ipad_chatroom_scope_config_override,
    update_wechat_ipad_chatroom_scope_enabled,
)
from app.services.wechat_winauto_chatrooms import (
    build_wechat_winauto_chatroom_detail,
    build_wechat_winauto_chatroom_list,
    reset_wechat_winauto_chatroom_scope,
    sync_wechat_winauto_chatrooms,
    update_wechat_winauto_chatroom_scope_config_override,
    update_wechat_winauto_chatroom_scope_enabled,
)
from app.services.directory_pages import build_chatroom_directory, build_chatroom_plugin_directory, build_contact_directory, channel_options, chatroom_plugin_options
from app.services.wechat_ipad.chatrooms import update_group_plugin_scope_enabled
from app.services.points_admin import adjust_points, build_points_accounts, build_points_ledger, group_options
from app.services.wechat_webridge.chatrooms import build_webridge_chatroom_detail
from app.services.wechat_webridge.dashboard import build_webridge_dashboard, build_webridge_device_panel, build_webridge_live_summary
from app.services.identity_directory import IdentityDirectory
from app.services import message_media, message_view, moments_media, payment_ledger
from app.services.wechat_webridge import moments as webridge_moments
from app.services.wechat_webridge.access import account_info as webridge_account_info, connection_url as webridge_connection_url, initialize_connection as initialize_webridge_connection
from app.services.guga_agent.access import account_info as guga_account_info, connection_url as guga_connection_url, initialize_connection as initialize_guga_connection
from app.services.outbound_files import build_public_base_url
from app.services.wechat_webridge.service import enqueue_command, reap_stale_commands
from app.services.workwechat_chatrooms import (
    build_workwechat_chatroom_detail,
    build_workwechat_chatroom_list,
    reset_workwechat_chatroom_scope,
    sync_workwechat_chatroom_members,
    sync_workwechat_chatroom_profile_from_list,
    sync_workwechat_chatrooms,
    update_workwechat_chatroom_scope_config_override,
    update_workwechat_chatroom_scope_enabled,
)
from app.services.workwechat_contacts import (
    build_workwechat_contact_detail,
    build_workwechat_contact_list,
    build_workwechat_contact_summary,
    mark_workwechat_contact_deleted,
    restore_workwechat_contact,
    sync_workwechat_contacts,
    upsert_workwechat_contact,
)
from app.web import billing_router, developer_docs_router, group_triggers_router, open_api_apps_router, platform_admin_router, platform_auth_router, platform_billing_router, platform_operations_router, platform_plugins_router, provider_nodes_router, public_site_router, tenant_admin_router, tenant_auth_router
from app.web.auth_common import (
    PLATFORM_LOGIN_PATH,
    PLATFORM_SESSION_COOKIE,
    TENANT_CSRF_COOKIE,
    TENANT_LOGIN_PATH,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    TENANT_SESSION_COOKIE,
    platform_must_change_password_redirect,
    require_tenant_csrf,
    tenant_must_change_password_redirect,
)
from app.web.tenant_navigation import build_tenant_navigation
configure_logging()
logger = get_logger(__name__)

# 应用元数据统一从 Settings 读取，部署时可以覆盖名称，但默认明确标识 SaaS 服务。
app = FastAPI(title=settings.app_name, version="1.0.0")
APP_STARTED_AT = now_shanghai()
APP_ROOT = Path(__file__).resolve().parents[1]

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(tool_api_router)
app.include_router(channel_outbound_router)
app.include_router(channel_inbound_router)
app.include_router(customer_operations_router)
app.include_router(open_api_router)
app.include_router(open_resources_router)
app.include_router(open_messages_router)
app.include_router(open_media_router)
app.include_router(payment_webhook_router)
app.include_router(workwechat_provider_cloud_router)
app.include_router(wechat_webridge_router)
app.include_router(guga_agent_router)
app.include_router(local_proxy_connector_router)
app.include_router(provider_router)
# 平台和租户认证使用独立 Router 与 Cookie，主应用只负责统一注册和请求身份恢复。
app.include_router(public_site_router)
app.include_router(developer_docs_router)
app.include_router(group_triggers_router)
app.include_router(tenant_auth_router)
app.include_router(platform_auth_router)
app.include_router(billing_router)
app.include_router(platform_admin_router)
app.include_router(platform_billing_router)
app.include_router(platform_operations_router)
app.include_router(platform_plugins_router)
app.include_router(tenant_admin_router)
app.include_router(open_api_apps_router)
app.include_router(provider_nodes_router)
templates = Jinja2Templates(directory="app/templates")
BUILTIN_CHANNEL_TYPES = list_supported_channel_types()
# 支持群级插件启停、配置覆盖和纳管状态的渠道；微桥复用企微的群级作用域实现。
GROUP_SCOPE_CHANNEL_TYPES = GROUP_SCOPED_CHANNEL_TYPES
CHANNEL_TYPE_FORM_SCHEMA = serialize_channel_type_definitions()
CHANNEL_TYPE_FORM_SCHEMA_BY_CODE = {item["code"]: item for item in CHANNEL_TYPE_FORM_SCHEMA}
logger.info("application module initialized", extra={"app_env": settings.app_env, "log_dir": settings.log_dir})

# 机器接口通过自身签名或一次性令牌完成鉴权，不能被租户页面 Cookie 中间件重定向。
# 将公开前缀集中定义，便于支付回调、开放 API 和 Provider 接口共同维护边界。
PUBLIC_MACHINE_PREFIXES = (
    "/api/open/",
    "/api/payment/",
    "/api/workwechat/customer-operations",
    "/api/workwechat/provider",
    "/api/provider",
    "/api/wechat-webridge",
    "/api/guga-agent",
    "/outbound-files/",
    "/inbound-files/",
    "/webhooks/",
    "/static/",
)


def log_startup_banner() -> None:
    # 在 INFO 级别输出显眼的系统启动完成标识，便于从日志中快速定位启动成功时间点。
    banner_lines = [
        " __  __ _   _ _   _____ ___    ____   ___ _____     _    ____  __  __ ___ _   _ ",
        "|  \\/  | | | | | |_   _|_ _|  | __ ) / _ \\_   _|   / \\  |  _ \\|  \\/  |_ _| \\ | |",
        "| |\\/| | | | | |   | |  | |   |  _ \\| | | || |    / _ \\ | | | | |\\/| || ||  \\| |",
        "| |  | | |_| | |___| |  | |   | |_) | |_| || |   / ___ \\| |_| | |  | || || |\\  |",
        "|_|  |_|\\___/|_____|_| |___|  |____/ \\___/ |_|  /_/   \\_\\____/|_|  |_|___|_| \\_|",
    ]
    logger.info("========== MULTI BOT SAAS STARTUP COMPLETE ==========")
    for line in banner_lines:
        logger.info(line)
    logger.info(
        "MULTI BOT SAAS 已启动并完成初始化",
        extra={
            "app_env": settings.app_env,
            "started_at": APP_STARTED_AT.isoformat(),
        },
    )
    logger.info("======================================================")


@app.on_event("startup")
async def startup_event() -> None:
    db = SessionLocal()
    try:
        init_db_with_seed(db)
    except Exception:
        # 数据库未迁移到 head 时应用仍保留 liveness/readiness 探针，但绝不启动 Worker。
        logger.exception("application started in database-not-ready mode")
        return
    finally:
        db.close()
    start_background_workers()
    await local_proxy_relay_server.start()
    log_startup_banner()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await local_proxy_relay_server.stop()
    stop_background_workers()


def format_beijing_time(value):
    return format_shanghai_time(value)


def format_uptime(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        started_at = ensure_shanghai_datetime(value)
    else:
        return str(value)
    delta = max(now_shanghai() - started_at, timedelta())
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts[:3])


def get_cron_probe_snapshot() -> dict[str, str]:
    probe_script = APP_ROOT / "scripts" / "fulcrum_cron_probe.sh"
    if not probe_script.exists():
        return {"recommended_state": "unknown", "probe_error": "missing_script"}
    try:
        result = subprocess.run(
            [str(probe_script)],
            cwd=APP_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return {"recommended_state": "unknown", "probe_error": str(exc)}

    snapshot: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        snapshot[key.strip()] = value.strip()

    if result.returncode != 0:
        snapshot.setdefault("recommended_state", "unknown")
        stderr = (result.stderr or "").strip()
        snapshot["probe_error"] = stderr or f"probe_exit_{result.returncode}"

    snapshot.setdefault("recommended_state", "unknown")
    return snapshot


templates.env.filters["bj_time"] = format_beijing_time
templates.env.filters["uptime"] = format_uptime
templates.env.filters["channel_type_label"] = get_channel_type_label
templates.env.filters["message_display"] = lambda value, message_type="": format_message_display(value, message_type)


def build_url(path: str, **params: object) -> str:
    filtered = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, int) and value == 0:
            continue
        filtered[key] = value
    if not filtered:
        return path
    return f"{path}?{urlencode(filtered)}"


def current_path_with_query(request: Request) -> str:
    return f"{request.url.path}?{request.url.query}" if request.url.query else request.url.path


def safe_return_to(value: str | None, fallback: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return fallback
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def build_context_href(path: str, return_to: str = "") -> str:
    """生成站内链接；机器人环境已删除，只保留必要的返回地址。"""

    return build_url(path, return_to=return_to)


templates.env.globals["context_href"] = build_context_href


def build_module_links() -> dict[str, str]:
    """集中提供不携带旧环境参数的模块入口。"""

    return {
        "dashboard": "/dashboard",
        "bots": "/bots",
        "channels": "/channels",
        "plugins": "/plugins",
        "plugin_runtime": "/plugins/runtime",
        "messages": "/messages",
    }


def detail_fallback(path: str) -> str:
    return path


def filtered_bots_query(db: Session):
    return db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()


def build_bot_name_map(bots: list[Bot]) -> dict[int, str]:
    return {int(bot.id): str(bot.name or "").strip() or f"Bot #{bot.id}" for bot in bots}


def build_channel_chatroom_summary(list_view: dict[str, Any] | None) -> dict[str, Any] | None:
    if not list_view:
        return None
    items = list(list_view.get("items") or [])
    # 渠道详情页只需要一屏摘要；完整群列表在 /chatrooms 查看。
    recent_chatrooms = items[:8]
    return {
        "total": int(list_view.get("total") or len(items)),
        "active_chatroom_count": int(list_view.get("active_chatroom_count") or 0),
        "pending_chatroom_count": int(list_view.get("pending_chatroom_count") or 0),
        "member_snapshot_chatroom_count": int(list_view.get("member_snapshot_chatroom_count") or 0),
        "binding_total": int(list_view.get("binding_total") or 0),
        "binding_enabled_total": int(list_view.get("binding_enabled_total") or 0),
        "overridden_chatroom_count": int(list_view.get("overridden_chatroom_count") or 0),
        "sync_action": str(list_view.get("sync_action") or ""),
        "sync_button_label": str(list_view.get("sync_button_label") or "刷新群列表"),
        "intro_summary": str(list_view.get("intro_summary") or "基于本地群样本生成群管理摘要。"),
        "empty_state_message": str(list_view.get("empty_state_message") or "当前还没有观测到群消息，先同步群列表或等待群消息进入。"),
        "recent_chatrooms": recent_chatrooms,
    }


def build_workwechat_cloud_poll_config_map(channels: list[Channel]) -> dict[int, dict[str, Any]]:
    # 渠道列表页需要按每个企微渠道判断是否云端 WS/拉取模式；
    # 这里在服务端提前解析 JSON，避免模板里反复解析 adapter_config_json。
    result: dict[int, dict[str, Any]] = {}
    for channel in channels:
        if channel.channel_type == "workwechat":
            result[int(channel.id)] = load_cloud_poll_config(channel)
    return result


def _build_workwechat_contact_form_payload(**values: str) -> dict[str, Any]:
    relation = {
        "owner_userid": str(values.pop("owner_userid", "") or "").strip(),
        "owner_name": str(values.pop("owner_name", "") or "").strip(),
        "remark": str(values.pop("remark", "") or "").strip(),
        "relationship_status": "active",
    }
    payload = {key: str(value or "").strip() for key, value in values.items()}
    payload["source"] = "manual"
    payload["sync_status"] = "active"
    if relation["owner_userid"] or relation["owner_name"] or relation["remark"]:
        payload["follow_user"] = [relation]
    return payload


def filtered_channels_query(db: Session):
    return db.scalars(select(Channel).where(Channel.deleted_at.is_(None)).order_by(Channel.id.desc())).all()


def filtered_bindings_query(db: Session):
    return db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound").order_by(BotPlugin.id.desc())).all()


def render(request: Request, template: str, **context):
    notice = str(context.get("notice", "") or "").strip()
    response_status = int(context.pop("response_status", 200) or 200)
    notice_tone = context.get("notice_tone")
    if not notice_tone:
        notice_tone = "danger" if response_status >= 400 else infer_notice_tone(notice)
    navigation_context = build_tenant_navigation(request.url.path)
    base_context = {
        "current_user": getattr(request.state, "current_user", None),
        # 所有租户页面共用同一个可信 CSRF Cookie；状态变更表单必须把原始值放入
        # 隐藏字段，由对应路由再与数据库会话摘要进行三方校验。
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "logout_action": TENANT_LOGOUT_PATH,
        "password_action": TENANT_PASSWORD_PATH,
        "app_started_at": APP_STARTED_AT,
        "notice_tone": notice_tone,
        "module_links": build_module_links(),
    }
    base_context.update(navigation_context)
    base_context.update(context)
    response = templates.TemplateResponse(request, template, base_context)
    response.status_code = response_status
    return response


def infer_notice_tone(notice: str) -> str:
    text = str(notice or "").strip()
    if not text:
        return "default"
    error_keywords = ("错误", "失败", "异常", "不正确", "不一致", "不能为空", "不存在", "缺少", "无效", "至少", "请先")
    if any(keyword in text for keyword in error_keywords):
        return "danger"
    return "default"


def parse_json_object(raw_text: str) -> tuple[dict[str, object], str]:
    normalized = (raw_text or "").strip()
    if not normalized:
        return {}, ""
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        return {}, f"JSON 解析失败: {exc}"
    if not isinstance(parsed, dict):
        return {}, "JSON 必须是对象。"
    return parsed, ""


def build_group_scope_effective_config(plugin_code: str | None, binding_config_json: str | None, scope_config_json: str | None) -> dict[str, Any]:
    base: dict[str, Any] = {}
    schema = get_plugin_config_schema(plugin_code)
    if schema:
        for field in schema.fields:
            if field.default not in (None, "", []):
                base[field.name] = field.default
    binding_config = parse_config_json(binding_config_json)
    scope_config = parse_config_json(scope_config_json)
    return deep_merge_config(deep_merge_config(base, binding_config), scope_config)


def merge_channel_adapter_config(
    *,
    channel_type: str,
    adapter_config_json: str,
) -> tuple[str, str]:
    """规范化渠道运行配置，并直接删除微信 iPad 已废弃的长期代理节点。"""

    parsed, error = parse_json_object(adapter_config_json)
    if error:
        return "", error
    if str(channel_type or "").strip() == "wechat_ipad":
        # 当前架构只允许 LoginProxySession 保存单次扫码代理，编辑历史账号时同步清理旧数据。
        parsed.pop("proxy", None)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True) if parsed else "", ""


def parse_message_lines(raw_text: str) -> list[str]:
    return [line.strip() for line in (raw_text or "").splitlines() if line.strip()]


def format_message_display(raw_text: object, message_type: object = "") -> str:
    """一行摘要(会话列表、最近消息等):文字取开头,媒体/卡片/引用用 [图片] [引用] 标题 这样的短标签。

    完整内容在消息中心用卡片展示(见 message_view 与 _message_rows.html)。
    """

    return message_view.preview(raw_text, message_type, limit=80)


def _normalize_dt_utc(value: datetime | None) -> datetime:
    return ensure_shanghai_datetime(value)


def _hour_bucket_utc(value: datetime | None = None) -> datetime:
    return hour_bucket_shanghai(value)


def build_time_series_chart(items: list[dict[str, object]], *, value_key: str, tone: str = "primary") -> dict[str, object]:
    values = [max(int(item.get(value_key) or 0), 0) for item in items]
    max_value = max(values) if values else 0
    points: list[str] = []
    bars: list[dict[str, object]] = []
    count = len(values)
    for index, value in enumerate(values):
        x = 0 if count <= 1 else round((index / (count - 1)) * 100, 2)
        y = 100 if max_value <= 0 else round(100 - ((value / max_value) * 100), 2)
        points.append(f"{x},{y}")
        bars.append(
            {
                "label": str(items[index].get("label") or ""),
                "value": value,
                "height_percent": 0 if max_value <= 0 or value <= 0 else max(round((value / max_value) * 100, 2), 3),
            }
        )
    return {
        "max_value": max_value,
        "points": " ".join(points) if points else "",
        "items": bars,
        "tone": tone,
    }


def build_dashboard_trend_series(
    db: Session,
    *,
    hours: int = 24,
) -> dict[str, object]:
    now_bucket = _hour_bucket_utc()
    start_bucket = now_bucket - timedelta(hours=hours - 1)
    message_stmt = select(MessageActivityHourly).where(
        MessageActivityHourly.bucket_start >= start_bucket,
        MessageActivityHourly.bucket_start <= now_bucket,
    )
    plugin_stmt = select(PluginActivityHourly).where(
        PluginActivityHourly.bucket_start >= start_bucket,
        PluginActivityHourly.bucket_start <= now_bucket,
    )
    message_rows = db.scalars(message_stmt.order_by(MessageActivityHourly.bucket_start.asc())).all()
    plugin_rows = db.scalars(plugin_stmt.order_by(PluginActivityHourly.bucket_start.asc())).all()
    logger.info(
        "dashboard trend query window",
        extra={
            "hours": hours,
            "start_bucket": start_bucket.isoformat(),
            "now_bucket": now_bucket.isoformat(),
            "message_hourly_rows": len(message_rows),
            "plugin_hourly_rows": len(plugin_rows),
        },
    )
    message_map: dict[datetime, dict[str, int]] = {}
    for row in message_rows:
        bucket = _normalize_dt_utc(row.bucket_start)
        item = message_map.setdefault(bucket, {"in": 0, "out": 0})
        direction = str(row.direction or "").strip().lower()
        if direction == "out":
            item["out"] += int(row.message_count or 0)
        else:
            item["in"] += int(row.message_count or 0)
    if message_rows:
        logger.info(
            "dashboard message hourly rows",
            extra={
                # 环境筛选已从当前 SaaS 架构移除；日志只记录已受租户会话约束的统计行，
                # 避免有数据时访问已删除变量，导致整个企业总览返回 500。
                "rows": [
                    {
                        "bucket_start": _normalize_dt_utc(row.bucket_start).isoformat(),
                        "bot_id": row.bot_id,
                        "channel_id": row.channel_id,
                        "channel_type": row.channel_type or "",
                        "chat_type": row.chat_type or "",
                        "direction": row.direction or "",
                        "message_count": int(row.message_count or 0),
                    }
                    for row in message_rows
                ],
            },
        )
    plugin_map: dict[datetime, dict[str, int]] = {}
    for row in plugin_rows:
        bucket = _normalize_dt_utc(row.bucket_start)
        item = plugin_map.setdefault(bucket, {"trigger": 0, "success": 0, "failed": 0})
        item["trigger"] += int(row.attempted_count or 0)
        item["success"] += int(row.success_count or 0)
        failures = int(row.failed_count or 0) + int(row.timeout_count or 0)
        item["failed"] += failures
    if not message_map:
        raw_message_stmt = (
            select(
                Message.created_at,
                Message.direction,
            )
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.created_at >= start_bucket, Message.created_at <= now_bucket + timedelta(hours=1))
        )
        raw_message_rows = db.execute(raw_message_stmt).all()
        logger.info(
            "dashboard trend fallback to raw messages",
            extra={
                "raw_message_rows": len(raw_message_rows),
            },
        )
        for created_at, direction in raw_message_rows:
            bucket = _hour_bucket_utc(created_at)
            item = message_map.setdefault(bucket, {"in": 0, "out": 0})
            normalized_direction = str(direction or "").strip().lower()
            if normalized_direction == "out":
                item["out"] += 1
            else:
                item["in"] += 1
    if not plugin_map:
        raw_plugin_stmt = (
            select(
                PluginExecutionLog.created_at,
                PluginExecutionLog.ok,
                PluginExecutionLog.status,
                PluginExecutionLog.attempted,
            )
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= start_bucket,
                PluginExecutionLog.created_at <= now_bucket + timedelta(hours=1),
                PluginExecutionLog.attempted.is_(True),
            )
        )
        for created_at, ok, status, attempted in db.execute(raw_plugin_stmt).all():
            if not attempted:
                continue
            bucket = _hour_bucket_utc(created_at)
            item = plugin_map.setdefault(bucket, {"trigger": 0, "success": 0, "failed": 0})
            item["trigger"] += 1
            if ok:
                item["success"] += 1
            if str(status or "").strip().lower() in {"error", "timeout", "outbound_failed"}:
                item["failed"] += 1
    timeline: list[dict[str, object]] = []
    for offset in range(hours):
        bucket = start_bucket + timedelta(hours=offset)
        message_item = message_map.get(bucket, {"in": 0, "out": 0})
        plugin_item = plugin_map.get(bucket, {"trigger": 0, "success": 0, "failed": 0})
        timeline.append(
            {
                "bucket": bucket.isoformat(),
                "label": bucket.astimezone(SHANGHAI_TZ).strftime("%H:%M"),
                "inbound": int(message_item["in"]),
                "outbound": int(message_item["out"]),
                "plugin_trigger": int(plugin_item["trigger"]),
                "plugin_success": int(plugin_item["success"]),
                "plugin_failed": int(plugin_item["failed"]),
            }
        )
    logger.info(
        "dashboard trend summary",
        extra={
            "inbound_max": max((int(item["inbound"]) for item in timeline), default=0),
            "outbound_max": max((int(item["outbound"]) for item in timeline), default=0),
            "plugin_trigger_max": max((int(item["plugin_trigger"]) for item in timeline), default=0),
            "nonzero_inbound_buckets": [
                {
                    "label": item["label"],
                    "bucket": item["bucket"],
                    "inbound": item["inbound"],
                    "outbound": item["outbound"],
                }
                for item in timeline
                if int(item["inbound"]) > 0 or int(item["outbound"]) > 0
            ],
        },
    )
    return {
        "items": timeline,
        "inbound_chart": build_time_series_chart(timeline, value_key="inbound", tone="primary"),
        "plugin_chart": build_time_series_chart(timeline, value_key="plugin_trigger", tone="success"),
    }


def build_dashboard_rankings(db: Session) -> dict[str, list[dict[str, object]]]:
    since_24h = now_shanghai() - timedelta(hours=24)

    bot_rows = db.execute(
        select(Bot.id, Bot.name, func.count(Message.id).label("message_count"))
        .join(Conversation, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.created_at >= since_24h, Message.direction == "in", Bot.deleted_at.is_(None))
        .group_by(Bot.id, Bot.name)
        .order_by(func.count(Message.id).desc(), Bot.id.asc())
    ).all()
    chatroom_rows = db.execute(
        select(
            Conversation.channel_id,
            Conversation.external_id,
            Conversation.title,
            func.count(Message.id).label("message_count"),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
        .where(
            Conversation.chat_type == "group",
            Message.direction == "in",
            Message.created_at >= since_24h,
        )
        .group_by(Conversation.channel_id, Conversation.external_id, Conversation.title)
        .order_by(func.count(Message.id).desc(), Conversation.external_id.asc())
        .limit(10)
    ).all()

    user_rows = db.execute(
        select(
            Conversation.channel_id,
            Message.sender_id,
            func.max(Message.sender_name).label("sender_name"),
            func.count(Message.id).label("message_count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
        .where(
            Message.direction == "in",
            Message.sender_id.is_not(None),
            Message.created_at >= since_24h,
        )
        # 同一个 wxid 只在所属渠道内才有意义，按渠道+发送者分组才能正确解析昵称头像。
        .group_by(Conversation.channel_id, Message.sender_id)
        .order_by(func.count(Message.id).desc(), Message.sender_id.asc())
        .limit(10)
    ).all()
    # 活跃用户统一解析昵称头像，wxid 只作为弱信息显示在第二行。
    identity_directory = IdentityDirectory(db)
    room_ids_by_channel: dict[int, set[str]] = {}
    for row in chatroom_rows:
        room_ids_by_channel.setdefault(int(row.channel_id or 0), set()).add(str(row.external_id))
    for channel_id_value, room_ids in room_ids_by_channel.items():
        identity_directory.preload(channel_id_value, room_ids)
    user_ids_by_channel: dict[int, set[str]] = {}
    for row in user_rows:
        user_ids_by_channel.setdefault(int(row.channel_id or 0), set()).add(str(row.sender_id))
    for channel_id_value, sender_ids in user_ids_by_channel.items():
        identity_directory.preload(channel_id_value, sender_ids)

    plugin_rows = db.execute(
        select(
            PluginExecutionLog.plugin_code,
            func.count(PluginExecutionLog.id).label("trigger_count"),
            func.sum(PluginExecutionLog.outbound_count).label("outbound_count"),
        )
        .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
        .where(
            PluginExecutionLog.created_at >= since_24h,
            PluginExecutionLog.attempted.is_(True),
        )
        .group_by(PluginExecutionLog.plugin_code)
        .order_by(func.count(PluginExecutionLog.id).desc(), PluginExecutionLog.plugin_code.asc())
        .limit(10)
    ).all()

    return {
        "bots": [
            {"label": str(row.name or f"Bot #{row.id}"), "value": int(row.message_count or 0), "href": f"/bots/{int(row.id)}"}
            for row in bot_rows[:10]
        ],
        "chatrooms": [
            {
                # 群名优先取群档案，其次会话标题；两者都没有时才退回群 ID。
                "identity": identity_directory.get(int(row.channel_id or 0), row.external_id, row.title),
                "value": int(row.message_count or 0),
            }
            for row in chatroom_rows
        ],
        "users": [
            {
                "identity": identity_directory.get(int(row.channel_id or 0), row.sender_id, row.sender_name),
                "value": int(row.message_count or 0),
            }
            for row in user_rows
        ],
        "plugins": [
            {"label": str(row.plugin_code or "-"), "value": int(row.trigger_count or 0), "sub_label": f"自动出站 {int(row.outbound_count or 0)}"}
            for row in plugin_rows
        ],
    }


def build_wechat_ipad_flow_payload(db: Session, channel: Channel) -> dict[str, object]:
    """把协议运行态收口为扫码页面唯一需要的四类业务状态。"""

    overview = build_wechat_ipad_overview(channel)
    login_state = str(overview.get("login_state") or "").strip()
    qrcode_status = str(overview.get("qrcode_status") or "").strip()
    if login_state == "online" or qrcode_status == "success":
        status = "success"
        status_label = "登录成功"
        message = "账号连接成功，正在进入下一步"
        should_poll = False
    elif qrcode_status in {"scanned", "confirmed"}:
        status = "phone_confirm"
        status_label = "请在手机确认"
        message = "已识别扫码，请在手机微信中确认登录"
        should_poll = True
    elif qrcode_status == "waiting_scan" and overview.get("qrcode_image_url"):
        status = "waiting_scan"
        status_label = "等待扫码"
        message = "请使用手机微信扫描二维码"
        should_poll = True
    elif qrcode_status in {"failed", "expired", "unavailable"}:
        status = "failed"
        status_label = "登录未完成"
        message = "本次登录未完成，请重新开始"
        should_poll = False
    else:
        status = "preparing"
        status_label = "准备二维码"
        message = "正在准备安全登录二维码"
        should_poll = True
    network = build_login_proxy_session_summary(db, channel_id=int(channel.id))
    return {
        "ok": True,
        "status": status,
        "status_label": status_label,
        "message": message,
        "qrcode_image_url": str(overview.get("qrcode_image_url") or ""),
        "should_poll": should_poll,
        "network_label": str(network.get("network_label") or ""),
    }


def get_session() -> Session:
    """返回绑定当前已认证租户的业务会话。"""

    return open_tenant_session(TenantSessionLocal)


def get_platform_session() -> Session:
    """返回显式平台会话；结构校验只在应用启动阶段执行一次。"""

    return SessionLocal()


def require_user(request: Request) -> TenantPrincipal:
    """返回中间件从 SaaS 数据库会话恢复的可信租户身份。"""

    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def enforce_role(request: Request, required_role: str) -> TenantPrincipal:
    """兼容旧业务路由的角色名称，并在服务端按新租户角色层级强制鉴权。"""

    user = require_user(request)
    # 现有业务路由仍使用 admin/ops/readonly 三档名称；在完成页面租户化前统一映射，
    # 避免把客户端展示角色或旧 AdminUser Cookie 当成新 SaaS 权限依据。
    tenant_role = {
        "admin": "tenant_admin",
        "ops": "operator",
        "readonly": "readonly",
    }.get(required_role, required_role)
    if not has_tenant_role(user, tenant_role):
        raise HTTPException(status_code=403, detail="Permission denied")
    return user


def parse_date_input(value: str) -> date | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def parse_log_timestamp(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            continue
    return None


def ensure_utc_datetime(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return ensure_shanghai_datetime(value)


def level_rank(level: str) -> int:
    mapping = {"INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    return mapping.get(level.upper(), 0)


def build_binding_schema_cards(bindings: list[BotPlugin]) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for binding in bindings:
        plugin_code = binding.plugin.code if binding.plugin else None
        # 页面只需要知道哪些敏感字段已经配置。服务端在可信绑定范围内解密后仅传
        # 布尔状态给模板，密钥原文不会进入 HTML、JSON 预览或浏览器表单值。
        secret_config = decrypt_plugin_secret_config(
            tenant_id=int(binding.tenant_id),
            binding_id=int(binding.id),
            encrypted=binding.secret_config_encrypted,
        )
        config_view = build_plugin_config_view(
            plugin_code,
            binding.config_json,
            secret_config=secret_config,
        )
        cards.append(
            {
                "binding": binding,
                "config_view": config_view,
                "has_schema": config_view["mode"] == "schema",
                "runtime_info": get_plugin_runtime_meta(plugin_code),
                "runtime_view": build_binding_runtime_view(binding),
            }
        )
    return cards


def build_binding_schema_cards_with_dispatch(db: Session, bindings: list[BotPlugin]) -> list[dict[str, object]]:
    cards = build_binding_schema_cards(bindings)
    for card, binding in zip(cards, bindings):
        card["dispatch_view"] = build_binding_dispatch_view(db, binding)
    return cards


def build_plugin_runtime_summary(plugins: list[Plugin], bindings: list[BotPlugin]) -> dict[str, int]:
    runtime_map = {plugin.code: get_plugin_runtime_meta(plugin.code) for plugin in plugins}
    functional_plugins = [plugin for plugin in plugins if runtime_map.get(plugin.code, {}).get("plugin_category") == "functional"]
    migrated_functional = [
        plugin for plugin in functional_plugins if runtime_map.get(plugin.code, {}).get("migration_state") == "migrated"
    ]
    executable_functional = [
        plugin for plugin in migrated_functional if runtime_map.get(plugin.code, {}).get("supports_execution")
    ]
    active_bindings = [binding for binding in bindings if binding.status == "enabled"]
    invalid_manifests = [plugin for plugin in plugins if runtime_map.get(plugin.code, {}).get("manifest_valid") is False]
    warning_manifests = [
        plugin
        for plugin in plugins
        if runtime_map.get(plugin.code, {}).get("manifest_valid") is not False
        and runtime_map.get(plugin.code, {}).get("manifest_warnings")
    ]
    return {
        "functional_total": len(functional_plugins),
        "functional_migrated": len(migrated_functional),
        "functional_executable": len(executable_functional),
        "bindings_total": len(bindings),
        "bindings_active": len(active_bindings),
        "manifest_invalid": len(invalid_manifests),
        "manifest_warning": len(warning_manifests),
    }


def build_plugin_toggle_matrix(
    bots: list[Bot],
    plugins: list[Plugin],
    bindings: list[BotPlugin],
    plugin_runtime_map: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    binding_map = {(binding.bot_id, binding.plugin_id): binding for binding in bindings if binding.status != "unbound"}
    # 插件开关是实际业务入口，只展示已经验证可执行的目录插件。损坏 manifest
    # 或缺少真实入口的镜像仍留在下方诊断清单，但不能被租户绑定或启用。
    executable_plugins = [
        plugin
        for plugin in plugins
        if plugin_runtime_map.get(plugin.code, {}).get("migration_state") == "migrated"
        and bool(plugin_runtime_map.get(plugin.code, {}).get("supports_execution"))
    ]
    rows: list[dict[str, object]] = []
    for bot in bots:
        plugin_items: list[dict[str, object]] = []
        enabled_count = 0
        for plugin in executable_plugins:
            runtime_info = plugin_runtime_map.get(plugin.code, {})
            binding = binding_map.get((bot.id, plugin.id))
            is_enabled = bool(binding and str(binding.status or "").strip() == "enabled")
            if is_enabled:
                enabled_count += 1
            plugin_items.append(
                {
                    "plugin_id": plugin.id,
                    "plugin_code": plugin.code,
                    "plugin_name": plugin.display_name,
                    "binding_id": binding.id if binding else None,
                    "is_bound": binding is not None,
                    "is_enabled": is_enabled,
                    "status_label": "已启用" if is_enabled else "已停用",
                    "status_tone": "success" if is_enabled else "muted",
                    "binding_status": str(binding.status or "").strip() if binding else "unbound",
                    "runtime_status": str(binding.runtime_status or "").strip() if binding else "not_bound",
                    "supports_execution": bool(runtime_info.get("supports_execution")),
                    "manifest_valid": runtime_info.get("manifest_valid"),
                }
            )
        rows.append(
            {
                "bot_id": bot.id,
                "bot_name": bot.name,
                "enabled_count": enabled_count,
                "total_count": len(plugin_items),
                "items": plugin_items,
            }
        )
    return rows


def format_setup_database_error(error: str) -> str:
    normalized = str(error or "").strip()
    lowered = normalized.lower()
    if "unknown database" in lowered:
        return f"MariaDB 连接失败：数据库不存在，请先执行建库命令。原始错误：{normalized}"
    if "access denied" in lowered:
        return f"MariaDB 连接失败：账号或密码不正确，或当前用户没有访问该库的权限。原始错误：{normalized}"
    if "can't connect" in lowered or "connection refused" in lowered:
        return f"MariaDB 连接失败：无法连到数据库主机，请检查主机、端口和服务状态。原始错误：{normalized}"
    return f"MariaDB 连接失败：{normalized}"


def plugin_matches_filters(
    plugin: Plugin,
    runtime_info: dict[str, object],
    *,
    keyword: str = "",
    plugin_type: str = "",
    category: str = "",
    migration_state: str = "",
    execution_state: str = "",
    manifest_state: str = "",
) -> bool:
    normalized_keyword = keyword.strip().lower()
    if normalized_keyword:
        haystack = " ".join(
            [
                str(plugin.code or ""),
                str(plugin.display_name or ""),
                str(plugin.description or ""),
                str(plugin.author or ""),
                str(plugin.plugin_type or ""),
                str(runtime_info.get("plugin_category") or ""),
                str(runtime_info.get("migration_state") or ""),
            ]
        ).lower()
        if normalized_keyword not in haystack:
            return False

    if plugin_type and (plugin.plugin_type or "") != plugin_type:
        return False
    if category and str(runtime_info.get("plugin_category") or "") != category:
        return False
    if migration_state and str(runtime_info.get("migration_state") or "") != migration_state:
        return False

    supports_execution = bool(runtime_info.get("supports_execution"))
    if execution_state == "executable" and not supports_execution:
        return False
    if execution_state == "non-executable" and supports_execution:
        return False

    manifest_valid = runtime_info.get("manifest_valid")
    has_warnings = bool(runtime_info.get("manifest_warnings"))
    if manifest_state == "valid" and manifest_valid is not True:
        return False
    if manifest_state == "invalid" and manifest_valid is not False:
        return False
    if manifest_state == "warning" and not (manifest_valid is not False and has_warnings):
        return False

    return True


def build_plugin_filter_options(
    plugins: list[Plugin], plugin_runtime_map: dict[str, dict[str, object]]
) -> dict[str, list[tuple[str, str]]]:
    plugin_types = sorted({(plugin.plugin_type or "").strip() for plugin in plugins if (plugin.plugin_type or "").strip()})
    categories = sorted(
        {
            str(plugin_runtime_map.get(plugin.code, {}).get("plugin_category") or "").strip()
            for plugin in plugins
            if str(plugin_runtime_map.get(plugin.code, {}).get("plugin_category") or "").strip()
        }
    )
    migration_states = sorted(
        {
            str(plugin_runtime_map.get(plugin.code, {}).get("migration_state") or "").strip()
            for plugin in plugins
            if str(plugin_runtime_map.get(plugin.code, {}).get("migration_state") or "").strip()
        }
    )
    return {
        "plugin_types": [("", "全部")] + [(item, item) for item in plugin_types],
        "categories": [("", "全部")] + [(item, item) for item in categories],
        "migration_states": [("", "全部")] + [(item, item) for item in migration_states],
        "execution_states": [("", "全部"), ("executable", "可执行"), ("non-executable", "不可执行")],
        "manifest_states": [("", "全部"), ("valid", "有效"), ("invalid", "无效"), ("warning", "警告")],
    }


def build_channel_health_timeline(db: Session, channel_id: int) -> dict[str, object]:
    latest_checks = db.scalars(
        select(HealthCheckRecord)
        .where(HealthCheckRecord.target_type == "channel", HealthCheckRecord.target_id == channel_id)
        .order_by(HealthCheckRecord.id.desc())
        .limit(20)
    ).all()
    latest_success = next((item for item in latest_checks if item.status == "online"), None)
    latest_failure = next((item for item in latest_checks if item.status != "online"), None)
    return {
        "latest": latest_checks[0] if latest_checks else None,
        "latest_success_at": getattr(latest_success, "created_at", None),
        "latest_success_summary": getattr(latest_success, "summary", None),
        "latest_failure_at": getattr(latest_failure, "created_at", None),
        "latest_failure_summary": getattr(latest_failure, "summary", None),
    }


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    started_at = time_module.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time_module.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "request failed",
            extra={"method": request.method, "path": request.url.path, "duration_ms": duration_ms},
        )
        raise

    duration_ms = round((time_module.perf_counter() - started_at) * 1000, 2)
    # 平台后台的响应性能统计:用路由模板聚合,静态资源不记。
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path and not request.url.path.startswith("/static/"):
        record_request(f"{request.method} {route_path}", response.status_code, duration_ms)
    logger.info(
        "request completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """恢复平台与租户数据库会话，并按 URL 空间选择唯一可信身份。"""

    path = request.url.path
    # 存活探针、就绪探针和静态资源不能依赖业务表查询；数据库落后时运维仍需
    # 能读取 readyz 原因，并正常加载反向代理的静态错误页。
    if path in {"/healthz", "/readyz"} or path.startswith("/static/"):
        return await call_next(request)
    readiness = database_readiness()
    if not readiness["ready"]:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "ready": False,
                "error": readiness["error"],
                "expected_revision": readiness["expected_revision"],
            },
        )

    # 云端企微 Provider 接口是桌面客户端的机器到机器入口，不能依赖后台登录 Cookie。
    # 这些接口内部会分别校验一次性授权码或 client token，因此这里只放行到业务路由。
    # Open API、支付回调、Provider 和受控文件入口均使用自己的鉴权边界，
    # 不能因为未携带后台 Cookie 被重定向到网页登录页。
    public_prefixes = PUBLIC_MACHINE_PREFIXES
    public_paths = {
        "/",
        "/pricing",
        "/register",
        "/healthz",
        "/readyz",
        "/setup",
        "/setup/completed",
        TENANT_LOGIN_PATH,
        PLATFORM_LOGIN_PATH,
    }
    # 认证中间件只保护应用真实注册的页面和接口。完全未注册的路径直接交给
    # FastAPI 生成原生 404；仅方法不匹配的路由仍继续处理，以保留标准 405 响应。
    has_registered_route = any(
        route.matches(request.scope)[0] is not Match.NONE
        for route in request.app.routes
    )
    if not has_registered_route:
        return await call_next(request)
    db = get_platform_session()
    try:
        # 两类会话始终分别恢复。租户业务只读取 current_user，平台路径只读取
        # platform_principal，任何一方都不能通过同名账号或另一类 Cookie 自动升级身份。
        request.state.current_user = load_tenant_principal(db, request.cookies.get(TENANT_SESSION_COOKIE))
        request.state.platform_principal = load_platform_principal(
            db,
            request.cookies.get(PLATFORM_SESSION_COOKIE),
        )
        request.state.setup_required = is_setup_required(db)
    finally:
        db.close()
    if request.state.setup_required and path not in {"/setup", "/healthz", "/readyz"}:
        return RedirectResponse(url="/setup", status_code=303)

    if path in public_paths or path.startswith(public_prefixes):
        return await call_next(request)

    if path.startswith("/platform"):
        platform_principal = request.state.platform_principal
        if platform_principal is None:
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        password_redirect = platform_must_change_password_redirect(platform_principal, path)
        if password_redirect is not None:
            return RedirectResponse(url=password_redirect, status_code=303)
        return await call_next(request)

    tenant_principal = request.state.current_user
    if tenant_principal is None:
        return RedirectResponse(url=TENANT_LOGIN_PATH, status_code=303)
    password_redirect = tenant_must_change_password_redirect(tenant_principal, path)
    if password_redirect is not None:
        return RedirectResponse(url=password_redirect, status_code=303)
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and path not in {
        TENANT_PASSWORD_PATH,
        TENANT_LOGOUT_PATH,
    }:
        try:
            # 所有租户浏览器写请求统一经过订阅策略；路由无需自行读取状态或日期，
            # 新增页面也不会因为遗漏判断而在停用、挂起或到期后继续修改数据。
            require_tenant_mutation_allowed(tenant_principal)
        except TenantPolicyError as exc:
            return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    # ContextVar 必须覆盖完整的 ASGI 调用链，页面内部新建的每个业务会话才能从
    # 已验证数据库会话恢复出的 principal 取得 tenant_id。退出请求时上下文会
    # 精确复位，异步线程复用也不会把本次租户泄漏到下一次请求。
    with tenant_scope(tenant_principal.tenant_id):
        return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "fulcrum"}


@app.get("/readyz")
async def readyz():
    """只有数据库 revision 已到 head 且安全种子完成后才返回就绪。"""

    readiness = database_readiness()
    status_code = 200 if readiness["ready"] else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": bool(readiness["ready"]),
            "ready": bool(readiness["ready"]),
            "error": readiness["error"],
            "expected_revision": readiness["expected_revision"],
        },
    )


@app.get("/setup/completed", response_class=HTMLResponse)
async def setup_completed_page(request: Request):
    return render(
        request,
        "setup_completed.html",
        page_title="安装完成",
        notice=request.query_params.get("notice", ""),
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_session()
    try:
        recent_cutoff = now_shanghai() - timedelta(hours=24)
        today_cutoff = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
        # 总览始终统计当前租户的完整数据集；机器人环境已经从产品和数据库中删除。
        stats = {
            "bots": db.scalar(select(func.count(Bot.id)).where(Bot.deleted_at.is_(None))) or 0,
            "channels": db.scalar(select(func.count(Channel.id)).where(Channel.deleted_at.is_(None))) or 0,
            "plugins": db.scalar(select(func.count(Plugin.id))) or 0,
            "messages": db.scalar(select(func.count(Message.id))) or 0,
            "errors": db.scalar(select(func.count(ErrorLog.id))) or 0,
            "conversations": db.scalar(select(func.count(Conversation.id))) or 0,
        }
        ops_snapshot = {
            "enabled_bots": db.scalar(select(func.count(Bot.id)).where(Bot.is_enabled.is_(True), Bot.deleted_at.is_(None))) or 0,
            "enabled_channels": db.scalar(select(func.count(Channel.id)).where(Channel.is_enabled.is_(True), Channel.deleted_at.is_(None))) or 0,
            "online_channels": db.scalar(
                select(func.count(Channel.id)).where(
                    Channel.channel_type.in_(BUILTIN_CHANNEL_TYPES),
                    Channel.deleted_at.is_(None),
                    Channel.is_enabled.is_(True),
                    Channel.status == "online",
                )
            )
            or 0,
            "abnormal_channels": db.scalar(
                select(func.count(Channel.id)).where(
                    Channel.channel_type.in_(BUILTIN_CHANNEL_TYPES),
                    Channel.deleted_at.is_(None),
                    Channel.is_enabled.is_(True),
                    Channel.status.in_(("degraded", "offline", "failed")),
                )
            )
            or 0,
            "inbound_24h": db.scalar(
                select(func.count(Message.id)).where(Message.direction == "in", Message.created_at >= recent_cutoff)
            )
            or 0,
            "recent_errors_24h": db.scalar(select(func.count(ErrorLog.id)).where(ErrorLog.created_at >= recent_cutoff)) or 0,
            "failed_jobs": db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status == "failed")) or 0,
            "pending_jobs": db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status.in_(("pending", "running")))) or 0,
        }
        total_open_alerts = db.scalar(select(func.count(AlertRecord.id)).where(AlertRecord.status == "open")) or 0
        active_bot_stmt = (
            select(func.count(func.distinct(Conversation.bot_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.created_at >= recent_cutoff)
        )
        active_user_stmt = (
            select(func.count(func.distinct(Message.sender_id)))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(
                Message.direction == "in",
                Message.sender_id.is_not(None),
                Message.created_at >= recent_cutoff,
            )
        )
        active_group_stmt = (
            select(func.count(func.distinct(Conversation.external_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(
                Conversation.chat_type == "group",
                Message.direction == "in",
                Message.created_at >= recent_cutoff,
            )
        )
        inbound_today_stmt = (
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.direction == "in", Message.created_at >= today_cutoff)
        )
        outbound_24h_stmt = (
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, and_(Conversation.bot_id == Bot.id, Bot.deleted_at.is_(None)))
            .where(Message.direction == "out", Message.created_at >= recent_cutoff)
        )
        plugin_trigger_stmt = (
            select(func.count(PluginExecutionLog.id))
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= recent_cutoff,
                PluginExecutionLog.attempted.is_(True),
            )
        )
        plugin_success_stmt = (
            select(func.count(PluginExecutionLog.id))
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= recent_cutoff,
                PluginExecutionLog.attempted.is_(True),
                PluginExecutionLog.ok.is_(True),
            )
        )
        plugin_attempt_stmt = (
            select(func.count(PluginExecutionLog.id))
            .join(Bot, PluginExecutionLog.bot_id == Bot.id, isouter=True)
            .where(
                PluginExecutionLog.created_at >= recent_cutoff,
                PluginExecutionLog.attempted.is_(True),
            )
        )
        plugin_success_count = db.scalar(plugin_success_stmt) or 0
        plugin_attempt_count = db.scalar(plugin_attempt_stmt) or 0
        plugin_success_rate = 0 if plugin_attempt_count <= 0 else round((plugin_success_count / plugin_attempt_count) * 100)
        dashboard_metrics = {
            "inbound_today": db.scalar(inbound_today_stmt) or 0,
            "inbound_24h": ops_snapshot["inbound_24h"],
            "outbound_24h": db.scalar(outbound_24h_stmt) or 0,
            "active_bots_24h": db.scalar(active_bot_stmt) or 0,
            "active_users_24h": db.scalar(active_user_stmt) or 0,
            "active_groups_24h": db.scalar(active_group_stmt) or 0,
            "plugin_triggers_24h": db.scalar(plugin_trigger_stmt) or 0,
            "plugin_success_rate_24h": plugin_success_rate,
            "abnormal_channels": ops_snapshot["abnormal_channels"],
            "open_alerts": total_open_alerts,
        }
        trend_series = build_dashboard_trend_series(db, hours=24)
        rankings = build_dashboard_rankings(db)
        bot_rows = []
        for bot in db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all():
            bot_rows.append(
                {
                    "bot": bot,
                    "channels": db.scalar(select(func.count(Channel.id)).where(Channel.bot_id == bot.id, Channel.deleted_at.is_(None))) or 0,
                    "plugins": db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot.id)) or 0,
                    "conversations": db.scalar(select(func.count(Conversation.id)).where(Conversation.bot_id == bot.id)) or 0,
                    "messages": db.scalar(
                        select(func.count(Message.id))
                        .join(Conversation, Message.conversation_id == Conversation.id)
                        .where(Conversation.bot_id == bot.id)
                    )
                    or 0,
                }
            )
        return render(
            request,
            "dashboard.html",
            page_title="仪表盘",
            stats=stats,
            dashboard_metrics=dashboard_metrics,
            trend_series=trend_series,
            rankings=rankings,
            ops_snapshot=ops_snapshot,
            bot_rows=bot_rows,
        )
    finally:
        db.close()


@app.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request):
    db = get_session()
    try:
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        # 列表需要稳定的计数键，模板不再触碰已删除的分组、模板、标签和环境关系。
        channels_by_bot: dict[int, list[Channel]] = {}
        for channel in db.scalars(select(Channel).where(Channel.deleted_at.is_(None)).order_by(Channel.id.asc())).all():
            channels_by_bot.setdefault(int(channel.bot_id), []).append(channel)
        bot_rows = [
            {
                "bot": bot,
                "identity": bot_identity(bot.name, channels_by_bot.get(int(bot.id), [])),
                "channel_count": db.scalar(select(func.count(Channel.id)).where(Channel.bot_id == bot.id, Channel.deleted_at.is_(None))) or 0,
                "binding_count": db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot.id)) or 0,
            }
            for bot in bots
        ]
        return render(
            request,
            "bots.html",
            page_title="机器人管理",
            bot_rows=bot_rows,
            bot_count=len(bot_rows),
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
        )
    finally:
        db.close()


@app.get("/bots/{bot_id}", response_class=HTMLResponse)
async def bot_detail_page(request: Request, bot_id: int, return_to: str = ""):
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        channels = db.scalars(select(Channel).where(Channel.bot_id == bot_id, Channel.deleted_at.is_(None)).order_by(Channel.id.desc())).all()
        bindings = db.scalars(select(BotPlugin).where(BotPlugin.bot_id == bot_id, BotPlugin.status != "unbound").order_by(BotPlugin.id.desc())).all()
        conversation_count = db.scalar(select(func.count(Conversation.id)).where(Conversation.bot_id == bot_id)) or 0
        return render(
            request,
            "bot_detail.html",
            page_title=f"机器人详情 · {bot.name}",
            bot=bot,
            bot_ident=bot_identity(bot.name, list(reversed(channels))),
            channels=channels,
            bindings=bindings,
            channel_count=len(channels),
            binding_count=len(bindings),
            conversation_count=conversation_count,
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            back_href=safe_return_to(return_to, detail_fallback("/bots")),
        )
    finally:
        db.close()


@app.post("/bots/batch-toggle")
async def batch_toggle_bots_action(request: Request, action: str = Form(...)):
    enforce_role(request, "ops")
    db = get_session()
    try:
        form = await request.form()
        bot_ids = [int(value) for value in form.getlist("bot_ids") if str(value).isdigit()]
        if not bot_ids:
            return RedirectResponse(url="/bots?notice=" + quote("请先选择至少一个机器人"), status_code=303)

        bots = db.scalars(select(Bot).where(Bot.id.in_(bot_ids), Bot.deleted_at.is_(None))).all()
        enabled = action == "enable"
        for bot in bots:
            bot.is_enabled = enabled
            bot.status = "running" if enabled else "stopped"
        db.commit()
        record_audit(db, "bot.batch-toggle", "bot", detail=f"action={action}, count={len(bots)}")
        message = f"已批量{'启用' if enabled else '停用'} {len(bots)} 个机器人"
        return RedirectResponse(url="/bots?notice=" + quote(message), status_code=303)
    finally:
        db.close()


@app.post("/bots/{bot_id}/edit")
async def edit_bot_action(
    request: Request,
    bot_id: int,
    name: str = Form(...),
    description: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        cleaned_name = name.strip()
        if not cleaned_name:
            return RedirectResponse(
                url=f"/bots/{bot_id}?notice=" + quote("机器人名称不能为空") + "&tone=danger",
                status_code=303,
            )
        duplicate_id = db.scalar(select(Bot.id).where(Bot.name == cleaned_name, Bot.id != bot_id))
        if duplicate_id is not None:
            return RedirectResponse(
                url=f"/bots/{bot_id}?notice=" + quote("机器人名称已存在，请换一个名称") + "&tone=danger",
                status_code=303,
            )
        try:
            update_bot(db, bot, BotUpdate(name=cleaned_name, description=description))
        except IntegrityError:
            db.rollback()
            return RedirectResponse(
                url=f"/bots/{bot_id}?notice=" + quote("机器人名称已存在，请换一个名称") + "&tone=danger",
                status_code=303,
            )
        record_audit(db, "bot.update", "bot", bot.id, f"更新机器人 {bot.name}")
        return RedirectResponse(url=f"/bots/{bot_id}?notice=" + quote("机器人资料已更新"), status_code=303)
    finally:
        db.close()


@app.post("/bots/{bot_id}/toggle")
async def toggle_bot_action(request: Request, bot_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        toggle_bot(db, bot)
        record_audit(db, "bot.toggle", "bot", bot.id, f"enabled={bot.is_enabled}")
        return RedirectResponse(url=f"/bots/{bot_id}", status_code=303)
    finally:
        db.close()


@app.post("/bots/{bot_id}/status-check")
async def check_bot_status_action(request: Request, bot_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        status, message = run_bot_status_check(db, bot)
        record_health_check(
            db,
            "status-check",
            "bot",
            status,
            message,
            target_id=bot.id,
            details={"bot": bot.name},
        )
        if status != "running":
            create_alert(db, "bot-health", f"机器人 {bot.name} 状态异常", message, related_type="bot", related_id=bot.id)
        record_audit(db, "bot.status-check", "bot", bot.id, message)
        return RedirectResponse(url=f"/bots/{bot_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/bots/{bot_id}/delete")
async def delete_bot_action(request: Request, bot_id: int, confirmation_name: str = Form("")):
    enforce_role(request, "admin")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        expected_name = (bot.name or "").strip()
        submitted_name = (confirmation_name or "").strip()
        if not expected_name or submitted_name != expected_name:
            message = quote(f"删除失败：请输入机器人名称 {expected_name} 进行确认")
            return RedirectResponse(url=f"/bots/{bot_id}?notice={message}&tone=danger", status_code=303)
        name = bot.name
        delete_bot(db, bot)
        record_audit(db, "bot.delete", "bot", bot_id, f"删除机器人 {name}")
        return RedirectResponse(url="/bots?notice=机器人已删除", status_code=303)
    finally:
        db.close()


@app.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    db = get_session()
    try:
        tenant_id = require_tenant_id()
        # “账号接入”列表仍以真实 Channel 为运行对象，但默认只展示业务状态，
        # 不再把 API Base、Webhook、适配 JSON 和执行端主键平铺给租户。
        channels = db.scalars(
            select(Channel)
            .where(Channel.channel_type.in_(BUILTIN_CHANNEL_TYPES), Channel.deleted_at.is_(None))
            .order_by(Channel.id.desc())
        ).all()
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        bindings = db.scalars(
            select(BotBinding)
            .where(BotBinding.tenant_id == tenant_id)
            .order_by(BotBinding.id.desc())
        ).all()
        binding_by_channel_id = {
            int(binding.channel_id): binding
            for binding in bindings
            if binding.channel_id is not None
        }
        account_rows = [
            {
                "channel": channel,
                "bot_name": bot_name_map.get(channel.bot_id, "机器人"),
                "binding": binding_by_channel_id.get(channel.id),
                "connection": _channel_connection_state(channel),
                "identity": account_identity(channel),
                "last_online_at": channel.last_heartbeat_at or channel.last_message_sync_at or channel.last_login_at,
            }
            for channel in channels
        ]
        try:
            quota = get_bot_quota(db)
            quota_error = ""
        except BotEntitlementError as exc:
            quota = None
            quota_error = _account_connection_error_message(str(exc))
        return render(
            request,
            "channels.html",
            page_title="账号接入",
            account_rows=account_rows,
            quota=quota,
            quota_error=quota_error,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


def _channel_connection_state(channel: Channel) -> dict[str, object]:
    """从服务端运行信息读取统一连接状态，不把协议原始响应交给页面。"""

    try:
        runtime_info = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        runtime_info = {}
    connection = runtime_info.get("account_connection") if isinstance(runtime_info, dict) else None
    if not isinstance(connection, dict):
        connection = {}
    state = str(connection.get("state") or ("active" if channel.is_enabled else channel.status or "draft")).strip()
    labels = {
        "active": "已接入",
        "online": "已接入",
        "verified": "待确认",
        "connecting": "连接中",
        "failed": "连接失败",
        "draft": "待连接",
        "offline": "已停用",
    }
    return {
        "state": state,
        "label": labels.get(state, state or "未知"),
        "message": str(connection.get("message") or "").strip(),
        "display_name": str(connection.get("display_name") or channel.name).strip(),
    }


def _account_connection_error_message(code: str) -> str:
    """把连接和额度错误码转换为不泄露其他租户信息的操作提示。"""

    messages = {
        "bot_quota_exceeded": "机器人额度已用完，请先升级套餐",
        "subscription_inactive": "当前订阅不可用",
        "subscription_expired": "当前订阅已到期",
        "bot_account_already_bound": "该账号已经绑定，请联系平台处理",
        "bot_binding_conflict": "账号绑定冲突，请稍后重试",
        "device_owned_by_other_tenant": "该设备已经绑定，请联系平台处理",
        "device_trial_already_claimed": "该设备已经领取过试用权益",
        "trial_device_already_claimed": "当前试用设备不能更换绑定账号",
        "connection_not_verified": "请先完成账号验证",
        "connection_identity_changed": "微信账号信息已变化，请刷新页面后重新核对",
        "connection_already_active": "该账号已经完成接入",
        "connection_not_found": "连接记录不存在",
        "connection_type_not_supported": "暂不支持该接入方式",
        "account_identity_missing": "客户端没有返回可验证的账号身份",
        "platform_protocol_service_missing": "平台尚未配置该协议服务，请联系平台管理员",
        "platform_protocol_service_disabled": "该协议服务已暂停新账号接入，请联系平台管理员",
        "proxy_mode_invalid": "请选择本次登录使用的网络方式",
        "proxy_province_required": "请选择代理所在省份",
        "proxy_city_required": "请选择代理所在城市",
        "proxy_host_required": "请输入自有代理地址",
        "proxy_port_invalid": "请输入有效的自有代理端口",
        "proxy_region_unavailable": "所选地区暂无可用代理，请选择其他地区或直接登录",
        "proxy_connector_required": "请选择一个在线的本地代理连接器",
        "proxy_connector_unavailable": "本地连接器当前不在线，请在 Windows 连接器中启动后重试",
    }
    normalized = str(code or "").strip()
    return messages.get(normalized, normalized or "账号连接失败")


@app.get("/channels/connect", response_class=HTMLResponse)
async def channel_connect_page(request: Request):
    """按选择平台、连接账号和确认启用三个阶段展示统一接入向导。"""

    db = get_session()
    try:
        raw_channel_id = str(request.query_params.get("channel_id") or "").strip()
        channel = None
        if raw_channel_id:
            try:
                channel = get_channel(db, int(raw_channel_id))
            except ValueError:
                channel = None
            if channel is None:
                raise HTTPException(status_code=404, detail="Connection not found")
            if channel.channel_type == "wechat_webridge" and not has_tenant_role(require_user(request), "tenant_admin"):
                raise HTTPException(status_code=403, detail="Permission denied")
        requested_type = str(
            channel.channel_type if channel is not None else request.query_params.get("channel_type") or ""
        ).strip()
        if requested_type:
            try:
                requested_type = require_supported_channel_type(requested_type)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Connection type not found") from exc
        platform_choice = str(request.query_params.get("platform") or "").strip()
        connection = _channel_connection_state(channel) if channel is not None else {}
        network_setup = bool(channel is not None and request.query_params.get("network") == "1")
        is_relogin = bool(
            channel is not None
            and (network_setup or request.query_params.get("relogin") == "1")
        )
        if channel is not None and channel.channel_type == "wechat_gugatool":
            if not guga_connection_url(channel):
                try:
                    require_tenant_mutation_allowed(require_user(request))
                except TenantPolicyError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
                initialize_guga_connection(channel, build_public_base_url(request))
                db.commit()
            stage = "guga"
        elif channel is not None and channel.channel_type == "wechat_webridge":
            if not webridge_connection_url(channel):
                try:
                    require_tenant_mutation_allowed(require_user(request))
                except TenantPolicyError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
                initialize_webridge_connection(channel, build_public_base_url(request))
                db.commit()
            stage = "webridge"
        elif network_setup:
            stage = "configure"
        elif channel is not None:
            stage = "confirm" if connection.get("state") == "verified" else "progress"
        elif requested_type:
            stage = "configure"
        elif platform_choice == "wechat":
            stage = "wechat_method"
        else:
            stage = "choose"
        try:
            quota = get_bot_quota(db)
            quota_error = ""
        except BotEntitlementError as exc:
            quota = None
            quota_error = _account_connection_error_message(str(exc))
        response = render(
            request,
            "channel_connect.html",
            page_title="接入账号",
            stage=stage,
            requested_type=requested_type,
            channel=channel,
            connection=connection,
            webridge_url=webridge_connection_url(channel) if channel and channel.channel_type == "wechat_webridge" else "",
            webridge_account=webridge_account_info(channel) if channel and channel.channel_type == "wechat_webridge" else {},
            guga_url=guga_connection_url(channel) if channel and channel.channel_type == "wechat_gugatool" else "",
            guga_account=guga_account_info(channel) if channel and channel.channel_type == "wechat_gugatool" else {},
            quota=quota,
            quota_error=quota_error,
            provider_nodes=list_provider_nodes(db),
            proxy_regions=(list_available_proxy_regions(db) if requested_type == "wechat_ipad" else []),
            local_connectors=(list_online_proxy_connectors(db) if requested_type == "wechat_ipad" else []),
            is_relogin=is_relogin,
            login_network=(
                build_login_proxy_session_summary(db, channel_id=int(channel.id))
                if channel is not None and channel.channel_type == "wechat_ipad"
                else {}
            ),
            wechat_ipad_overview=(build_wechat_ipad_overview(channel) if channel and channel.channel_type == "wechat_ipad" else None),
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        db.close()


@app.post("/channels/connect/start")
async def channel_connect_start_action(
    request: Request,
    channel_type: str = Form(""),
    display_name: str = Form(""),
    api_base: str = Form(""),
    bot_token: str = Form(""),
    adapter_config_json: str = Form(""),
    provider_node_id: int = Form(0),
    provider_account_id: int = Form(0),
    proxy_mode: str = Form("direct"),
    proxy_province: str = Form(""),
    proxy_city: str = Form(""),
    custom_proxy_host: str = Form(""),
    custom_proxy_port: str = Form(""),
    custom_proxy_username: str = Form(""),
    custom_proxy_password: str = Form(""),
    proxy_connector_id: int = Form(0),
    csrf_token: str = Form(""),
):
    """创建连接草稿；可信账号 ID 和设备键绝不从此表单接收。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            channel = begin_account_connection(
                db,
                channel_type=channel_type,
                display_name=display_name,
                connection_values={
                    "api_base": build_public_base_url(request) if channel_type in {"wechat_webridge", "wechat_gugatool"} else api_base,
                    "bot_token": bot_token,
                    "adapter_config_json": adapter_config_json,
                    "provider_node_id": provider_node_id,
                    "provider_account_id": provider_account_id,
                },
            )
            if channel.channel_type == "wechat_ipad":
                # 登录网络只保存为本次扫码会话快照；渠道 adapter_config 始终不接收代理字段。
                create_login_proxy_session(
                    db,
                    channel=channel,
                    mode=proxy_mode,
                    values={
                        "province": proxy_province,
                        "city": proxy_city,
                        "host": custom_proxy_host,
                        "port": custom_proxy_port,
                        "username": custom_proxy_username,
                        "password": custom_proxy_password,
                        "connector_id": proxy_connector_id,
                    },
                )
            elif channel.channel_type not in {"wechat_webridge", "wechat_gugatool"}:
                verify_account_connection(db, channel_id=channel.id)
        except (AccountConnectionError, BotEntitlementError, LoginProxyPoolError, ValueError) as exc:
            message = quote(_account_connection_error_message(str(exc)))
            if "channel" in locals() and channel.channel_type == "wechat_ipad":
                return RedirectResponse(
                    url=f"/channels/{channel.id}/wechat-ipad/login-session?notice={message}&tone=danger",
                    status_code=303,
                )
            return RedirectResponse(url=f"/channels/connect?channel_type={quote(channel_type)}&notice={message}&tone=danger", status_code=303)
        return RedirectResponse(url=f"/channels/connect?channel_id={channel.id}", status_code=303)
    finally:
        db.close()


@app.get("/channels/{channel_id}/wechat-ipad/login-session")
async def configure_wechat_ipad_login_session(request: Request, channel_id: int):
    """进入统一登录网络选择页，首次接入草稿和正式账号共用同一页面。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if channel is None or channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=404, detail="Channel not found")
        query = urlencode(
            {
                "channel_id": int(channel.id),
                "network": "1",
                "notice": request.query_params.get("notice", ""),
                "tone": request.query_params.get("tone", ""),
            }
        )
        return RedirectResponse(url=f"/channels/connect?{query}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-session")
async def create_wechat_ipad_login_session_action(
    request: Request,
    channel_id: int,
    proxy_mode: str = Form("direct"),
    proxy_province: str = Form(""),
    proxy_city: str = Form(""),
    custom_proxy_host: str = Form(""),
    custom_proxy_port: str = Form(""),
    custom_proxy_username: str = Form(""),
    custom_proxy_password: str = Form(""),
    proxy_connector_id: int = Form(0),
    csrf_token: str = Form(""),
):
    """替换当前扫码会话并跳转到业务化二维码页，旧会话租约同步释放。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if channel is None or channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            create_login_proxy_session(
                db,
                channel=channel,
                mode=proxy_mode,
                values={
                    "province": proxy_province,
                    "city": proxy_city,
                    "host": custom_proxy_host,
                    "port": custom_proxy_port,
                    "username": custom_proxy_username,
                    "password": custom_proxy_password,
                    "connector_id": proxy_connector_id,
                },
            )
        except LoginProxyPoolError as exc:
            message = quote(_account_connection_error_message(str(exc)))
            return RedirectResponse(
                url=f"/channels/{channel.id}/wechat-ipad/login-session?notice={message}&tone=danger",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/channels/connect?channel_id={channel.id}&relogin=1",
            status_code=303,
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}/connect/status")
async def channel_connect_status(request: Request, channel_id: int):
    """只向所属企业管理员返回接入进度，不暴露 Token 或设备会话。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if channel is None or channel.channel_type not in {"wechat_webridge", "wechat_gugatool"}:
            raise HTTPException(status_code=404, detail="Connection not found")
        account = webridge_account_info(channel) if channel.channel_type == "wechat_webridge" else guga_account_info(channel)
        return JSONResponse(
            {"connection": _channel_connection_state(channel), "account": account},
            headers={"Cache-Control": "no-store"},
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/connect/verify")
async def channel_connect_verify_action(
    request: Request,
    channel_id: int,
    csrf_token: str = Form(""),
):
    """重新检查草稿连接状态并保存可信身份摘要。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            result = verify_account_connection(db, channel_id=channel_id)
        except AccountConnectionError as exc:
            message = quote(_account_connection_error_message(str(exc)))
            return RedirectResponse(url=f"/channels/connect?channel_id={channel_id}&notice={message}&tone=danger", status_code=303)
        tone = "danger" if result.state == "failed" else ""
        return RedirectResponse(
            url=f"/channels/connect?channel_id={channel_id}&notice={quote(result.message)}&tone={tone}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/connect/finalize")
async def channel_connect_finalize_action(
    request: Request,
    channel_id: int,
    display_name: str = Form(""),
    expected_account_id: str = Form(""),
    csrf_token: str = Form(""),
):
    """确认显示名称并原子启用机器人、渠道和额度绑定。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            account = finalize_account_connection(db, channel_id=channel_id, display_name=display_name, expected_account_id=expected_account_id)
        except AccountConnectionError as exc:
            message = quote(_account_connection_error_message(str(exc)))
            return RedirectResponse(url=f"/channels/connect?channel_id={channel_id}&notice={message}&tone=danger", status_code=303)
        return RedirectResponse(url=f"/channels/{account.channel.id}?notice={quote('账号已接入')}", status_code=303)
    finally:
        db.close()


@app.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_detail_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in BUILTIN_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        provider_nodes = list_provider_nodes(db)
        bot_name_map = build_bot_name_map(bots)
        binding = db.scalar(select(BotBinding).where(BotBinding.channel_id == channel.id))
        connection = _channel_connection_state(channel)
        try:
            quota = get_bot_quota(db)
            quota_error = ""
        except BotEntitlementError as exc:
            quota = None
            quota_error = _account_connection_error_message(str(exc))
        message_page_size = 20
        raw_message_page = str(request.query_params.get("message_page", "1")).strip()
        try:
            message_page = max(int(raw_message_page), 1)
        except ValueError:
            message_page = 1
        message_total = int(
            db.scalar(
                select(func.count(Message.id))
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.channel_id == channel.id)
            )
            or 0
        )
        message_total_pages = max((message_total + message_page_size - 1) // message_page_size, 1)
        message_page = min(message_page, message_total_pages)
        message_offset = (message_page - 1) * message_page_size
        recent_channel_messages = db.scalars(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.channel_id == channel.id)
            .order_by(Message.id.desc())
            .offset(message_offset)
            .limit(message_page_size)
        ).all()
        message_pagination = {
            "page": message_page,
            "page_size": message_page_size,
            "total": message_total,
            "total_pages": message_total_pages,
            "has_prev": message_page > 1,
            "has_next": message_page < message_total_pages,
            "prev_page": message_page - 1 if message_page > 1 else 1,
            "next_page": message_page + 1 if message_page < message_total_pages else message_total_pages,
            "from_index": message_offset + 1 if message_total else 0,
            "to_index": min(message_offset + len(recent_channel_messages), message_total),
        }
        recent_message_identities = IdentityDirectory(db).for_messages(recent_channel_messages)
        # workwechat 云端 WS/拉取模式由本地 provider 主动注册和领取任务，不需要主服务直连 provider 的 API Base。
        workwechat_cloud_poll_config = load_cloud_poll_config(channel) if channel.channel_type == "workwechat" else None
        workwechat_cloud_poll_enabled = bool(
            channel.channel_type == "workwechat"
            and workwechat_cloud_poll_config
            and str(workwechat_cloud_poll_config.get("workwechat_delivery_mode") or "").strip() == "cloud_poll"
        )
        health_checks = {
            "api_base": True if workwechat_cloud_poll_enabled else bool(channel.api_base),
            "api_base_not_required": workwechat_cloud_poll_enabled,
            "webhook_path": bool(channel.webhook_path),
            "webhook_not_required": workwechat_cloud_poll_enabled,
            "bot_token": bool(channel.bot_token_encrypted),
            "adapter_config": bool(channel.adapter_config_json),
        }
        wechat_ipad_group_summary = build_wechat_ipad_group_summary(db, channel) if channel.channel_type == "wechat_ipad" else None
        channel_chatroom_summary = None
        if channel.channel_type == "wechat_winauto":
            # 渠道详情页只读取本地轻量群视图；主动刷新仍由页面按钮触发，避免打开详情页时阻塞。
            channel_chatroom_summary = build_channel_chatroom_summary(build_wechat_winauto_chatroom_list(db, channel))
        elif channel.channel_type == "workwechat":
            # workwechat 群摘要来自独立群服务，包含 Hook 群列表和消息观测落库后的本地样本。
            channel_chatroom_summary = build_channel_chatroom_summary(build_workwechat_chatroom_list(db, channel))
        # WeBridge 的群组信息统一放在微桥后台，账号详情页不再重复展示群聊概览卡片。
        workwechat_contact_summary = build_workwechat_contact_summary(db, channel) if channel.channel_type == "workwechat" else None
        # 企微云端拉取配置只在渠道详情页展示摘要；授权码明文不落库，生成后只通过一次性提示展示。
        wechat_ipad_contact_list = build_wechat_ipad_contact_list(channel) if channel.channel_type == "wechat_ipad" else None
        return render(
            request,
            "channel_detail.html",
            page_title=f"账号详情 · {channel.name}",
            account=account_identity(channel),
            channel=channel,
            binding=binding,
            connection=connection,
            quota=quota,
            quota_error=quota_error,
            last_online_at=channel.last_heartbeat_at or channel.last_message_sync_at or channel.last_login_at,
            bots=bots,
            bot_name_map=bot_name_map,
            builtin_channel_types=BUILTIN_CHANNEL_TYPES,
            channel_definition=CHANNEL_TYPE_FORM_SCHEMA_BY_CODE[channel.channel_type],
            channel_config_field_map={
                item["name"]: item
                for item in CHANNEL_TYPE_FORM_SCHEMA_BY_CODE[channel.channel_type]["config_fields"]
            },
            provider_nodes=provider_nodes,
            recent_channel_messages=recent_channel_messages,
            recent_message_identities=recent_message_identities,
            message_pagination=message_pagination,
            token_preview=channel.bot_token_masked or "-",
            health_checks=health_checks,
            channel_health_timeline=build_channel_health_timeline(db, channel.id),
            wechat_ipad_overview=build_wechat_ipad_overview(channel, group_summary=wechat_ipad_group_summary) if channel.channel_type == "wechat_ipad" else None,
            wechat_ipad_group_summary=wechat_ipad_group_summary,
            channel_chatroom_summary=channel_chatroom_summary,
            workwechat_contact_summary=workwechat_contact_summary,
            workwechat_cloud_poll_config=workwechat_cloud_poll_config,
            wechat_ipad_contact_list=wechat_ipad_contact_list,
            can_manage_channel=has_tenant_role(require_user(request), "tenant_admin"),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}/wechat-webridge", response_class=HTMLResponse)
async def wechat_webridge_dashboard_page(request: Request, channel_id: int, tab: str = "contacts", keyword: str = "", page: int = 1, conversation_id: int = 0, sender: str = ""):
    """展示 WeBridge 渠道内已落库的好友、群组和消息信息。"""

    # 后台页面必须先恢复当前租户身份，避免匿名请求或错误租户上下文读取渠道数据。
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="WeBridge channel not found")
        # 手机离线时不会有 poll，打开后台页面时也回收一次超时命令，避免页面一直显示进行中。
        reap_stale_commands(db, channel)
        return render(
            request,
            "wechat_webridge_dashboard.html",
            page_title=f"微桥后台 · {channel.name}",
            channel=channel,
            dashboard=build_webridge_dashboard(db, channel, tab=tab, keyword=keyword, page=page, conversation_id=conversation_id, sender=sender),
            device=build_webridge_device_panel(db, channel),
            live=build_webridge_live_summary(db, channel),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}/wechat-webridge/live")
async def wechat_webridge_live_summary(request: Request, channel_id: int):
    """顶部摘要的实时数据（只读 JSON），页面每 10 秒轮询一次。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="WeBridge channel not found")
        return JSONResponse(build_webridge_live_summary(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-webridge/command")
async def wechat_webridge_dashboard_command_action(
    request: Request,
    channel_id: int,
    command_name: str = Form(""),
    args_json: str = Form("{}"),
    csrf_token: str = Form(""),
):
    """从后台直接发起好友、群组和成员同步命令。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="WeBridge channel not found")
        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as exc:
            return RedirectResponse(url=f"/channels/{channel_id}/wechat-webridge?notice={quote('参数 JSON 无效')}", status_code=303)
        if not isinstance(args, dict):
            return RedirectResponse(url=f"/channels/{channel_id}/wechat-webridge?notice={quote('参数必须是 JSON 对象')}", status_code=303)
        job = enqueue_command(db, channel, command_name=command_name, args=args)
        db.commit()
        return RedirectResponse(url=f"/channels/{channel_id}/wechat-webridge?notice={quote(f'已提交 {command_name}，任务 #{job.id}')}", status_code=303)
    except ValueError as exc:
        return RedirectResponse(url=f"/channels/{channel_id}/wechat-webridge?notice={quote(str(exc))}", status_code=303)
    finally:
        db.close()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = get_platform_session()
    try:
        if not is_setup_required(db):
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        return render(
            request,
            "setup.html",
            page_title="创建平台管理员",
            notice=request.query_params.get("notice", ""),
            form_data={
                "admin_username": "admin",
                "admin_display_name": "平台管理员",
            },
        )
    finally:
        db.close()


@app.post("/setup")
async def setup_action(
    request: Request,
    admin_username: str = Form(...),
    admin_display_name: str = Form("平台管理员"),
    admin_password: str = Form(...),
    admin_password_confirm: str = Form(...),
):
    form_data = {
        "admin_username": str(admin_username or "").strip(),
        "admin_display_name": str(admin_display_name or "").strip() or "平台管理员",
    }
    db = get_platform_session()
    try:
        if not is_setup_required(db):
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        if not form_data["admin_username"]:
            return render(request, "setup.html", page_title="创建平台管理员", notice="管理员用户名不能为空", form_data=form_data, response_status=400)
        if len(form_data["admin_username"]) > 100:
            return render(request, "setup.html", page_title="创建平台管理员", notice="管理员用户名长度不能超过 100", form_data=form_data, response_status=400)
        if admin_password != admin_password_confirm:
            return render(request, "setup.html", page_title="创建平台管理员", notice="两次输入的管理员密码不一致", form_data=form_data, response_status=400)
        password_error = validate_password_change(admin_password)
        if password_error:
            return render(request, "setup.html", page_title="创建平台管理员", notice=password_error, form_data=form_data, response_status=400)
        provision_error = provision_platform_admin(
            db,
            admin_username=form_data["admin_username"],
            admin_password=admin_password,
            admin_display_name=form_data["admin_display_name"],
        )
        if provision_error:
            return render(request, "setup.html", page_title="创建平台管理员", notice=f"创建失败：{provision_error}", form_data=form_data, response_status=500)
    finally:
        db.close()

    return RedirectResponse(
        url=PLATFORM_LOGIN_PATH + "?notice=" + quote("平台管理员已创建，请登录后创建套餐和租户"),
        status_code=303,
    )


@app.get("/channels/{channel_id}/chatrooms/{chatroom_id}", response_class=HTMLResponse)
async def channel_chatroom_detail_page(request: Request, channel_id: int, chatroom_id: int, tab: str = "features"):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type == "wechat_ipad":
            detail = build_wechat_ipad_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "wechat_winauto":
            detail = build_wechat_winauto_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "workwechat":
            detail = build_workwechat_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "wechat_webridge":
            detail = build_webridge_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "wechat_gugatool":
            # 咕嘎助手的群实体由群消息驱动创建，详情页复用通用构造但隐藏同步按钮。
            from app.services.wechat_gugatool_chatrooms import build_wechat_gugatool_chatroom_detail

            detail = build_wechat_gugatool_chatroom_detail(db, channel, chatroom)
        else:
            raise HTTPException(status_code=400, detail="Chatroom detail only supported for wechat_ipad / wechat_winauto / workwechat / wechat_webridge / wechat_gugatool channels")
        return render(
            request,
            "chatroom_detail.html",
            page_title=f"群详情 · {detail['name']}",
            channel=channel,
            chatroom=chatroom,
            detail=detail,
            # 群详情分为 功能/插件/成员/消息/资料 五个标签，非法值回落到功能。
            tab=tab if tab in ("features", "plugins", "members", "messages", "profile") else "features",
            # 没单独保存过设置的能力跟随默认值:AI 对话跟随租户「AI 助手」里的聊天开关。
            features=build_feature_cards(chatroom, channel.channel_type, defaults={"ai_chat": bool(getattr(ai_dispatch.get_ai_setting(db), "chat_enabled", False))}),
            group_modes=GROUP_MODE_OPTIONS,
            # 自动抢红包、自动收款的明细与统计,按群功能 key 取(仅微桥的群有这两个功能)。
            ledgers=(
                {
                    "red_packet": payment_ledger.build_ledger(db, channel_id=channel.id, kind=payment_ledger.KIND_RED_PACKET, chat_external_id=chatroom.external_id),
                    "transfer_receive": payment_ledger.build_ledger(db, channel_id=channel.id, kind=payment_ledger.KIND_TRANSFER, chat_external_id=chatroom.external_id),
                }
                if channel.channel_type == "wechat_webridge"
                else {}
            ),
            # 当前运行模式:完全关闭看状态,其余按总开关(is_active)区分正常运行与只采集。
            group_mode=GROUP_MODE_OFF if is_collection_off(chatroom) else (GROUP_MODE_RUN if chatroom.is_active else GROUP_MODE_COLLECT_ONLY),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


def _directory_pager_base(path: str, **params: Any) -> str:
    """目录页分页链接的公共前缀:保留搜索与筛选条件,页码由模板追加。"""

    query = urlencode({key: value for key, value in params.items() if value not in ("", 0, None)})
    return f"{path}?{query}" if query else f"{path}?"


@app.get("/chatrooms", response_class=HTMLResponse)
async def chatroom_directory_page(request: Request, q: str = "", channel_id: int = 0, status: str = "", page: int = 1):
    """群管理唯一入口:所有账号的群合并展示,支持搜索、账号与状态筛选。"""

    db = get_session()
    try:
        directory = build_chatroom_directory(db, keyword=q, channel_id=channel_id, status=status, page=page)
        return render(
            request,
            "chatroom_directory.html",
            page_title="群管理",
            directory=directory,
            channels=channel_options(db),
            pager_base=_directory_pager_base("/chatrooms", q=directory["keyword"], channel_id=channel_id, status=directory["status"]),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/chatrooms/plugin-scopes", response_class=HTMLResponse)
async def chatroom_plugin_scopes_page(request: Request, plugin_id: int = 0, q: str = "", channel_id: int = 0, scope_status: str = "", scope_source: str = "", page: int = 1):
    user = enforce_role(request, "readonly")
    db = get_session()
    try:
        directory = build_chatroom_plugin_directory(db, tenant_id=user.tenant_id, plugin_id=plugin_id, keyword=q, channel_id=channel_id, scope_status=scope_status, scope_source=scope_source, page=page)
        if plugin_id and directory["plugin"] is None:
            raise HTTPException(status_code=404, detail="插件未绑定或当前不可用")
        return render(
            request,
            "chatroom_plugin_scopes.html",
            page_title="群插件批量管理",
            directory=directory,
            tenant_id=user.tenant_id,
            pager_base=_directory_pager_base("/chatrooms/plugin-scopes", plugin_id=plugin_id, q=directory["keyword"], channel_id=channel_id, scope_status=directory["status"], scope_source=directory["source"]),
            selection_cleared=request.query_params.get("selection_cleared") == "1",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.post("/chatrooms/plugins/batch")
async def batch_chatroom_plugin_scope_action(
    request: Request,
    plugin_id: int = Form(...),
    chatroom_ids: list[int] = Form(...),
    mode: str = Form(...),
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    """只调整所选群内的一个插件，不打开群总开关或机器人插件总开关。"""

    user = enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        if mode not in {"enabled", "disabled"}:
            raise HTTPException(status_code=400, detail="请选择启用或禁用")
        room_ids = set(chatroom_ids)
        if not room_ids or len(room_ids) > 1000:
            raise HTTPException(status_code=400, detail="请选择 1 至 1000 个群聊，可跨页选择")
        option = {item["id"]: item for item in chatroom_plugin_options(db, user.tenant_id)}.get(plugin_id)
        if option is None:
            raise HTTPException(status_code=404, detail="插件未绑定或当前不可用")
        rooms = db.scalars(select(Chatroom).where(Chatroom.id.in_(room_ids))).all()
        if len(rooms) != len(room_ids):
            raise HTTPException(status_code=404, detail="所选群聊不存在或不属于当前企业")
        channels = {int(item.id): item for item in db.scalars(select(Channel).where(Channel.id.in_({room.channel_id for room in rooms}))).all()}
        bindings = {
            int(item.bot_id): item
            for item in db.scalars(select(BotPlugin).where(BotPlugin.plugin_id == plugin_id, BotPlugin.status != "unbound")).all()
        }
        # 整批验证通过后才修改，避免遇到未绑定或跨账号的群时只保存了前半批。
        for room in rooms:
            channel = channels.get(int(room.channel_id))
            binding = bindings.get(int(room.bot_id))
            if (
                not channel or channel.deleted_at is not None
                or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES
                or channel.bot_id != room.bot_id
                or room.tenant_id != user.tenant_id or channel.tenant_id != user.tenant_id
                or room.status == "left" or binding is None
                or binding.tenant_id != user.tenant_id or room.bot_id not in option["bot_ids"]
            ):
                raise HTTPException(status_code=400, detail="所选群包含未绑定该插件、已退出或不可操作的群，请重新选择")
        for room in rooms:
            update_group_plugin_scope_enabled(
                db,
                channel=channels[int(room.channel_id)],
                chatroom=room,
                bot_plugin_id=bindings[int(room.bot_id)].id,
                enabled=mode == "enabled",
                operator_id=user.user_id,
                audit_note="群管理批量启用插件" if mode == "enabled" else "群管理批量禁用插件",
                source="group_scope_batch_toggle",
            )
        db.commit()
        record_audit(db, "chatroom.plugin-scope.batch", "plugin", plugin_id, f"mode={mode}, chatrooms={','.join(str(item) for item in sorted(room_ids))}, operator={user.username}")
        verb = "启用" if mode == "enabled" else "禁用"
        message = f"已在 {len(rooms)} 个群聊中{verb}「{option['name']}」"
        if mode == "enabled":
            message += "；群聊和机器人上的插件总开关均开启后才会运行"
        target = urlsplit(str(next or ""))
        location = f"/chatrooms/plugin-scopes?{target.query}&" if target.path == "/chatrooms/plugin-scopes" and not target.netloc and not target.scheme else f"/chatrooms/plugin-scopes?plugin_id={plugin_id}&"
        redirect_url = f"{location}selection_cleared=1&notice={quote(message)}"
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": True, "notice": message, "count": len(rooms), "redirect_url": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)
    except (HTTPException, ValueError):
        db.rollback()
        raise
    finally:
        db.close()


@app.get("/contacts", response_class=HTMLResponse)
async def contact_directory_page(request: Request, q: str = "", channel_id: int = 0, page: int = 1):
    """好友唯一入口:所有账号的好友合并展示,可直接跳到与好友的聊天。"""

    db = get_session()
    try:
        directory = build_contact_directory(db, keyword=q, channel_id=channel_id, page=page)
        return render(
            request,
            "contact_directory.html",
            page_title="好友",
            directory=directory,
            channels=channel_options(db),
            pager_base=_directory_pager_base("/contacts", q=directory["keyword"], channel_id=channel_id),
        )
    finally:
        db.close()


@app.get("/points", response_class=HTMLResponse)
async def points_accounts_page(request: Request, q: str = "", group_id: str = "", page: int = 1, notice: str = ""):
    """积分管理:所有群的积分账户,可筛选,并可人工发放或扣减。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        groups = group_options(db)
        # 默认进入第一个群的成员列表:成员(含 0 积分的)都在这里,选到人再发放。
        if not group_id and groups:
            group_id = str(groups[0]["id"])
        directory = build_points_accounts(db, keyword=q, group_id=group_id, page=page)
        return render(
            request,
            "points_accounts.html",
            page_title="积分",
            directory=directory,
            groups=groups,
            pager_base=_directory_pager_base("/points", q=directory["keyword"], group_id=group_id),
            notice=notice,
        )
    finally:
        db.close()


@app.get("/points/ledger", response_class=HTMLResponse)
async def points_ledger_page(request: Request, q: str = "", group_id: str = "", source: str = "", page: int = 1):
    """积分账单:每一笔赚取、消费、转账和人工调整,最新在前。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        directory = build_points_ledger(db, keyword=q, group_id=group_id, source=source, page=page)
        return render(
            request,
            "points_ledger.html",
            page_title="积分账单",
            directory=directory,
            groups=group_options(db),
            pager_base=_directory_pager_base("/points/ledger", q=directory["keyword"], group_id=group_id, source=source),
        )
    finally:
        db.close()


@app.post("/points/adjust")
async def points_adjust_action(
    request: Request,
    group_id: str = Form(""),
    user_id: str = Form(""),
    amount: int = Form(0),
    mode: str = Form("grant"),
    note: str = Form(""),
    form_token: str = Form(""),
    csrf_token: str = Form(""),
    back: str = Form("/points"),
):
    """对列表里已选中的某个成员发放或扣减积分,提交后回到原列表位置。"""

    user = enforce_role(request, "ops")
    # 只允许回到积分管理页,防止被当作开放跳转。
    back_url = back if back.startswith("/points") and "//" not in back else "/points"
    separator = "&" if "?" in back_url else "?"
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        signed = abs(int(amount)) * (1 if mode == "grant" else -1)
        result = adjust_points(
            db, group_id=group_id.strip(), user_id=user_id.strip(), amount=signed, operator=user.username, note=note, request_id=form_token.strip()[:64]
        )
        if result.ok:
            # 积分已入账,审计只是附加记录:失败只记日志,不能让页面报错而误导运营重复发放。
            # 审计的 target_id 是整数列,用积分账户主键;成员 id 是 wxid 字符串,只能放进 detail。
            try:
                record_audit(
                    db,
                    "points.adjust",
                    "points_account",
                    (result.account or {}).get("id"),
                    f"group={group_id}, user={user_id}, amount={signed}, operator={user.username}",
                )
            except Exception:
                db.rollback()
                logger.exception("积分调整审计日志写入失败 | group_id=%s user_id=%s", group_id, user_id)
            after = (result.account or {}).get("total_points")
            notice = f"已{'发放' if signed > 0 else '扣减'} {abs(signed)} 积分" + (f",当前余额 {after}" if after is not None else "")
        else:
            notice = result.message
        return RedirectResponse(url=f"{back_url}{separator}notice={quote(notice)}", status_code=303)
    finally:
        db.close()


@app.get("/channels/{channel_id}/wechat-ipad/qrcode-image")
async def wechat_ipad_qrcode_image(channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        image = resolve_wechat_ipad_qrcode_image(channel)
        if not image.get("available"):
            raise HTTPException(status_code=404, detail="QR code image not available")
        if image["kind"] == "url":
            return RedirectResponse(url=image["url"], status_code=307)
        return Response(content=image["content"], media_type=image["media_type"])
    finally:
        db.close()


@app.get("/channels/{channel_id}/wechat-ipad/login-flow")
async def get_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-flow/start")
async def start_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        active_session = get_active_login_proxy_session(db, channel_id=int(channel.id))
        connector_registry = None
        if active_session is not None and active_session.mode == "local_connector":
            from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry

            connector_registry = local_proxy_connector_ws_registry
            if not connector_registry.is_online(int(active_session.proxy_connector_id or 0)):
                raise HTTPException(status_code=409, detail="proxy_connector_offline")
        try:
            start_login_with_proxy_session(db, channel)
        except WechatIpadLoginFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        active_session = get_active_login_proxy_session(db, channel_id=int(channel.id))
        if active_session is not None and active_session.mode == "local_connector":
            if connector_registry is None:
                raise HTTPException(status_code=409, detail="proxy_connector_offline")
            await connector_registry.bind_session(
                int(active_session.proxy_connector_id or 0), active_session.session_key
            )
            await connector_registry.send_session_message(
                active_session.session_key,
                {"type": "session_bind", "session_key": active_session.session_key},
            )
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-flow/cancel")
async def cancel_wechat_ipad_login_flow(request: Request, channel_id: int):
    """取消当前扫码会话；重新开始必须先创建新的服务端代理会话。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        cancel_login_with_proxy_session(db, channel)
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-flow/poll")
async def poll_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        try:
            poll_login_with_proxy_session(db, channel)
        except WechatIpadLoginFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/edit")
async def edit_channel_action(
    request: Request,
    channel_id: int,
    name: str = Form(...),
    channel_type: str = Form(...),
    api_base: str = Form(""),
    bot_token: str = Form(""),
    webhook_path: str | None = Form(None),
    dm_policy: str = Form("allowlist"),
    allow_from: str = Form(""),
    adapter_config_json: str | None = Form(None),
    csrf_token: str = Form(""),
):
    principal = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in BUILTIN_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            normalized_channel_type = require_supported_channel_type(channel_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not has_tenant_role(principal, "tenant_admin"):
            raise HTTPException(status_code=403, detail="Permission denied")
        require_tenant_csrf(db, request, csrf_token)
        if normalized_channel_type != channel.channel_type:
            return RedirectResponse(
                url=f"/channels/{channel_id}?notice={quote('渠道类型不可修改，请新建渠道后停用旧渠道')}&tone=danger",
                status_code=303,
            )
        channel_definition = get_channel_type_definition(channel.channel_type)
        # 平台托管渠道的地址只能由平台后台同步。租户即使手工构造表单字段，保存时也必须保留服务端现值。
        resolved_api_base = channel.api_base if channel_definition.platform_managed_endpoint or channel.channel_type == "wechat_webridge" else api_base
        # 普通租户页面不回显原始适配 JSON；没有提交的高级字段保留服务端现值，
        # 但 merge_channel_adapter_config 会明确删除 iPad 已废弃的长期代理节点。
        adapter_config_source = channel.adapter_config_json if adapter_config_json is None else adapter_config_json
        resolved_webhook_path = channel.webhook_path if webhook_path is None else webhook_path
        merged_adapter_config_json, adapter_error = merge_channel_adapter_config(
            channel_type=channel.channel_type,
            adapter_config_json=adapter_config_source or "",
        )
        if adapter_error:
            return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(adapter_error)}", status_code=303)
        try:
            # 机器人、外部账号和执行器共同构成商业绑定身份。高级设置只允许
            # 调整运行配置，任何额外表单字段都不能把渠道改挂到另一账号或机器人。
            provider_node, provider_account = validate_channel_executor_binding(
                db,
                channel_type=channel.channel_type,
                provider_node_id=channel.provider_node_id,
                provider_account_id=channel.provider_account_id,
                channel_id=channel.id,
            )
        except ProviderNodeError as exc:
            return RedirectResponse(
                url=f"/channels/{channel_id}?notice={quote(f'执行端绑定失败：{exc}')}&tone=danger",
                status_code=303,
            )
        try:
            validate_enabled_channel_configuration(
                channel.channel_type,
                is_enabled=channel.is_enabled,
                field_values={
                    "api_base": resolved_api_base,
                    "bot_token": bot_token,
                    "webhook_path": resolved_webhook_path,
                    "dm_policy": dm_policy,
                    "allow_from": allow_from,
                    "adapter_config_json": merged_adapter_config_json,
                    "provider_node_id": provider_node.id if provider_node else None,
                    "provider_account_id": provider_account.id if provider_account else None,
                },
                configured_secret_fields={"bot_token"} if channel.bot_token_encrypted else (),
            )
        except ChannelConfigurationError as exc:
            return RedirectResponse(
                url=f"/channels/{channel_id}?notice={quote(str(exc))}&tone=danger",
                status_code=303,
            )
        payload = ChannelUpdate(
            bot_id=channel.bot_id,
            name=name,
            channel_type=channel.channel_type,
            api_base=resolved_api_base,
            bot_token=bot_token,
            webhook_path=resolved_webhook_path,
            dm_policy=dm_policy,
            allow_from=allow_from,
            adapter_config_json=merged_adapter_config_json,
            provider_node_id=provider_node.id if provider_node else None,
            provider_account_id=provider_account.id if provider_account else None,
            is_enabled=channel.is_enabled,
        )
        update_channel(db, channel, payload)
        record_audit(db, "channel.update", "channel", channel.id, f"更新渠道 {channel.name}")
        return RedirectResponse(url=f"/channels/{channel_id}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/toggle")
async def toggle_channel_action(request: Request, channel_id: int, csrf_token: str = Form("")):
    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if not channel.is_enabled:
            # 启用动作必须关联当前租户的有效商业绑定；仅有完整协议配置的草稿
            # 仍不能进入调度，避免绕过可信身份验证、设备限制和机器人额度占用。
            binding = db.scalar(
                select(BotBinding).where(
                    BotBinding.tenant_id == require_tenant_id(),
                    BotBinding.bot_id == channel.bot_id,
                    BotBinding.channel_id == channel.id,
                    BotBinding.adapter_type == channel.channel_type,
                    BotBinding.status.in_(("active", "disabled")),
                )
            )
            if binding is None:
                if channel.channel_type == "wechat_webridge":
                    return RedirectResponse(url=f"/channels/connect?channel_id={channel_id}", status_code=303)
                return RedirectResponse(
                    url=f"/channels/{channel_id}?notice={quote('请先把系统生成的地址粘贴到手机微桥插件，完成首次注册')}&tone=danger",
                    status_code=303,
                )
            try:
                validate_channel_executor_binding(
                    db,
                    channel_type=channel.channel_type,
                    provider_node_id=channel.provider_node_id,
                    provider_account_id=channel.provider_account_id,
                    channel_id=channel.id,
                )
                validate_enabled_channel_configuration(
                    channel.channel_type,
                    is_enabled=True,
                    field_values={
                        "api_base": channel.api_base,
                        "bot_token": None,
                        "webhook_path": channel.webhook_path,
                        "dm_policy": channel.dm_policy,
                        "allow_from": channel.allow_from,
                        "adapter_config_json": channel.adapter_config_json,
                        "provider_node_id": channel.provider_node_id,
                        "provider_account_id": channel.provider_account_id,
                    },
                    configured_secret_fields={"bot_token"} if channel.bot_token_encrypted else (),
                )
            except (ChannelConfigurationError, ProviderNodeError) as exc:
                return RedirectResponse(
                    url=f"/channels/{channel_id}?notice={quote(str(exc))}&tone=danger",
                    status_code=303,
                )
        toggle_channel(db, channel)
        record_audit(db, "channel.toggle", "channel", channel.id, f"enabled={channel.is_enabled}")
        return RedirectResponse(url=f"/channels/{channel_id}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/delete")
async def delete_channel_action(request: Request, channel_id: int, csrf_token: str = Form("")):
    """删除未启用的账号(逻辑删除):从清单隐藏并释放额度,聊天数据全部保留。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        name = channel.name
        try:
            released = delete_channel_account(db, channel)
        except ChannelDeletionError as exc:
            return RedirectResponse(url=f"/channels?notice={quote(str(exc))}&tone=danger", status_code=303)
        db.commit()
        record_audit(db, "channel.delete", "channel", channel_id, f"逻辑删除账号 {name},释放额度 {released} 个")
        return RedirectResponse(url=f"/channels?notice={quote('账号已删除，占用的额度已释放')}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/health-check")
async def check_channel_health_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        adapter = get_channel_adapter(channel)
        adapter_result = adapter.health_check(channel)
        adapter_status = str(adapter_result.get("status") or "").strip().lower()
        if adapter_result.get("ok"):
            status = adapter_status or "online"
            message = str(adapter_result.get("summary") or "渠道健康检查通过")
            channel.status = status
            db.commit()
        elif adapter_status:
            status = adapter_status
            message = str(adapter_result.get("summary") or "渠道健康检查失败")
            channel.status = status
            db.commit()
        else:
            status, fallback_message = run_channel_health_check(db, channel)
            message = str(adapter_result.get("summary") or fallback_message)
        record_health_check(
            db,
            "health-check",
            "channel",
            status,
            message,
            target_id=channel.id,
            details={
                "channel": channel.name,
                "type": channel.channel_type,
                "adapter_type": adapter_result.get("adapter_type"),
                "adapter_details": adapter_result.get("details"),
            },
        )
        if status != "online":
            create_alert(db, "channel-health", f"渠道 {channel.name} 健康异常", message, related_type="channel", related_id=channel.id)
        record_audit(db, "channel.health-check", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/pull-messages")
async def pull_wechat_ipad_messages_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = pull_wechat_ipad_messages(db, channel)
        message = str(overview.get("last_sync_summary") or "已执行微信 iPad 消息拉取")
        record_audit(db, "channel.wechat-ipad.pull-messages", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/sync-chatrooms")
async def sync_wechat_ipad_chatrooms_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = sync_wechat_ipad_chatrooms(db, channel)
        message = str(overview.get("last_status_summary") or "已触发群资料同步")
        record_audit(db, "channel.wechat-ipad.sync-chatrooms", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/sync-contacts")
async def sync_wechat_ipad_contacts_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = sync_wechat_ipad_contacts(db, channel)
        message = str(overview.get("last_contact_sync_summary") or overview.get("last_status_summary") or "已执行联系人同步")
        record_audit(db, "channel.wechat-ipad.sync-contacts", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-gugatool/sync-friends")
async def sync_wechat_gugatool_friends_action(request: Request, channel_id: int, csrf_token: str = Form("")):
    """下发好友同步命令,由手机领取后回传通讯录。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_gugatool":
            raise HTTPException(status_code=404, detail="GugaTool channel not found")
        from app.services.guga_agent.service import enqueue_command

        enqueue_command(
            db,
            channel,
            command_name="get_friend_list",
            params={},
            priority=45,
            purpose="friend_sync",
        )
        db.commit()
        message = "已下发好友同步命令，手机在线时会自动回传。"
        record_audit(db, "channel.wechat-gugatool.sync-friends", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/actions/{action}")
async def run_wechat_ipad_action_api(request: Request, channel_id: int, action: str):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        overview = run_wechat_ipad_action(db, channel, action=action, params=payload)
        db.refresh(channel)
        runtime_info = json.loads(channel.runtime_info_json or "{}")
        record_audit(
            db,
            f"channel.wechat-ipad.action.{action}",
            "channel",
            channel.id,
            str(runtime_info.get("last_action_summary") or overview.get("last_status_summary") or action),
        )
        return JSONResponse(
            {
                "ok": True,
                "action": action,
                "summary": runtime_info.get("last_action_summary") or overview.get("last_status_summary"),
                "preview": runtime_info.get("last_action_result_preview") or {},
                "runtime_info": {
                    "last_action_name": runtime_info.get("last_action_name"),
                    "last_action_at": runtime_info.get("last_action_at"),
                    "last_action_ok": runtime_info.get("last_action_ok"),
                },
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        db.close()


@app.get("/channels/{channel_id}/workwechat/contacts", response_class=HTMLResponse)
async def workwechat_contact_list_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        list_view = build_workwechat_contact_list(
            db,
            channel,
            query=request.query_params.get("q", ""),
            sync_status=request.query_params.get("sync_status", ""),
            contact_kind=request.query_params.get("contact_kind", ""),
        )
        return render(
            request,
            "workwechat_contact_list.html",
            page_title=f"企微联系人 · {channel.name}",
            channel=channel,
            list_view=list_view,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}/workwechat/contacts/new", response_class=HTMLResponse)
async def workwechat_contact_create_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        return render(
            request,
            "workwechat_contact_detail.html",
            page_title=f"新增企微联系人 · {channel.name}",
            channel=channel,
            contact=None,
            detail=None,
            mode="create",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}/workwechat/contacts/{contact_id}", response_class=HTMLResponse)
async def workwechat_contact_detail_page(request: Request, channel_id: int, contact_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        detail = build_workwechat_contact_detail(db, channel, contact)
        return render(
            request,
            "workwechat_contact_detail.html",
            page_title=f"企微联系人 · {detail['name']}",
            channel=channel,
            contact=contact,
            detail=detail,
            mode="edit",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/contacts")
async def create_workwechat_contact_action(
    request: Request,
    channel_id: int,
    contact_kind: str = Form("external_contact"),
    contact_external_id: str = Form(""),
    conversation_external_id: str = Form(""),
    external_userid: str = Form(""),
    userid: str = Form(""),
    name: str = Form(""),
    display_name: str = Form(""),
    alias: str = Form(""),
    mobile: str = Form(""),
    email: str = Form(""),
    corp_name: str = Form(""),
    corp_full_name: str = Form(""),
    corp_id: str = Form(""),
    position: str = Form(""),
    avatar_url: str = Form(""),
    gender: str = Form("unknown"),
    contact_type: str = Form("unknown"),
    remark: str = Form(""),
    owner_userid: str = Form(""),
    owner_name: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        payload = _build_workwechat_contact_form_payload(
            contact_kind=contact_kind,
            contact_external_id=contact_external_id,
            conversation_external_id=conversation_external_id,
            external_userid=external_userid,
            userid=userid,
            name=name,
            display_name=display_name,
            alias=alias,
            mobile=mobile,
            email=email,
            corp_name=corp_name,
            corp_full_name=corp_full_name,
            corp_id=corp_id,
            position=position,
            avatar_url=avatar_url,
            gender=gender,
            contact_type=contact_type,
            remark=remark,
            owner_userid=owner_userid,
            owner_name=owner_name,
        )
        try:
            contact = upsert_workwechat_contact(db, channel, payload)
        except ValueError as exc:
            return RedirectResponse(
                url=f"/channels/{channel_id}/workwechat/contacts/new?notice={quote(str(exc))}",
                status_code=303,
            )
        db.commit()
        record_audit(db, "channel.workwechat.contact.create", "workwechat_contact", contact.id, f"contact={contact.contact_external_id}, operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{contact.id}?notice={quote('联系人已保存')}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/contacts/{contact_id}/edit")
async def update_workwechat_contact_action(
    request: Request,
    channel_id: int,
    contact_id: int,
    contact_kind: str = Form("external_contact"),
    contact_external_id: str = Form(""),
    conversation_external_id: str = Form(""),
    external_userid: str = Form(""),
    userid: str = Form(""),
    name: str = Form(""),
    display_name: str = Form(""),
    alias: str = Form(""),
    mobile: str = Form(""),
    email: str = Form(""),
    corp_name: str = Form(""),
    corp_full_name: str = Form(""),
    corp_id: str = Form(""),
    position: str = Form(""),
    avatar_url: str = Form(""),
    gender: str = Form("unknown"),
    contact_type: str = Form("unknown"),
    remark: str = Form(""),
    owner_userid: str = Form(""),
    owner_name: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        payload = _build_workwechat_contact_form_payload(
            contact_kind=contact_kind,
            contact_external_id=contact.contact_external_id,
            conversation_external_id=conversation_external_id,
            external_userid=external_userid,
            userid=userid,
            name=name,
            display_name=display_name,
            alias=alias,
            mobile=mobile,
            email=email,
            corp_name=corp_name,
            corp_full_name=corp_full_name,
            corp_id=corp_id,
            position=position,
            avatar_url=avatar_url,
            gender=gender,
            contact_type=contact_type,
            remark=remark,
            owner_userid=owner_userid,
            owner_name=owner_name,
        )
        saved = upsert_workwechat_contact(db, channel, payload)
        db.commit()
        record_audit(db, "channel.workwechat.contact.edit", "workwechat_contact", saved.id, f"contact={saved.contact_external_id}, operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{saved.id}?notice={quote('联系人已更新')}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/contacts/{contact_id}/mark-deleted")
async def mark_workwechat_contact_deleted_action(
    request: Request,
    channel_id: int,
    contact_id: int,
    delete_reason: str = Form("deleted_by_contact"),
    delete_event_key: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        mark_workwechat_contact_deleted(db, contact, reason=delete_reason, event_key=delete_event_key)
        db.commit()
        record_audit(db, "channel.workwechat.contact.mark-deleted", "workwechat_contact", contact.id, f"reason={delete_reason}, operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{contact_id}?notice={quote('联系人已标记为删除')}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/contacts/{contact_id}/restore")
async def restore_workwechat_contact_action(request: Request, channel_id: int, contact_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        restore_workwechat_contact(db, contact)
        db.commit()
        record_audit(db, "channel.workwechat.contact.restore", "workwechat_contact", contact.id, f"operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{contact_id}?notice={quote('联系人已恢复为有效')}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/sync-contacts")
async def sync_workwechat_contacts_action(request: Request, channel_id: int, contact_kind: str = Form("all")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "workwechat":
            raise HTTPException(status_code=400, detail="Channel is not workwechat")
        if is_cloud_poll_enabled(channel):
            # 云端模式下联系人列表必须由客服电脑本地 provider 调 Hook 11036/11037 后回传；
            # 服务端只负责下发控制任务，避免要求配置内网 API Base。
            job = enqueue_job(
                db,
                job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                payload={
                    "channel_id": channel.id,
                    "action": "sync_contacts",
                    "payload": {"contact_kind": contact_kind, "page_size": 100, "max_pages": 50},
                },
                priority=95,
                related_type="channel",
                related_id=channel.id,
                max_attempts=3,
            )
            db.commit()
            notify_cloud_job_enqueued(channel, job)
            message = "已下发企微云端联系人同步任务，请等待客户端执行后刷新联系人列表。"
            record_audit(db, "channel.workwechat.sync-contacts", "channel", channel.id, message)
            return RedirectResponse(url=f"/channels/{channel_id}/workwechat/contacts?notice={quote(message)}", status_code=303)
        overview = sync_workwechat_contacts(db, channel, contact_kind=contact_kind)
        db.commit()
        message = str(overview.get("last_status_summary") or "已同步企微联系人")
        record_audit(db, "channel.workwechat.sync-contacts", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}/workwechat/contacts?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/cloud-poll/authorization-code")
async def issue_workwechat_cloud_poll_authorization_code_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        try:
            channel, code = issue_channel_authorization_code(db, channel_id)
        except ValueError as exc:
            error_map = {
                "channel_not_found": "渠道不存在",
                "workwechat_channel_required": "只有企业微信渠道可以生成云端授权码",
            }
            message = error_map.get(str(exc), f"生成企微云端授权码失败：{exc}")
            return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
        # 授权码是一次性明文，只在本次操作提示里展示；数据库仅保存哈希和过期时间。
        message = f"企微云端授权码：{code}（24小时内使用一次）"
        record_audit(db, "channel.workwechat.cloud-poll.authorization-code", "channel", channel.id, "生成企微云端授权码")
        return RedirectResponse(url=f"/channels/{channel.id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/sync")
async def sync_wechat_ipad_chatroom_action(request: Request, channel_id: int, chatroom_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type == "wechat_ipad":
            overview = sync_wechat_ipad_chatrooms(db, channel, chatroom_ids=[chatroom.external_id])
            message = str(overview.get("last_status_summary") or "已触发单群资料同步")
            record_audit(db, "channel.wechat-ipad.sync-chatroom", "chatroom", chatroom.id, message)
        elif channel.channel_type == "wechat_winauto":
            overview = sync_wechat_winauto_chatrooms(db, channel)
            db.commit()
            message = str(overview.get("last_status_summary") or "已刷新当前群样本")
            record_audit(db, f"channel.{channel.channel_type}.sync-chatroom", "chatroom", chatroom.id, message)
            return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
        elif channel.channel_type == "workwechat":
            if is_cloud_poll_enabled(channel):
                # 云端模式下服务端不能直连客服电脑；先下发群列表任务补中文群名，再下发成员任务补成员快照。
                profile_job = enqueue_job(
                    db,
                    job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                    payload={
                        "channel_id": channel.id,
                        "action": "sync_chatrooms",
                        "target_chatroom_id": chatroom.id,
                        "target_external_id": chatroom.external_id,
                        "payload": {"page_size": 100, "max_pages": 20, "target_external_id": chatroom.external_id},
                    },
                    priority=96,
                    related_type="chatroom",
                    related_id=chatroom.id,
                    max_attempts=3,
                )
                member_job = enqueue_job(
                    db,
                    job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                    payload={
                        "channel_id": channel.id,
                        "action": "sync_chatroom_members",
                        "chatroom_id": chatroom.id,
                        "payload": {"conversation_id": chatroom.external_id, "page_size": 100, "max_pages": 30},
                    },
                    priority=95,
                    related_type="chatroom",
                    related_id=chatroom.id,
                    max_attempts=3,
                )
                db.commit()
                notify_cloud_job_enqueued(channel, profile_job)
                notify_cloud_job_enqueued(channel, member_job)
                message = "已下发企微云端群资料和成员同步任务，请等待客户端执行后刷新当前群。"
                record_audit(db, "channel.workwechat.sync-chatroom", "chatroom", chatroom.id, message)
                return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
            # workwechat 没有单群资料详情接口；这里先用全量群列表只更新当前群，再用 Hook 11040 刷新成员。
            profile_overview = sync_workwechat_chatroom_profile_from_list(db, channel, chatroom)
            member_overview = sync_workwechat_chatroom_members(db, channel, chatroom)
            db.commit()
            profile_summary = str(profile_overview.get("last_status_summary") or "").strip()
            member_summary = str(member_overview.get("last_status_summary") or "").strip()
            message = "；".join(part for part in (profile_summary, member_summary) if part) or "已刷新当前企微群资料和成员"
            record_audit(db, "channel.workwechat.sync-chatroom", "chatroom", chatroom.id, message)
            return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
        elif channel.channel_type == "wechat_webridge":
            # 微桥由手机插件执行：入队 room_members 命令，结果回传后由目录持久化写入成员快照。
            job = enqueue_command(db, channel, command_name="room_members", args={"room_id": chatroom.external_id})
            db.commit()
            message = f"已下发群成员同步，任务 #{job.id}，稍后刷新本页查看"
            record_audit(db, "channel.wechat-webridge.sync-chatroom", "chatroom", chatroom.id, message)
            return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
        else:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat / wechat_webridge")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/chatrooms/create")
async def create_channel_chatroom_action(
    request: Request,
    channel_id: int = Form(...),
    external_id: str = Form(...),
    name: str = Form(""),
    remark: str = Form(""),
    operator_note: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        normalized_external_id = str(external_id or "").strip()
        if not normalized_external_id:
            return RedirectResponse(url=f"/chatrooms?channel_id={channel_id}&notice={quote('群外部 ID 不能为空')}", status_code=303)
        chatroom = upsert_manual_chatroom(
            db,
            channel=channel,
            external_id=normalized_external_id,
            name=name,
            remark=remark,
            operator_note=f"{str(operator_note or '').strip()}".strip() or f"manual created by {user.username}",
        )
        db.commit()
        record_audit(db, f"channel.{channel.channel_type}.chatroom.create", "chatroom", chatroom.id, f"external_id={normalized_external_id}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/chatrooms/{chatroom.id}?notice={quote('已创建群登记，默认待启用')}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/management")
async def update_channel_chatroom_management_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    management_status: str = Form(...),
    next: str = Form(""),
    csrf_token: str = Form(""),
):
    """切换群的运行模式。群详情页的单选框选中即生效(脚本 fetch 提交,返回 JSON);群列表页的行内开关是整页表单提交。"""

    user = enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        set_chatroom_management_status(chatroom, management_status)
        db.commit()
        record_audit(
            db,
            f"channel.{channel.channel_type}.chatroom.management",
            "chatroom",
            chatroom.id,
            f"status={chatroom.management_status}, operator={user.username}",
        )
        message = {
            "enabled": "本群已设为正常运行：采集消息，并响应插件、群功能和 AI",
            "disabled": "本群已设为只采集消息：继续记录消息，但不触发任何功能",
            "archived": "本群已完全关闭：不接收、不记录消息，也不触发任何功能",
        }.get(chatroom.management_status, "已将该群标记为待启用")
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": True, "notice": message, "management_status": chatroom.management_status})
        # 列表页行内开关带 next 回到列表;只接受站内路径,防止开放重定向。
        target = next if next.startswith("/") and not next.startswith("//") else f"/channels/{channel_id}/chatrooms/{chatroom_id}"
        separator = "&" if "?" in target else "?"
        return RedirectResponse(url=f"{target}{separator}notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-winauto/sync-chatrooms")
async def sync_wechat_winauto_chatrooms_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_winauto":
            raise HTTPException(status_code=400, detail="Channel is not wechat_winauto")
        overview = sync_wechat_winauto_chatrooms(db, channel)
        db.commit()
        message = str(overview.get("last_status_summary") or "已刷新 wxauto 群列表")
        record_audit(db, f"channel.{channel.channel_type}.sync-chatrooms", "channel", channel.id, message)
        return RedirectResponse(url=f"/chatrooms?channel_id={channel_id}&notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/workwechat/sync-chatrooms")
async def sync_workwechat_chatrooms_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "workwechat":
            raise HTTPException(status_code=400, detail="Channel is not workwechat")
        if is_cloud_poll_enabled(channel):
            # 云端模式下服务端无法直连客服电脑的 provider，因此这里改为下发控制任务；
            # 在线 provider 会通过 WS 立即领取，离线时也会在下一次 poll 中补偿执行。
            job = enqueue_job(
                db,
                job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                payload={"channel_id": channel.id, "action": "sync_chatrooms", "payload": {"page_size": 100, "max_pages": 20}},
                priority=95,
                related_type="channel",
                related_id=channel.id,
                max_attempts=3,
            )
            db.commit()
            notify_cloud_job_enqueued(channel, job)
            message = "已下发企微云端群列表同步任务，请等待客户端执行后刷新群管理。"
            record_audit(db, "channel.workwechat.sync-chatrooms", "channel", channel.id, message)
            return RedirectResponse(url=f"/chatrooms?channel_id={channel_id}&notice={quote(message)}", status_code=303)
        # workwechat 群列表同步走 provider 的 11038，失败时服务层会保留本地消息样本视图。
        overview = sync_workwechat_chatrooms(db, channel)
        db.commit()
        message = str(overview.get("last_status_summary") or "已同步企微群列表")
        record_audit(db, "channel.workwechat.sync-chatrooms", "channel", channel.id, message)
        return RedirectResponse(url=f"/chatrooms?channel_id={channel_id}&notice={quote(message)}", status_code=303)
    finally:
        db.close()


def _csrf_from_header(request: Request) -> str:
    """页面脚本(fetch)提交时,CSRF 令牌放在 X-CSRF-Token 请求头里(令牌由页面 <meta name="csrf-token"> 提供)。"""

    return request.headers.get("X-CSRF-Token", "")


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/features/{feature_key}")
async def update_chatroom_feature_action(request: Request, channel_id: int, chatroom_id: int, feature_key: str):
    """保存群功能的设置(有文本/数字/密钥输入的设置区点「保存」时提交),存进群记录的 extra_json。

    只服务页面脚本:返回 JSON,页面不刷新,脚本弹出成功提示。
    """

    user = enforce_role(request, "ops")
    form = await request.form()
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        try:
            save_group_feature_form(chatroom, feature_key, form)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        db.commit()
        record_audit(db, "channel.chatroom-feature.update", "chatroom", chatroom.id, f"{feature_key} by {user.username}")
        feature = get_group_feature(feature_key)
        return JSONResponse({"ok": True, "notice": f"已保存「{feature.name}」的设置"})
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/features/{feature_key}/toggle")
async def toggle_chatroom_feature_action(request: Request, channel_id: int, chatroom_id: int, feature_key: str):
    """点开关即保存:body 为 {"field": "enabled" | <某个开关配置项>, "value": true|false},只改这一个开关。"""

    user = enforce_role(request, "ops")
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "notice": "请求格式不正确"}, status_code=400)
    field = str((body or {}).get("field") or "")
    value = (body or {}).get("value")
    if not field or not isinstance(value, bool):
        return JSONResponse({"ok": False, "notice": "请求格式不正确"}, status_code=400)
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        try:
            notice = toggle_group_feature_flag(chatroom, feature_key, field, value)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        db.commit()
        record_audit(db, "channel.chatroom-feature.toggle", "chatroom", chatroom.id, f"{feature_key}.{field}={value} by {user.username}")
        return JSONResponse({"ok": True, "notice": notice})
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/enable")
async def enable_wechat_ipad_chatroom_scope_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            update_wechat_ipad_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.user_id,
                audit_note=f"manual enable by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.user_id,
                audit_note=f"manual enable by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.user_id,
                audit_note=f"manual enable by {user.username}",
            )
        db.commit()
        message = "已将该群的插件草稿作用域设为启用"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.enable", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/disable")
async def disable_wechat_ipad_chatroom_scope_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            update_wechat_ipad_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.user_id,
                audit_note=f"manual disable by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.user_id,
                audit_note=f"manual disable by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.user_id,
                audit_note=f"manual disable by {user.username}",
            )
        db.commit()
        message = "已将该群的插件草稿作用域设为停用"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.disable", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/reset")
async def reset_wechat_ipad_chatroom_scope_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            reset_wechat_ipad_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.user_id,
                audit_note=f"reset to inherited by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            reset_wechat_winauto_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.user_id,
                audit_note=f"reset to inherited by {user.username}",
            )
        else:
            reset_workwechat_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.user_id,
                audit_note=f"reset to inherited by {user.username}",
            )
        db.commit()
        message = "已恢复继承 bot_plugin 绑定状态"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.reset", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/config")
async def update_wechat_ipad_chatroom_scope_config_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    bot_plugin_id: int,
    config_override_json: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        normalized_override = config_override_json.strip()
        normalized_to_save: str | None = None
        if normalized_override:
            parsed_override, config_error = parse_json_object(normalized_override)
            if config_error:
                return RedirectResponse(
                    url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote('群级配置 JSON 无效：' + config_error)}",
                    status_code=303,
                )
            effective_base = build_group_scope_effective_config(
                getattr(binding.plugin, "code", None),
                binding.config_json,
                None,
            )
            computed_override = diff_config_override(effective_base, parsed_override)
            normalized_to_save = dump_config_json(computed_override)
        if channel.channel_type == "wechat_ipad":
            update_wechat_ipad_chatroom_scope_config_override(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                config_override_json=normalized_to_save,
                operator_id=user.user_id,
                audit_note=f"group override updated by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_config_override(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                config_override_json=normalized_to_save,
                operator_id=user.user_id,
                audit_note=f"group override updated by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_config_override(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                config_override_json=normalized_to_save,
                operator_id=user.user_id,
                audit_note=f"group override updated by {user.username}",
            )
        db.commit()
        message = "已保存群级插件配置覆盖" if normalized_to_save else "已清空群级插件配置覆盖，恢复继承默认配置"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.config", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/mode")
async def set_wechat_ipad_chatroom_scope_mode_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    bot_plugin_id: int,
    mode: str = Form(...),
    csrf_token: str = Form(""),
):
    """群内插件的启用/停用/恢复默认。群「插件」标签里的开关是脚本提交(返回 JSON,页面不刷新);恢复默认是整页表单提交。"""

    user = enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        channel = db.get(Channel, channel_id)
        if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Wechat chatroom channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode == "enabled":
            if channel.channel_type == "wechat_ipad":
                update_wechat_ipad_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为启用",
                )
            elif channel.channel_type == "wechat_winauto":
                update_wechat_winauto_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为启用",
                )
            else:
                update_workwechat_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为启用",
                )
            message = "群插件已切换为群内启用"
        elif normalized_mode == "disabled":
            if channel.channel_type == "wechat_ipad":
                update_wechat_ipad_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为停用",
                )
            elif channel.channel_type == "wechat_winauto":
                update_wechat_winauto_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为停用",
                )
            else:
                update_workwechat_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为停用",
                )
            message = "群插件已切换为群内停用"
        elif normalized_mode == "inherited":
            if channel.channel_type == "wechat_ipad":
                reset_wechat_ipad_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.user_id,
                    audit_note="群级开关恢复继承",
                )
            elif channel.channel_type == "wechat_winauto":
                reset_wechat_winauto_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.user_id,
                    audit_note="群级开关恢复继承",
                )
            else:
                reset_workwechat_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.user_id,
                    audit_note="群级开关恢复继承",
                )
            message = "群插件已恢复继承 bot 级状态"
        else:
            raise HTTPException(status_code=400, detail="Invalid scope mode")
        # 群权限模式是运维人员在页面上直接操作的状态，先显式提交业务变更，再记录审计，避免依赖审计函数的隐式提交。
        db.commit()
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.mode", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}, mode={normalized_mode}")
        if request.headers.get("X-Requested-With") == "fetch":
            # custom:这个群是否单独设置过(启用/停用都算自定义,恢复默认才回到继承),页面据此显示"本群已自定义"和"恢复默认"。
            return JSONResponse({"ok": True, "notice": message, "mode": normalized_mode, "custom": normalized_mode != "inherited"})
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}", status_code=303)
    except ValueError as exc:
        db.rollback()
        # 服务层会在绑定和群不匹配等数据异常时抛 ValueError；这里转成页面提示，避免按钮操作直接落到 500 错误页。
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": False, "notice": f"群权限保存失败：{exc}"}, status_code=400)
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote('群权限保存失败：' + str(exc))}", status_code=303)
    finally:
        db.close()


@app.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugins = tenant_visible_plugins(db, _current_tenant_id())
        visible_plugin_ids = {plugin.id for plugin in plugins}
        bindings = [
            item
            for item in db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound", BotPlugin.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None)))).order_by(BotPlugin.id.desc())).all()
            if item.plugin_id in visible_plugin_ids
        ]
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        plugin_runtime_map = {plugin.code: get_plugin_runtime_meta(plugin.code) for plugin in plugins}
        search_query = request.query_params.get("q", "").strip()
        plugin_type_filter = request.query_params.get("plugin_type", "").strip()
        category_filter = request.query_params.get("category", "").strip()
        migration_filter = request.query_params.get("migration_state", "").strip()
        execution_filter = request.query_params.get("execution_state", "").strip()
        manifest_filter = request.query_params.get("manifest_state", "").strip()
        raw_open_bot_id = request.query_params.get("open_bot_id", "").strip()
        try:
            open_bot_id = int(raw_open_bot_id) if raw_open_bot_id else 0
        except ValueError:
            open_bot_id = 0
        filtered_plugins = [
            plugin
            for plugin in plugins
            if plugin_matches_filters(
                plugin,
                plugin_runtime_map.get(plugin.code, {}),
                keyword=search_query,
                plugin_type=plugin_type_filter,
                category=category_filter,
                migration_state=migration_filter,
                execution_state=execution_filter,
                manifest_state=manifest_filter,
            )
        ]
        for binding in bindings:
            binding.runtime_status = compute_binding_runtime(binding)
        return render(
            request,
            "plugins.html",
            page_title="插件管理",
            plugins=filtered_plugins,
            all_plugins=plugins,
            bindings=bindings,
            binding_cards=build_binding_schema_cards_with_dispatch(db, bindings),
            bots=bots,
            plugin_runtime_map=plugin_runtime_map,
            plugin_toggle_matrix=build_plugin_toggle_matrix(bots, filtered_plugins, bindings, plugin_runtime_map),
            runtime_summary=build_plugin_runtime_summary(plugins, bindings),
            plugin_filters={
                "q": search_query,
                "plugin_type": plugin_type_filter,
                "category": category_filter,
                "migration_state": migration_filter,
                "execution_state": execution_filter,
                "manifest_state": manifest_filter,
            },
            plugin_filter_options=build_plugin_filter_options(plugins, plugin_runtime_map),
            filtered_plugin_count=len(filtered_plugins),
            total_plugin_count=len(plugins),
            open_bot_id=open_bot_id,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.post("/plugins/refresh")
async def refresh_plugins_action(request: Request):
    enforce_role(request, "ops")
    db = get_session()
    try:
        rows = sync_plugins_from_directory(db, force=True)
        record_audit(db, "plugin.refresh-directory", "plugin", None, f"count={len(rows)}")
        return RedirectResponse(url="/plugins?notice=" + quote(f"已刷新插件目录，共发现 {len(rows)} 个插件"), status_code=303)
    finally:
        db.close()


@app.get("/plugins/runtime", response_class=HTMLResponse)
async def plugin_runtime_page(request: Request):
    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugins = tenant_visible_plugins(db, _current_tenant_id())
        visible_plugin_ids = {plugin.id for plugin in plugins}
        bindings = [
            item
            for item in db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound", BotPlugin.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None)))).order_by(BotPlugin.id.desc())).all()
            if item.plugin_id in visible_plugin_ids
        ]
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        for binding in bindings:
            binding.runtime_status = compute_binding_runtime(binding)
        return render(
            request,
            "plugin_runtime.html",
            page_title="插件运行状态",
            bindings=bindings,
            binding_cards=build_binding_schema_cards_with_dispatch(db, bindings),
            bot_name_map=bot_name_map,
            plugins=plugins,
            plugin_runtime_map={plugin.code: get_plugin_runtime_meta(plugin.code) for plugin in plugins},
            runtime_summary=build_plugin_runtime_summary(plugins, bindings),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/plugins/{plugin_id}", response_class=HTMLResponse)
async def plugin_detail_page(request: Request, plugin_id: int):
    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugin = _get_tenant_usable_plugin(db, plugin_id)
        bindings = db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound", BotPlugin.plugin_id == plugin_id, BotPlugin.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None)))).order_by(BotPlugin.id.desc())).all()
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        for binding in bindings:
            binding.runtime_status = compute_binding_runtime(binding)
        return render(
            request,
            "plugin_detail.html",
            page_title=plugin.display_name,
            plugin=plugin,
            bindings=bindings,
            binding_cards=build_binding_schema_cards_with_dispatch(db, bindings),
            plugin_config_view=build_plugin_config_view(plugin.code, None),
            plugin_runtime_info=get_plugin_runtime_meta(plugin.code),
            bots=bots,
            bindable_bots=[bot for bot in bots if bot.id not in {binding.bot_id for binding in bindings}],
            bot_name_map=bot_name_map,
            runtime_summary=build_plugin_runtime_summary([plugin], bindings),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


def _current_tenant_id() -> int:
    """当前请求所属租户(租户页面里的插件可见性都按它判断)。"""

    return int(require_tenant_id())


def _get_tenant_usable_plugin(db, plugin_id: int) -> Plugin:
    """取一个当前租户可以使用的插件;不存在、目录里已缺失、被平台停用或对本租户不可见都按 404 处理。"""

    plugin = db.get(Plugin, plugin_id)
    if not plugin or plugin.missing_at is not None or plugin.code in blocked_plugin_codes(db, _current_tenant_id()):
        raise HTTPException(status_code=404, detail="Plugin not found")
    return plugin


def _ensure_binding_plugin_usable(db, binding) -> None:
    """启用或手动执行绑定前确认插件没有被平台停用:绑定数据保留,但此时不能让它跑起来。"""

    if binding.plugin is not None and binding.plugin.code in blocked_plugin_codes(db, _current_tenant_id()):
        raise HTTPException(status_code=403, detail="该插件已被平台停用或对当前租户不可用")


def _safe_plugin_next(value: str, default: str) -> str:
    """表单里的"保存后跳回哪里":只允许本站的 /plugins 路径(防开放重定向),否则用默认页。"""

    candidate = str(value or "").strip()
    if candidate.startswith("/plugins") and "//" not in candidate and "\\" not in candidate and "?" not in candidate and "\n" not in candidate:
        return candidate
    return default


@app.post("/plugins/bind")
async def bind_plugin_action(
    request: Request,
    bot_id: int = Form(...),
    plugin_id: int = Form(...),
    status: str = Form("enabled"),
    config_json: str = Form(""),
    group_default_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        plugin = _get_tenant_usable_plugin(db, plugin_id)
        if get_bot(db, bot_id) is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
        form = await request.form()
        merged_config = merge_schema_config(
            plugin.code,
            form,
            fallback_config_json=config_json or None,
        )
        payload = BotPluginBindCreate(
            bot_id=bot_id,
            plugin_id=plugin_id,
            status=status,
            config_json=merged_config.config_json,
            group_default_enabled=group_default_enabled,
        )
        binding = bind_plugin(
            db,
            payload,
            secret_config_patch=merged_config.secret_patch,
            clear_secret_fields=merged_config.clear_secret_fields,
        )
        record_audit(db, "plugin.bind", "bot_plugin", binding.id, f"bot={binding.bot_id}, plugin={binding.plugin_id}")
        return RedirectResponse(url=f"/plugins/{plugin_id}?notice=" + quote("插件绑定已保存"), status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/unbind")
async def unbind_plugin_action(request: Request, binding_id: int, csrf_token: str = Form("")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if binding is None:
            raise HTTPException(status_code=404, detail="插件绑定不存在")
        plugin_id = binding.plugin_id
        unbind_plugin(db, binding)
        record_audit(db, "plugin.unbind", "bot_plugin", binding_id, f"bot={binding.bot_id}, plugin={plugin_id}")
        return RedirectResponse(url=f"/plugins/{plugin_id}?notice=" + quote("插件已从机器人解绑，历史数据和配置已保留"), status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/toggle")
async def toggle_plugin_binding_action(request: Request, binding_id: int, enabled: bool = Form(...), csrf_token: str = Form("")):
    """插件运行页里某个绑定的启用/停用开关:点击即保存,提交目标状态(幂等),返回 JSON,由页面脚本弹出提示。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        if enabled:
            _ensure_binding_plugin_usable(db, binding)
        if (binding.status == "enabled") != enabled:
            toggle_binding(db, binding)
            record_audit(db, "plugin.toggle", "bot_plugin", binding.id, f"status={binding.status}")
        return JSONResponse({"ok": True, "notice": f"插件绑定已{'启用' if binding.status == 'enabled' else '停用'}", "is_enabled": binding.status == "enabled"})
    finally:
        db.close()


@app.post("/plugins/toggle-direct")
async def toggle_plugin_direct_action(
    request: Request,
    bot_id: int = Form(...),
    plugin_id: int = Form(...),
    enabled: bool = Form(...),
    group_default_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    """插件页里按机器人启停插件的开关:点击即保存,提交目标状态(幂等),返回 JSON 供页面脚本更新状态标签并弹出提示。

    还没绑定过的插件第一次打开时会自动创建绑定并启用;关闭一个没绑定过的插件什么也不用做。
    """

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        plugin = db.get(Plugin, plugin_id)
        bot = get_bot(db, bot_id)
        if not plugin or not bot:
            raise HTTPException(status_code=404, detail="Bot or plugin not found")
        if enabled and plugin.code in blocked_plugin_codes(db, _current_tenant_id()):
            raise HTTPException(status_code=403, detail="该插件已被平台停用或对当前租户不可用")
        binding = db.scalar(select(BotPlugin).where(BotPlugin.bot_id == bot_id, BotPlugin.plugin_id == plugin_id))
        if binding and binding.status != "unbound":
            if (binding.status == "enabled") != enabled:
                toggle_binding(db, binding)
                record_audit(db, "plugin.toggle-direct", "bot_plugin", binding.id, f"status={binding.status}")
        elif enabled:
            binding = bind_plugin(db, BotPluginBindCreate(bot_id=bot_id, plugin_id=plugin_id, status="enabled", config_json=binding.config_json if binding else None, group_default_enabled=group_default_enabled))
            record_audit(db, "plugin.toggle-direct", "bot_plugin", binding.id, "created=enabled")
        elif binding and binding.status == "unbound":
            binding = None
        is_enabled = bool(binding and str(binding.status or "").strip() == "enabled")
        enabled_count = db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot_id, BotPlugin.status == "enabled")) or 0
        total_count = db.scalar(select(func.count(Plugin.id))) or 0
        return JSONResponse(
            {
                "ok": True,
                "notice": f"{bot.name} 的 {plugin.display_name} 已{'启用' if is_enabled else '停用'}",
                "bot_id": bot_id,
                "plugin_id": plugin_id,
                "binding_id": binding.id if binding else None,
                "is_bound": binding is not None,
                "is_enabled": is_enabled,
                "status_label": "已启用" if is_enabled else "已停用",
                "status_tone": "success" if is_enabled else "muted",
                "enabled_count": int(enabled_count),
                "total_count": int(total_count),
            }
        )
    finally:
        db.close()


@app.post("/plugins/{binding_id}/edit")
async def edit_plugin_binding_action(
    request: Request,
    binding_id: int,
    status: str | None = Form(None),
    config_json: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    """保存绑定配置。启用/停用由单独的开关负责,表单里没带 status 时保持原状态;
    页面脚本用 fetch 提交时返回 JSON(弹出提示),普通表单提交则跳回 next(只允许本站 /plugins 路径)。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        form = await request.form()
        plugin_code = binding.plugin.code if binding.plugin else None
        merged_config = merge_schema_config(
            plugin_code,
            form,
            existing_config_json=binding.config_json,
            fallback_config_json=config_json or None,
        )
        update_binding_config(
            db,
            binding,
            status=status or binding.status,
            config_json=merged_config.config_json,
            secret_config_patch=merged_config.secret_patch,
            clear_secret_fields=merged_config.clear_secret_fields,
        )
        record_audit(db, "plugin.update", "bot_plugin", binding.id, f"status={binding.status}")
        notice = "插件绑定配置已保存"
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": True, "notice": notice})
        return RedirectResponse(url=f"{_safe_plugin_next(next, '/plugins/runtime')}?notice={quote(notice)}", status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/reload")
async def reload_plugin_binding_action(request: Request, binding_id: int, csrf_token: str = Form(""), next: str = Form("")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        _, message = reload_binding(db, binding)
        record_audit(db, "plugin.reload", "bot_plugin", binding.id, message)
        return RedirectResponse(url=f"{_safe_plugin_next(next, '/plugins/runtime')}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/execute")
async def execute_plugin_binding_action(request: Request, binding_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        _ensure_binding_plugin_usable(db, binding)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        context = _build_manual_plugin_execution_context(db, binding, payload)
        result = execute_binding_message_action(
            db,
            binding,
            "execute",
            payload=payload,
            context=context,
        )
        if result is None:
            raise HTTPException(status_code=400, detail="Plugin execution not supported")
        persist_binding_execution(db, binding, result, action="execute")
        record_audit(db, "plugin.execute", "bot_plugin", binding.id, result.message)
        return JSONResponse(
            {
                "ok": result.ok,
                "binding_id": binding.id,
                "plugin_code": binding.plugin.code if binding.plugin else None,
                "status": result.status,
                "summary": result.message,
                "output": dict(result.output),
            }
        )
    finally:
        db.close()


def _build_manual_plugin_execution_context(
    db: Session,
    binding: BotPlugin,
    payload: Mapping[str, object],
) -> PluginExecutionContext | None:
    """把员工手工选择的会话校验为当前绑定资源，再构造可信插件上下文。"""

    raw_conversation_id = payload.get("conversation_id")
    if raw_conversation_id in (None, ""):
        return None
    try:
        conversation_id = int(raw_conversation_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="会话编号不合法") from exc
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or int(conversation.bot_id) != int(binding.bot_id):
        raise HTTPException(status_code=400, detail="会话不属于当前插件机器人")
    return PluginExecutionContext(
        tenant_id=int(binding.tenant_id),
        binding_id=int(binding.id),
        bot_id=int(binding.bot_id),
        channel_id=int(conversation.channel_id) if conversation.channel_id else None,
        conversation_id=int(conversation.id),
        channel_type=str(conversation.channel_type or "").strip() or None,
        chat_type=str(conversation.chat_type or "").strip() or None,
    )


# 消息中心单条发送内容的长度上限(微信文本消息本身有上限,这里取一个稳妥值)。
MANUAL_MESSAGE_MAX_CHARS = 2000


@app.post("/messages/send")
async def send_manual_message_action(request: Request, conversation_id: int = Form(...), content: str = Form(...), csrf_token: str = Form("")):
    """消息中心发送框提交的消息;由页面脚本 fetch 调用,返回 JSON,页面不刷新。

    无论发送成败,经渠道适配器出站都会留下一条消息记录(失败的标记为 failed),前端据此把它拉进对话里。
    """

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        text_content = content.strip()
        if not text_content:
            return JSONResponse({"ok": False, "notice": "消息内容不能为空"}, status_code=400)
        if len(text_content) > MANUAL_MESSAGE_MAX_CHARS:
            return JSONResponse({"ok": False, "notice": f"消息太长了，最多 {MANUAL_MESSAGE_MAX_CHARS} 字"}, status_code=400)
        channel = get_channel(db, int(conversation.channel_id or 0)) if conversation.channel_id else None
        if channel is None:
            return JSONResponse({"ok": False, "notice": "会话未绑定渠道，无法发送"}, status_code=400)
        # 必须经过渠道适配器统一出站：微桥会在这里创建手机 poll 要领取的命令任务，
        # 之前只写消息表不入队，所以手机永远收不到后台发出的消息。
        message, adapter_result = dispatch_outbound_message(
            db, channel, conversation, {"message_type": "text", "content": text_content}
        )
        record_audit(db, "message.manual-send", "message", message.id, f"conversation={conversation.id}")
        if not adapter_result.get("ok"):
            notice = f"发送失败：{adapter_result.get('error_message') or adapter_result.get('error_code') or '未知错误'}"
            return JSONResponse({"ok": False, "notice": notice, "message_id": int(message.id)})
        return JSONResponse({"ok": True, "notice": "消息已提交，等待手机执行", "message_id": int(message.id)})
    finally:
        db.close()


@app.get("/media/attachments/{attachment_id}")
async def message_attachment_media(request: Request, attachment_id: int):
    """以当前租户权限提供已落库媒体，供消息中心直接展示图片、音频和视频。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        attachment = db.get(MessageAttachment, int(attachment_id))
        if attachment is None or not attachment.storage_path:
            raise HTTPException(status_code=404, detail="Attachment not found")
        path = Path(str(attachment.storage_path))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file not found")
        return FileResponse(path, media_type=attachment.mime_type or "application/octet-stream", filename=path.name)
    finally:
        db.close()


# 消息中心一次加载的消息条数:首屏、每次向上滚动加载更早的消息、发送后拉取新消息都用这个数。
MESSAGE_PAGE_SIZE = 30


def load_conversation_messages(db: Session, conversation_id: int, *, before_id: int = 0, after_id: int = 0, limit: int = MESSAGE_PAGE_SIZE) -> dict[str, object]:
    """读取会话里的一段消息,按时间从早到晚返回,并附带显示所需的身份和附件。

    - 不带参数:最新的 limit 条(首屏);
    - before_id:比这条更早的最近 limit 条(向上滚动加载更早);
    - after_id:比这条更新的最多 limit 条(发送后拉取新消息)。
    多取一条用来判断两个方向上是否还有更多。
    """

    base = select(Message).where(Message.conversation_id == conversation_id)
    if after_id:
        rows = list(db.scalars(base.where(Message.id > after_id).order_by(Message.id.asc()).limit(limit + 1)).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        if before_id:
            base = base.where(Message.id < before_id)
        fetched = list(db.scalars(base.order_by(Message.id.desc()).limit(limit + 1)).all())
        has_more = len(fetched) > limit
        rows = list(reversed(fetched[:limit]))
    identities = IdentityDirectory(db).for_messages(rows)
    attachments: dict[int, list[MessageAttachment]] = {}
    if rows:
        for attachment in db.scalars(
            select(MessageAttachment).where(MessageAttachment.message_id.in_([int(item.id) for item in rows])).order_by(MessageAttachment.id.asc())
        ).all():
            if attachment.message_id is not None:
                attachments.setdefault(int(attachment.message_id), []).append(attachment)
    # 每条消息解析成展示结构(文字/引用/卡片/媒体占位…),媒体类再补上附件状态(已下载/未下载)
    views = {int(item.id): message_view.attach_media(message_view.describe(item.content, item.message_type), attachments.get(int(item.id), [])) for item in rows}
    return {"messages": rows, "has_more": has_more, "identities": identities, "attachments": attachments, "views": views}


def _media_message(db: Session, message_id: int) -> Message:
    message = db.get(Message, int(message_id)) if message_id else None
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.message_type not in message_media.MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Not a media message")
    return message


@app.post("/messages/media/load")
async def load_message_media(request: Request, message_id: int = Form(...)):
    """点击媒体占位卡片:让手机把这条消息的图片/视频/语音/表情/文件传上来(已有文件或已在获取中则直接返回当前状态)。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        message = _media_message(db, message_id)
        outcome = message_media.request(db, message)
        db.commit()
        return JSONResponse({"ok": outcome["state"] != "failed", **outcome, "notice": outcome["error"]})
    finally:
        db.close()


@app.get("/messages/media/status")
async def message_media_status(request: Request, message_id: int):
    """媒体获取进度(页面轮询):none / loading / ready(带地址)/ failed(带原因)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        message = _media_message(db, message_id)
        outcome = message_media.status(db, message)
        db.commit()  # 状态检查会把失败/超时落实到附件上
        return JSONResponse({"ok": True, **outcome})
    finally:
        db.close()


@app.get("/messages/history")
async def message_history(request: Request, conversation_id: int, before_id: int = 0, after_id: int = 0):
    """消息中心的增量加载接口:返回渲染好的消息行 HTML,前端直接插入到对话里。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        page = load_conversation_messages(db, conversation_id, before_id=max(before_id, 0), after_id=max(after_id, 0))
        macro = templates.env.get_template("_message_rows.html").module.message_rows
        html = str(macro(page["messages"], page["identities"], page["views"])) if page["messages"] else ""
        return JSONResponse({"html": html, "has_more": page["has_more"], "count": len(page["messages"])})
    finally:
        db.close()


def _moments_channels(db: Session) -> list[Channel]:
    """可查看朋友圈的账号:个人微信(微桥)账号,不含已删除的。"""

    return list(db.scalars(select(Channel).where(Channel.channel_type == "wechat_webridge", Channel.deleted_at.is_(None)).order_by(Channel.id.asc())).all())


def _moments_channel(db: Session, channel_id: int) -> Channel:
    channel = get_channel(db, channel_id) if channel_id else None
    if channel is None or channel.channel_type != "wechat_webridge":
        raise HTTPException(status_code=404, detail="WeBridge channel not found")
    return channel


def _moments_post(db: Session, post_id: int) -> MomentsPost:
    post = db.get(MomentsPost, int(post_id)) if post_id else None
    if post is None:
        raise HTTPException(status_code=404, detail="Moments post not found")
    return post


@app.post("/moments/media/load")
async def load_moments_media(request: Request, post_id: int = Form(...), mid: str = Form(...)):
    """点击朋友圈里的图片/视频占位卡片:让手机下载并解密,再传回服务器保存(已有文件或已在获取中则直接返回当前状态)。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        try:
            outcome = moments_media.request(db, _moments_post(db, post_id), mid)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc), "state": "failed", "url": "", "error": str(exc)}, status_code=400)
        db.commit()
        return JSONResponse({"ok": outcome["state"] != "failed", **outcome, "notice": outcome["error"]})
    finally:
        db.close()


@app.get("/moments/media/status")
async def moments_media_status(request: Request, post_id: int, mid: str):
    """朋友圈媒体获取进度(页面轮询):none / loading / ready(带地址)/ failed(带原因)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        post = _moments_post(db, post_id)
        outcome = moments_media.status(db, moments_media.find(db, post.id, mid))
        db.commit()  # 状态检查会把失败/超时落实到记录上
        return JSONResponse({"ok": True, **outcome})
    finally:
        db.close()


@app.get("/moments/media/file/{media_id}")
async def moments_media_file(request: Request, media_id: int):
    """朋友圈媒体文件(手机传回并保存的副本)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        row = db.get(MomentsMedia, int(media_id))
        if row is None or row.status != "downloaded" or not row.storage_path or not Path(row.storage_path).is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(row.storage_path, media_type=row.mime_type or "application/octet-stream", headers={"Cache-Control": "private, max-age=3600"})
    finally:
        db.close()


@app.get("/moments", response_class=HTMLResponse)
async def moments_page(request: Request, channel_id: int = 0):
    """朋友圈:查看从个人微信账号取回并缓存的朋友圈动态;「刷新」「加载更早」向手机发起取回。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        channels = _moments_channels(db)
        channel = next((item for item in channels if int(item.id) == int(channel_id)), channels[0] if channels else None)
        feed = webridge_moments.build_feed(db, channel) if channel else None
        return render(
            request,
            "moments.html",
            page_title="朋友圈",
            channels=channels,
            channel=channel,
            feed=feed,
            summary=webridge_moments.summary(db, channel) if channel else None,
            can_refresh=has_tenant_role(require_user(request), "operator"),
        )
    finally:
        db.close()


@app.get("/moments/feed")
async def moments_feed(request: Request, channel_id: int, before_id: int = 0):
    """朋友圈的增量加载:返回渲染好的动态卡片 HTML,页面直接追加(或整体替换)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        channel = _moments_channel(db, channel_id)
        feed = webridge_moments.build_feed(db, channel, before_id=max(before_id, 0))
        macro = templates.env.get_template("_moment_posts.html").module.moment_posts
        html = str(macro(feed["posts"], feed["identities"])) if feed["posts"] else ""
        return JSONResponse({"html": html, "has_more": feed["has_more"], "last_id": feed["posts"][-1]["id"] if feed["posts"] else 0, "count": len(feed["posts"])})
    finally:
        db.close()


@app.get("/moments/status")
async def moments_status(request: Request, channel_id: int):
    """刷新进度(页面刷新后轮询):idle / running / done / failed,以及新增条数、缓存总数。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        channel = _moments_channel(db, channel_id)
        info = webridge_moments.summary(db, channel)
        refreshed = info["refreshed_at"]
        return JSONResponse({**info, "ok": True, "refreshed_at": refreshed.strftime("%Y-%m-%d %H:%M:%S") if refreshed else ""})
    finally:
        db.close()


@app.post("/moments/refresh")
async def moments_refresh(request: Request, channel_id: int = Form(...), direction: str = Form("head")):
    """向手机发起一次朋友圈取回(head 刷新最新 / tail 取更早的)。脚本提交,返回 JSON;结果由 /moments/status 查询。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        channel = _moments_channel(db, channel_id)
        try:
            outcome = webridge_moments.request_refresh(db, channel, direction)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        db.commit()
        if outcome["started"]:
            record_audit(db, "moments.refresh", "channel", channel.id, f"direction={direction}")
            return JSONResponse({"ok": True, "started": True, "notice": "正在从手机取回朋友圈…"})
        return JSONResponse({"ok": True, "started": False, "notice": outcome["reason"]})
    finally:
        db.close()


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    bot_id: int = 0,
    channel_id: int = 0,
    chat_type: str = "",
    conversation_id: int = 0,
):
    db = get_session()
    try:
        normalized_chat_type = chat_type.strip().lower()
        if normalized_chat_type not in {"group", "direct"}:
            normalized_chat_type = ""

        # 已删除的账号/机器人(逻辑删除)的会话不再出现在列表里,聊天数据本身保留。
        conversations_stmt = select(Conversation).where(
            Conversation.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None))),
            or_(Conversation.channel_id.is_(None), Conversation.channel_id.in_(select(Channel.id).where(Channel.deleted_at.is_(None)))),
        )
        if bot_id:
            conversations_stmt = conversations_stmt.where(Conversation.bot_id == bot_id)
        if channel_id:
            conversations_stmt = conversations_stmt.where(Conversation.channel_id == channel_id)
        if normalized_chat_type:
            conversations_stmt = conversations_stmt.where(Conversation.chat_type == normalized_chat_type)
        conversations = db.scalars(
            conversations_stmt.order_by(
                Conversation.last_message_at.is_(None),
                Conversation.last_message_at.desc(),
                Conversation.id.desc(),
            ).limit(100)
        ).all()
        conversation_summaries = []
        summary_directory = IdentityDirectory(db)
        summary_identities = summary_directory.for_conversations(conversations)
        for item in conversations:
            latest = db.scalar(
                select(Message).where(Message.conversation_id == item.id).order_by(Message.id.desc()).limit(1)
            )
            identity = summary_identities[int(item.id)]
            display_name = identity["name"] or item.title or item.external_id or f"会话 #{item.id}"
            conversation_summaries.append(
                {
                    "conversation": item,
                    "latest": latest,
                    "display_name": display_name,
                    "initial": display_name[:1].upper(),
                    "identity": identity,
                }
            )

        # 只有选中了会话才读消息;没有分页,更早的消息由页面向上滚动时通过 /messages/history 继续加载。
        page = load_conversation_messages(db, conversation_id) if conversation_id else {"messages": [], "has_more": False, "identities": {}, "attachments": {}, "views": {}}
        selected_total = int(db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conversation_id)) or 0) if conversation_id else None
        # 选中群会话时,找到对应的群记录,线程头据此提供“群设置”入口。
        selected_chatroom = None
        selected_summary = next((row for row in conversation_summaries if int(row["conversation"].id) == conversation_id), None)
        if selected_summary and selected_summary["conversation"].chat_type == "group":
            selected_chatroom = db.scalar(
                select(Chatroom).where(
                    Chatroom.channel_id == selected_summary["conversation"].channel_id,
                    Chatroom.external_id == selected_summary["conversation"].external_id,
                )
            )
        return render(
            request,
            "messages.html",
            page_title="消息中心",
            messages=page["messages"],
            message_identities=page["identities"],
            message_views=page["views"],
            has_more_messages=page["has_more"],
            selected_total=selected_total,
            conversation_summaries=conversation_summaries,
            selected_conversation_id=conversation_id,
            selected_chatroom=selected_chatroom,
            channel_id=channel_id,
        )
    finally:
        db.close()


@app.post("/webhooks/{channel_id}/inbound")
async def inbound_webhook(channel_id: int, request: Request):
    body = await request.body()
    # URL 中的 channel_id 只能用于定位待校验凭证。平台会话完成签名预检后只保留
    # 可信 tenant_id 和渠道主键，后续事件、日志、告警及审计全部在租户会话中处理。
    with platform_session(SessionLocal) as auth_db:
        channel = get_channel(auth_db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type == "wechat_webridge":
            # 微桥必须走带 Token 和设备身份校验的专用入口，不能绕过账号确认。
            raise HTTPException(status_code=403, detail="请使用微桥专属长轮询地址")

        secret = decrypt_secret(channel.bot_token_encrypted)
        if channel.channel_type == "telegram":
            expected_secret = build_telegram_webhook_secret(secret, channel.id)
            provided_signature = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            signature_valid = verify_telegram_webhook_secret(expected_secret, provided_signature)
        else:
            provided_signature = request.headers.get("X-Signature") or request.headers.get("X-Abot-Signature")
            timestamp = request.headers.get("X-Timestamp") or request.headers.get("X-Abot-Timestamp") or ""
            signature_valid = verify_webhook_signature(secret, body, provided_signature, timestamp=timestamp)
        credential = (int(channel.tenant_id), int(channel.id))

    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        if not signature_valid:
            db.add(ErrorLog(source="webhook", level="ERROR", message=f"渠道 #{channel.id} Webhook 签名校验失败"))
            db.commit()
            create_alert(
                db,
                "webhook",
                f"渠道 {channel.name} Webhook 签名失败",
                "收到的请求签名与共享密钥不匹配",
                level="critical",
                related_type="channel",
                related_id=channel.id,
            )
            record_health_check(
                db,
                "webhook-signature",
                "channel",
                "failed",
                "Webhook 签名校验失败",
                target_id=channel.id,
                details={"header": provided_signature or "-", "channel_type": channel.channel_type},
            )
            record_audit(db, "webhook.reject", "channel", channel.id, "签名校验失败")
            return JSONResponse(status_code=401, content={"ok": False, "error": "invalid_signature"})

        payload = json.loads(body.decode("utf-8") or "{}")
        result = ingest_inbound_event(
            db,
            channel,
            payload,
            headers={key: value for key, value in request.headers.items()},
            source=channel.channel_type,
        )
        normalized = result["normalized"]
        event_key = str(normalized.get("event_key") or "")
        if result["deduplicated"]:
            if channel.channel_type == "wechat_ipad":
                record_wechat_ipad_receive_event(
                    db,
                    channel,
                    event_key=event_key or None,
                    mapped=bool(result["mapped"]),
                    deduplicated=True,
                    source=channel.channel_type,
                )
            record_audit(db, "webhook.deduplicated", "raw_webhook_event", result["raw_event_id"], f"event_key={event_key}")
            return {"ok": True, "deduplicated": True, "event_id": result["raw_event_id"]}

        db.commit()
        if channel.channel_type == "wechat_ipad":
            record_wechat_ipad_receive_event(
                db,
                channel,
                event_key=event_key or None,
                mapped=bool(result["mapped"]),
                deduplicated=False,
                source=channel.channel_type,
            )
        record_health_check(
            db,
            "webhook-inbound",
            "channel",
            "ok",
            "入站 Webhook 已接收并处理",
            target_id=channel.id,
            details={
                "event_key": event_key or "-",
                "mapped": result["mapped"],
                "adapter_type": normalized.get("adapter_type"),
                "event_type": normalized.get("event_type"),
                "adapter_details": normalized.get("details"),
                "dispatch": result.get("dispatch"),
            },
        )
        record_audit(db, "webhook.accept", "raw_webhook_event", result["raw_event_id"], f"mapped={result['mapped']}")
        return {
            "ok": True,
            "event_id": result["raw_event_id"],
            "mapped": result["mapped"],
            "event_type": normalized.get("event_type"),
            "dispatch": result.get("dispatch"),
        }


@app.post("/channels/{channel_id}/outbound")
async def outbound_message_action(channel_id: int, request: Request):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        payload = await request.json()
        conversation = None
        conversation_id = payload.get("conversation_id")
        if conversation_id:
            conversation = db.get(Conversation, int(conversation_id))
            if conversation and (conversation.channel_id != channel.id or conversation.bot_id != channel.bot_id):
                raise HTTPException(status_code=400, detail="conversation does not belong to channel")

        if not conversation:
            external_id = str(payload.get("external_id") or payload.get("receiver_id") or f"manual-{channel.id}")
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.bot_id == channel.bot_id,
                    Conversation.channel_id == channel.id,
                    Conversation.external_id == external_id,
                )
            )
            if not conversation:
                conversation = Conversation(
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    channel_type=channel.channel_type,
                    external_id=external_id,
                    title=str(payload.get("title") or payload.get("receiver_name") or external_id),
                    chat_type=str(payload.get("chat_type") or "direct"),
                )
                db.add(conversation)
                db.flush()

        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": payload,
            },
            priority=90,
            related_type="conversation",
            related_id=conversation.id,
        )
        notify_cloud_job_enqueued(channel, job)
        db.commit()
        record_audit(db, "message.outbound-queued", "task_job", job.id, f"channel={channel.id}, conversation={conversation.id}")
        return {
            "ok": True,
            "queued": True,
            "job_id": job.id,
            "conversation_id": conversation.id,
            "adapter": {"provider": "queued", "transport_status": "queued", "status": "pending"},
        }
    finally:
        db.close()
