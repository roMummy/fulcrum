import json
import subprocess
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import channel_inbound_router, channel_outbound_router, customer_operations_router, open_api_admin_router, open_api_router, provider_router, tool_api_router, workwechat_provider_cloud_router
from app.core.logging import configure_logging, get_logger
from app.core.settings import settings
from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session, platform_session, tenant_session
from app.core.time import SHANGHAI_TZ, ensure_shanghai_datetime, format_shanghai_time, hour_bucket_shanghai, now_shanghai
from app.models.entities import AlertRecord, AuditLog, Bot, BotPlugin, Channel, Chatroom, Conversation, ErrorLog, HealthCheckRecord, Message, MessageActivityHourly, MessageAttachment, Plugin, PluginActivityHourly, PluginExecutionLog, RawWebhookEvent, TaskJob, WorkWechatContact
from app.schemas.forms import BotCreate, BotPluginBindCreate, BotUpdate, ChannelCreate, ChannelUpdate
from app.services.auth import validate_password_change
from app.services.async_workers import start_background_workers, stop_background_workers
from app.services.channel_adapters import get_channel_adapter
from app.services.channel_events import ingest_inbound_event, ingest_normalized_event
from app.services.channel_labels import get_channel_type_label
from app.services.chatroom_registry import set_chatroom_management_status, upsert_manual_chatroom
from app.services.crud import (
    bind_plugin,
    create_bot,
    create_channel,
    delete_bot,
    get_binding,
    get_bot,
    get_channel,
    toggle_binding,
    toggle_bot,
    toggle_channel,
    update_binding_config,
    update_bot,
    update_channel,
)
from app.services.ops import (
    build_monitoring_snapshot,
    create_alert,
    record_audit,
    record_health_check,
)
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.plugin_schemas import (
    build_plugin_config_view,
    deep_merge_config,
    diff_config_override,
    dump_config_json,
    get_plugin_config_schema,
    merge_schema_config,
    parse_config_json,
)
from app.services.plugin_runtime import execute_binding_action, get_plugin_runtime_meta
from app.services.init_db import init_db_with_seed
from app.services.installer import (
    build_default_sqlite_url,
    build_mariadb_url,
    generate_secret_key,
    is_setup_required,
    provision_database,
    validate_database_connection,
    write_env_file,
)
from app.services.runtime import (
    append_manual_message,
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
    logout_wechat_ipad,
    poll_wechat_ipad_login_status,
    pull_wechat_ipad_messages,
    record_wechat_ipad_receive_event,
    record_wechat_ipad_send_event,
    reconnect_wechat_ipad,
    run_wechat_ipad_action,
    reset_wechat_ipad_chatroom_scope,
    resolve_wechat_ipad_qrcode_image,
    refresh_wechat_ipad_qrcode,
    refresh_wechat_ipad_status,
    start_wechat_ipad_login,
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
from app.web import platform_admin_router, platform_auth_router, tenant_admin_router, tenant_auth_router
from app.web.auth_common import (
    PLATFORM_LOGIN_PATH,
    PLATFORM_SESSION_COOKIE,
    TENANT_CSRF_COOKIE,
    TENANT_LOGIN_PATH,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    TENANT_SESSION_COOKIE,
    platform_must_change_password_redirect,
    tenant_must_change_password_redirect,
)
configure_logging()
logger = get_logger(__name__)

# 应用元数据统一从 Settings 读取，部署时可以覆盖名称，但默认明确标识 SaaS 服务。
app = FastAPI(title=settings.app_name, version="1.0.0")
APP_STARTED_AT = now_shanghai()
APP_ROOT = Path(__file__).resolve().parents[1]
SETUP_PENDING_RESTART_FILE = APP_ROOT / ".run" / "setup_pending_restart.json"

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(tool_api_router)
app.include_router(channel_outbound_router)
app.include_router(channel_inbound_router)
app.include_router(customer_operations_router)
app.include_router(open_api_router)
app.include_router(open_api_admin_router)
app.include_router(workwechat_provider_cloud_router)
app.include_router(provider_router)
# 平台和租户认证使用独立 Router 与 Cookie，主应用只负责统一注册和请求身份恢复。
app.include_router(tenant_auth_router)
app.include_router(platform_auth_router)
app.include_router(platform_admin_router)
app.include_router(tenant_admin_router)
templates = Jinja2Templates(directory="app/templates")
BUILTIN_CHANNEL_TYPES = ("wechat_ipad", "wechat_winauto", "workwechat", "openclaw", "simulator", "telegram")
logger.info("application module initialized", extra={"app_env": settings.app_env, "log_dir": settings.log_dir})


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
        if not is_setup_required(db):
            clear_setup_pending_restart_marker()
    finally:
        db.close()
    start_background_workers()
    log_startup_banner()


@app.on_event("shutdown")
async def shutdown_event() -> None:
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


def read_setup_pending_restart_marker() -> dict[str, str]:
    try:
        if not SETUP_PENDING_RESTART_FILE.exists():
            return {}
        raw = json.loads(SETUP_PENDING_RESTART_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def write_setup_pending_restart_marker(*, database_mode: str, database_url: str, admin_username: str) -> None:
    SETUP_PENDING_RESTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "database_mode": str(database_mode or "").strip(),
        "database_url": str(database_url or "").strip(),
        "admin_username": str(admin_username or "").strip(),
        "created_at": now_shanghai().isoformat(),
    }
    SETUP_PENDING_RESTART_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_setup_pending_restart_marker() -> None:
    try:
        SETUP_PENDING_RESTART_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def get_cron_probe_snapshot() -> dict[str, str]:
    probe_script = APP_ROOT / "scripts" / "mba_cron_probe.sh"
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
        "dashboard": "/",
        "bots": "/bots",
        "channels": "/channels",
        "plugins": "/plugins",
        "plugin_runtime": "/plugins/runtime",
        "conversations": "/conversations",
        "messages": "/messages",
        "lab": "/lab",
        "monitoring": "/monitoring",
        "errors": "/errors",
    }


def detail_fallback(path: str) -> str:
    return path


def filtered_bots_query(db: Session):
    return db.scalars(select(Bot).order_by(Bot.id.desc())).all()


def build_bot_name_map(bots: list[Bot]) -> dict[int, str]:
    return {int(bot.id): str(bot.name or "").strip() or f"Bot #{bot.id}" for bot in bots}


def build_channel_chatroom_summary(list_view: dict[str, Any] | None) -> dict[str, Any] | None:
    if not list_view:
        return None
    items = list(list_view.get("items") or [])
    # 渠道详情页只需要一屏摘要；完整群管理、成员和消息流水仍进入 /channels/{id}/chatrooms 查看。
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
    return db.scalars(select(Channel).order_by(Channel.id.desc())).all()


def filtered_bindings_query(db: Session):
    return db.scalars(select(BotPlugin).order_by(BotPlugin.id.desc())).all()


def render(request: Request, template: str, **context):
    notice = str(context.get("notice", "") or "").strip()
    response_status = int(context.pop("response_status", 200) or 200)
    notice_tone = context.get("notice_tone")
    if not notice_tone:
        notice_tone = "danger" if response_status >= 400 else infer_notice_tone(notice)
    path = request.url.path

    def is_path_active(prefixes: tuple[str, ...], *, exact_root: bool = False) -> bool:
        """按任务流判断激活态，避免顶部菜单只按单一路由前缀高亮。"""
        if exact_root:
            return path == "/"
        return any(path == prefix or (prefix != "/" and path.startswith(prefix)) for prefix in prefixes)

    def nav_item(label: str, href: str, prefixes: tuple[str, ...], *, exact_root: bool = False) -> dict[str, str | bool]:
        return {
            "label": label,
            "href": href,
            "active": is_path_active(prefixes, exact_root=exact_root),
        }

    # 顶部菜单按真实使用路线重排：总览、接入、机器人、插件、消息、排障。
    primary_nav_items = [
        nav_item("总览", "/", ("/",), exact_root=True),
        nav_item("接入", "/channels", ("/channels",)),
        nav_item("机器人", "/bots", ("/bots",)),
        nav_item("插件", "/plugins", ("/plugins",)),
        nav_item("消息", "/conversations", ("/conversations", "/messages", "/lab")),
        nav_item("开放接口", "/open-api/apps", ("/open-api",)),
        nav_item("排障", "/monitoring", ("/monitoring", "/errors")),
    ]
    secondary_nav_items: list[dict[str, str | bool]] = [
        nav_item("租户设置", "/tenant/users", ("/tenant",)),
    ]
    nav_items = [*primary_nav_items, *secondary_nav_items]
    secondary_nav_active = any(bool(item["active"]) for item in secondary_nav_items)

    # 二级入口只放当前任务域内的高频分支，避免顶部菜单横向堆满，同时让用户进入模块后能继续按业务路径切换。
    section_nav_items: list[dict[str, str | bool]] = []
    if is_path_active(("/channels",)):
        section_nav_items = [
            nav_item("渠道清单", "/channels", ("/channels",)),
        ]
    elif is_path_active(("/bots",)):
        section_nav_items = [
            nav_item("机器人", "/bots", ("/bots",)),
        ]
    elif is_path_active(("/plugins",)):
        section_nav_items = [
            {
                "label": "插件开关",
                "href": "/plugins",
                "active": path == "/plugins" or (path.startswith("/plugins/") and not path.startswith("/plugins/runtime")),
            },
            nav_item("运行状态", "/plugins/runtime", ("/plugins/runtime",)),
        ]
    elif is_path_active(("/conversations", "/messages", "/lab")):
        section_nav_items = [
            nav_item("会话", "/conversations", ("/conversations",)),
            nav_item("消息", "/messages", ("/messages",)),
            nav_item("实验台", "/lab", ("/lab",)),
        ]
    elif is_path_active(("/open-api",)):
        section_nav_items = [
            nav_item("应用配置", "/open-api/apps", ("/open-api/apps",)),
        ]
    elif is_path_active(("/monitoring", "/errors")):
        section_nav_items = [
            nav_item("监控审计", "/monitoring", ("/monitoring",)),
            nav_item("日志", "/errors", ("/errors",)),
        ]
    elif is_path_active(("/tenant",)):
        section_nav_items = [
            nav_item("员工管理", "/tenant/users", ("/tenant/users",)),
            nav_item("订阅信息", "/tenant/subscription", ("/tenant/subscription",)),
        ]
    base_context = {
        "current_user": getattr(request.state, "current_user", None),
        # 所有租户页面共用同一个可信 CSRF Cookie；状态变更表单必须把原始值放入
        # 隐藏字段，由对应路由再与数据库会话摘要进行三方校验。
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "logout_action": TENANT_LOGOUT_PATH,
        "password_action": TENANT_PASSWORD_PATH,
        "app_started_at": APP_STARTED_AT,
        "notice_tone": notice_tone,
        "nav_items": nav_items,
        "primary_nav_items": primary_nav_items,
        "secondary_nav_items": secondary_nav_items,
        "secondary_nav_active": secondary_nav_active,
        "section_nav_items": section_nav_items,
        "module_links": build_module_links(),
    }
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


def extract_wechat_ipad_proxy_form(adapter_config_json: str | None) -> dict[str, str]:
    parsed, _ = parse_json_object(adapter_config_json or "")
    proxy = parsed.get("proxy")
    if not isinstance(proxy, dict):
        return {
            "host": "",
            "port": "",
            "username": "",
            "password": "",
            "proxy_ip": "",
        }
    proxy_ip = str(proxy.get("proxy_ip") or "").strip()
    host = str(proxy.get("host") or proxy.get("ip") or "").strip()
    port = str(proxy.get("port") or "").strip()
    if (not host or not port) and proxy_ip and ":" in proxy_ip:
        resolved_host, resolved_port = proxy_ip.rsplit(":", 1)
        host = host or resolved_host.strip()
        port = port or resolved_port.strip()
    return {
        "host": host,
        "port": port,
        "username": str(proxy.get("username") or proxy.get("user") or "").strip(),
        "password": str(proxy.get("password") or "").strip(),
        "proxy_ip": proxy_ip,
    }


def merge_channel_adapter_config(
    *,
    channel_type: str,
    adapter_config_json: str,
    proxy_host: str = "",
    proxy_port: str = "",
    proxy_username: str = "",
    proxy_password: str = "",
) -> tuple[str, str]:
    parsed, error = parse_json_object(adapter_config_json)
    if error:
        return "", error
    if str(channel_type or "").strip() != "wechat_ipad":
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True) if parsed else "", ""

    host = str(proxy_host or "").strip()
    port = str(proxy_port or "").strip()
    username = str(proxy_username or "").strip()
    password = str(proxy_password or "").strip()
    proxy_ip = f"{host}:{port}" if host and port else ""
    if any([host, port, username, password]):
        parsed["proxy"] = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "proxy_ip": proxy_ip,
        }
    else:
        parsed.pop("proxy", None)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True) if parsed else "", ""


def parse_message_lines(raw_text: str) -> list[str]:
    return [line.strip() for line in (raw_text or "").splitlines() if line.strip()]


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
                "environment": environment or "all",
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
            .join(Bot, Conversation.bot_id == Bot.id)
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
        .join(Conversation, Conversation.bot_id == Bot.id)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.created_at >= since_24h, Message.direction == "in")
        .group_by(Bot.id, Bot.name)
        .order_by(func.count(Message.id).desc(), Bot.id.asc())
    ).all()
    chatroom_rows = db.execute(
        select(
            Conversation.external_id,
            Conversation.title,
            func.count(Message.id).label("message_count"),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .join(Bot, Conversation.bot_id == Bot.id)
        .where(
            Conversation.chat_type == "group",
            Message.direction == "in",
            Message.created_at >= since_24h,
        )
        .group_by(Conversation.external_id, Conversation.title)
        .order_by(func.count(Message.id).desc(), Conversation.external_id.asc())
        .limit(10)
    ).all()

    user_rows = db.execute(
        select(
            Message.sender_id,
            func.max(Message.sender_name).label("sender_name"),
            func.count(Message.id).label("message_count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .join(Bot, Conversation.bot_id == Bot.id)
        .where(
            Message.direction == "in",
            Message.sender_id.is_not(None),
            Message.created_at >= since_24h,
        )
        .group_by(Message.sender_id)
        .order_by(func.count(Message.id).desc(), Message.sender_id.asc())
        .limit(10)
    ).all()

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
            {"label": str(row.title or row.external_id or "-"), "sub_label": str(row.external_id or "-"), "value": int(row.message_count or 0)}
            for row in chatroom_rows
        ],
        "users": [
            {"label": str(row.sender_name or row.sender_id or "-"), "sub_label": str(row.sender_id or "-"), "value": int(row.message_count or 0)}
            for row in user_rows
        ],
        "plugins": [
            {"label": str(row.plugin_code or "-"), "value": int(row.trigger_count or 0), "sub_label": f"自动出站 {int(row.outbound_count or 0)}"}
            for row in plugin_rows
        ],
    }


def build_dashboard_alert_summary(db: Session) -> dict[str, object]:
    open_alerts = db.scalars(select(AlertRecord).where(AlertRecord.status == "open").order_by(AlertRecord.id.desc()).limit(50)).all()
    level_counts = {
        "critical": sum(1 for item in open_alerts if str(item.level or "").lower() == "critical"),
        "warning": sum(1 for item in open_alerts if str(item.level or "").lower() == "warning"),
        "info": sum(1 for item in open_alerts if str(item.level or "").lower() == "info"),
    }
    recent_items = [
        {
            "title": str(item.title or "-"),
            "message": str(item.message or "-"),
            "level": str(item.level or "warning").lower(),
            "created_at": item.created_at,
        }
        for item in open_alerts[:5]
    ]
    return {
        "total": len(open_alerts),
        "level_counts": level_counts,
        "recent_items": recent_items,
    }

def ensure_conversation(
    db: Session,
    *,
    channel: Channel,
    external_id: str,
    title: str,
    chat_type: str,
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.bot_id == channel.bot_id,
            Conversation.channel_id == channel.id,
            Conversation.external_id == external_id,
        )
    )
    if conversation:
        if title and not conversation.title:
            conversation.title = title
        if chat_type and not conversation.chat_type:
            conversation.chat_type = chat_type
        return conversation
    conversation = Conversation(
        bot_id=channel.bot_id,
        channel_id=channel.id,
        channel_type=channel.channel_type,
        external_id=external_id,
        title=title or external_id,
        chat_type=chat_type or "direct",
    )
    db.add(conversation)
    db.flush()
    return conversation


def build_lab_page_context(
    db: Session,
    *,
    notice: str = "",
    inbound_form: dict[str, object] | None = None,
    outbound_form: dict[str, object] | None = None,
    inbound_result: dict[str, object] | None = None,
    outbound_result: dict[str, object] | None = None,
    active_conversation: Conversation | None = None,
    active_messages: list[Message] | None = None,
):
    channels = filtered_channels_query(db)
    preferred_channel = next((item for item in channels if item.channel_type == "simulator"), channels[0] if channels else None)
    recent_conversations = db.scalars(select(Conversation).order_by(Conversation.id.desc()).limit(12)).all()
    return {
        "page_title": "消息实验台",
        "notice": notice,
        "channels": channels,
        "recent_conversations": recent_conversations,
        "active_conversation": active_conversation,
        "active_messages": active_messages or [],
        "inbound_form": inbound_form
        or {
            "channel_id": preferred_channel.id if preferred_channel else 0,
            "conversation_external_id": "lab-room-001",
            "conversation_title": "实验台会话",
            "chat_type": "group",
            "sender_id": "lab-user-001",
            "sender_name": "实验用户",
            "message_type": "text",
            "content": "/weather Shanghai",
            "event_type": "lab.message",
            "event_key": "",
            "mentioned_self": False,
            "dry_run_outbound": False if preferred_channel and preferred_channel.channel_type == "simulator" else True,
            "recent_messages_text": "",
        },
        "outbound_form": outbound_form
        or {
            "channel_id": preferred_channel.id if preferred_channel else 0,
            "conversation_external_id": "lab-room-001",
            "conversation_title": "实验台会话",
            "chat_type": "group",
            "message_type": "text",
            "content": "实验台手工出站消息",
            "extra_payload_json": "",
            "submit_mode": "preview",
        },
        "inbound_result": inbound_result or {},
        "outbound_result": outbound_result or {},
    }


def load_lab_conversation(
    db: Session,
    *,
    channel_id: int | None,
    external_id: str | None,
) -> tuple[Conversation | None, list[Message]]:
    if not channel_id or not external_id:
        return None, []
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.channel_id == channel_id,
            Conversation.external_id == external_id,
        )
    )
    if not conversation:
        return None, []
    messages = db.scalars(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id.desc()).limit(50)
    ).all()
    return conversation, messages


def to_jsonable(value):
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def build_wechat_ipad_flow_payload(db: Session, channel: Channel) -> dict[str, object]:
    group_summary = build_wechat_ipad_group_summary(db, channel)
    overview = build_wechat_ipad_overview(channel, group_summary=group_summary)
    return {
        "ok": True,
        "overview": to_jsonable(overview),
        "group_summary": to_jsonable(group_summary),
        "group_summary_version": {
            "chatroom_count": int(group_summary.get("chatroom_count") or 0),
            "member_count": int(group_summary.get("member_count") or 0),
            "scope_total": int((group_summary.get("persisted_scope_stats") or {}).get("total") or 0),
            "recent_chatrooms_len": len(group_summary.get("recent_chatrooms") or []),
        },
        "message": str(overview.get("last_status_summary") or ""),
    }


async def resolve_wechat_ipad_login_profile(request: Request) -> str | None:
    content_type = str(request.headers.get("content-type") or "").lower()
    profile = None
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            profile = payload.get("login_profile")
    if profile in (None, ""):
        try:
            form = await request.form()
        except Exception:
            form = None
        if form is not None:
            profile = form.get("login_profile")
    if profile in (None, ""):
        profile = request.query_params.get("login_profile")
    normalized = str(profile or "").strip().lower()
    return normalized or None


def get_session() -> Session:
    """返回绑定当前已认证租户的业务会话。"""

    return open_tenant_session(TenantSessionLocal)


def get_platform_session() -> Session:
    """返回认证、安装和机器身份预检使用的显式平台会话。"""

    db = SessionLocal()
    try:
        init_db_with_seed(db)
        return db
    except Exception:
        logger.exception("failed to initialize database session")
        db.close()
        raise


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
        config_view = build_plugin_config_view(plugin_code, binding.config_json)
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
    binding_map = {(binding.bot_id, binding.plugin_id): binding for binding in bindings}
    rows: list[dict[str, object]] = []
    for bot in bots:
        plugin_items: list[dict[str, object]] = []
        enabled_count = 0
        for plugin in plugins:
            runtime_info = plugin_runtime_map.get(plugin.code, {})
            binding = binding_map.get((bot.id, plugin.id))
            is_enabled = str(binding.status or "").strip() == "enabled" if binding else bool(plugin.default_enabled)
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


def build_execution_payload(db: Session, binding: BotPlugin, payload: dict[str, object]) -> dict[str, object]:
    plugin_code = binding.plugin.code if binding.plugin else None
    if plugin_code != "message_summary":
        return payload

    conversation_id_raw = payload.get("conversation_id")
    try:
        conversation_id = int(conversation_id_raw) if conversation_id_raw is not None else 0
    except (TypeError, ValueError):
        conversation_id = 0
    if not conversation_id:
        return payload

    config = parse_config_json(binding.config_json)
    limit = int(config.get("max_messages", 100) or 100)
    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    enriched = dict(payload)
    enriched["messages"] = [item.content for item in reversed(messages) if (item.content or "").strip()]
    return enriched


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

    # 云端企微 Provider 接口是桌面客户端的机器到机器入口，不能依赖后台登录 Cookie。
    # 这些接口内部会分别校验一次性授权码或 client token，因此这里只放行到业务路由。
    public_prefixes = (
        "/static/",
        "/webhooks/",
        # Open API 自身使用 X-App-Key/X-Timestamp/X-Signature 鉴权，不能再套后台登录态。
        "/api/open/",
        "/outbound-files/",
        "/inbound-files/",
        "/api/workwechat/customer-operations",
        "/api/workwechat/provider",
        "/api/provider",
    )
    public_paths = {
        "/healthz",
        "/setup",
        "/setup/completed",
        TENANT_LOGIN_PATH,
        PLATFORM_LOGIN_PATH,
    }
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

    if request.state.setup_required and request.url.path not in {"/setup", "/healthz"} and not request.url.path.startswith("/static/"):
        return RedirectResponse(url="/setup", status_code=303)

    path = request.url.path
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
    return {"ok": True, "service": "multi-bot-saas"}


@app.get("/setup/completed", response_class=HTMLResponse)
async def setup_completed_page(request: Request):
    return render(
        request,
        "setup_completed.html",
        page_title="安装完成",
        notice=request.query_params.get("notice", ""),
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_session()
    try:
        recent_cutoff = now_shanghai() - timedelta(hours=24)
        today_cutoff = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
        # 总览始终统计当前租户的完整数据集；机器人环境已经从产品和数据库中删除。
        stats = {
            "bots": db.scalar(select(func.count(Bot.id))) or 0,
            "channels": db.scalar(select(func.count(Channel.id))) or 0,
            "plugins": db.scalar(select(func.count(Plugin.id))) or 0,
            "messages": db.scalar(select(func.count(Message.id))) or 0,
            "errors": db.scalar(select(func.count(ErrorLog.id))) or 0,
            "conversations": db.scalar(select(func.count(Conversation.id))) or 0,
        }
        recent_errors = db.scalars(select(ErrorLog).order_by(ErrorLog.id.desc()).limit(5)).all()
        ops_snapshot = {
            "enabled_bots": db.scalar(select(func.count(Bot.id)).where(Bot.is_enabled.is_(True))) or 0,
            "enabled_channels": db.scalar(select(func.count(Channel.id)).where(Channel.is_enabled.is_(True))) or 0,
            "online_workwechat": db.scalar(
                select(func.count(Channel.id)).where(
                    Channel.channel_type == "workwechat",
                    Channel.login_state == "online",
                )
            )
            or 0,
            "abnormal_channels": db.scalar(
                select(func.count(Channel.id)).where(
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
        recent_failed_jobs = db.scalars(
            select(TaskJob).where(TaskJob.status.in_(("failed", "running", "pending"))).order_by(TaskJob.id.desc()).limit(6)
        ).all()
        total_open_alerts = db.scalar(select(func.count(AlertRecord.id)).where(AlertRecord.status == "open")) or 0
        active_bot_stmt = (
            select(func.count(func.distinct(Conversation.bot_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Bot, Conversation.bot_id == Bot.id)
            .where(Message.created_at >= recent_cutoff)
        )
        active_user_stmt = (
            select(func.count(func.distinct(Message.sender_id)))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, Conversation.bot_id == Bot.id)
            .where(
                Message.direction == "in",
                Message.sender_id.is_not(None),
                Message.created_at >= recent_cutoff,
            )
        )
        active_group_stmt = (
            select(func.count(func.distinct(Conversation.external_id)))
            .join(Message, Message.conversation_id == Conversation.id)
            .join(Bot, Conversation.bot_id == Bot.id)
            .where(
                Conversation.chat_type == "group",
                Message.direction == "in",
                Message.created_at >= recent_cutoff,
            )
        )
        inbound_today_stmt = (
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, Conversation.bot_id == Bot.id)
            .where(Message.direction == "in", Message.created_at >= today_cutoff)
        )
        outbound_24h_stmt = (
            select(func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(Bot, Conversation.bot_id == Bot.id)
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
        alert_summary = build_dashboard_alert_summary(db)
        risk_items: list[str] = []
        if ops_snapshot["abnormal_channels"]:
            risk_items.append(f"当前有 {ops_snapshot['abnormal_channels']} 个启用渠道状态异常，建议优先看渠道页和错误页。")
        if ops_snapshot["failed_jobs"]:
            risk_items.append(f"任务队列里有 {ops_snapshot['failed_jobs']} 个失败任务，可能影响插件或出站链路。")
        if not ops_snapshot["online_workwechat"]:
            risk_items.append("当前没有在线的企业微信账号，相关入站和心跳能力可能不可用。")
        if alert_summary["total"]:
            risk_items.append(f"当前有 {alert_summary['total']} 条未恢复告警，请优先关注首页告警摘要和监控页。")
        if not risk_items:
            risk_items.append("当前没有明显阻断项，建议继续关注最近错误和待处理任务。")
        bot_rows = []
        for bot in db.scalars(select(Bot).order_by(Bot.id.desc())).all():
            bot_rows.append(
                {
                    "bot": bot,
                    "channels": db.scalar(select(func.count(Channel.id)).where(Channel.bot_id == bot.id)) or 0,
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
            alert_summary=alert_summary,
            recent_errors=recent_errors,
            ops_snapshot=ops_snapshot,
            recent_failed_jobs=recent_failed_jobs,
            risk_items=risk_items,
            bot_rows=bot_rows,
        )
    finally:
        db.close()


@app.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request):
    db = get_session()
    try:
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
        # 列表需要稳定的计数键，模板不再触碰已删除的分组、模板、标签和环境关系。
        bot_rows = [
            {
                "bot": bot,
                "channel_count": db.scalar(select(func.count(Channel.id)).where(Channel.bot_id == bot.id)) or 0,
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
        channels = db.scalars(select(Channel).where(Channel.bot_id == bot_id).order_by(Channel.id.desc())).all()
        bindings = db.scalars(select(BotPlugin).where(BotPlugin.bot_id == bot_id).order_by(BotPlugin.id.desc())).all()
        conversation_count = db.scalar(select(func.count(Conversation.id)).where(Conversation.bot_id == bot_id)) or 0
        return render(
            request,
            "bot_detail.html",
            page_title=f"机器人详情 · {bot.name}",
            bot=bot,
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


@app.post("/bots")
async def create_bot_action(
    request: Request,
    name: str = Form(...),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        cleaned_name = name.strip()
        if not cleaned_name:
            return RedirectResponse(
                url="/bots?notice=" + quote("机器人名称不能为空") + "&tone=danger",
                status_code=303,
            )
        if db.scalar(select(Bot.id).where(Bot.name == cleaned_name)) is not None:
            return RedirectResponse(
                url="/bots?notice=" + quote("机器人名称已存在，请换一个名称") + "&tone=danger",
                status_code=303,
            )
        try:
            bot = create_bot(db, BotCreate(name=cleaned_name))
        except IntegrityError:
            # 数据库唯一约束负责并发兜底；回滚后只向用户返回业务提示，不暴露 SQL 细节。
            db.rollback()
            return RedirectResponse(
                url="/bots?notice=" + quote("机器人名称已存在，请换一个名称") + "&tone=danger",
                status_code=303,
            )
        record_audit(db, "bot.create", "bot", bot.id, f"创建机器人 {bot.name}")
        notice = quote("机器人已创建，请继续添加企业微信账号")
        return RedirectResponse(url=f"/bots/{bot.id}?notice={notice}", status_code=303)
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

        bots = db.scalars(select(Bot).where(Bot.id.in_(bot_ids))).all()
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
        channels = db.scalars(select(Channel).order_by(Channel.id.desc())).all()
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        return render(
            request,
            "channels.html",
            page_title="渠道管理",
            channels=channels,
            workwechat_cloud_poll_config_map=build_workwechat_cloud_poll_config_map(channels),
            bots=bots,
            bot_name_map=bot_name_map,
            builtin_channel_types=BUILTIN_CHANNEL_TYPES,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_detail_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        wechat_ipad_proxy_form = extract_wechat_ipad_proxy_form(channel.adapter_config_json)
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
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
        workwechat_contact_summary = build_workwechat_contact_summary(db, channel) if channel.channel_type == "workwechat" else None
        # 企微云端拉取配置只在渠道详情页展示摘要；授权码明文不落库，生成后只通过一次性提示展示。
        wechat_ipad_contact_list = build_wechat_ipad_contact_list(channel) if channel.channel_type == "wechat_ipad" else None
        return render(
            request,
            "channel_detail.html",
            page_title=f"渠道详情 · {channel.name}",
            channel=channel,
            bots=bots,
            bot_name_map=bot_name_map,
            builtin_channel_types=BUILTIN_CHANNEL_TYPES,
            recent_channel_messages=recent_channel_messages,
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
            wechat_ipad_proxy_form=wechat_ipad_proxy_form,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = get_platform_session()
    try:
        pending_setup = read_setup_pending_restart_marker()
        if pending_setup:
            return RedirectResponse(
                url="/setup/completed?notice=" + quote("安装配置已经写入，请先重启服务，再重新进入后台"),
                status_code=303,
            )
        if not is_setup_required(db):
            clear_setup_pending_restart_marker()
            return RedirectResponse(url="/login", status_code=303)
        return render(
            request,
            "setup.html",
            page_title="首次安装",
            notice=request.query_params.get("notice", ""),
            generated_secret_key=generate_secret_key(),
            form_data={
                "database_mode": "sqlite",
                "db_host": "127.0.0.1",
                "db_port": "3306",
                "db_name": "multi_bot_saas",
                "db_user": "multi_bot_saas",
                "db_password": "",
                "admin_username": "admin",
                "admin_display_name": "管理员",
                "secret_key": settings.secret_key if settings.secret_key != "change-me" else "",
            },
        )
    finally:
        db.close()


@app.post("/setup")
async def setup_action(
    request: Request,
    database_mode: str = Form("sqlite"),
    db_host: str = Form("127.0.0.1"),
    db_port: str = Form("3306"),
    db_name: str = Form("multi_bot_saas"),
    db_user: str = Form("multi_bot_saas"),
    db_password: str = Form(""),
    admin_username: str = Form(...),
    admin_display_name: str = Form("管理员"),
    admin_password: str = Form(...),
    admin_password_confirm: str = Form(...),
    secret_key: str = Form(...),
):
    pending_setup = read_setup_pending_restart_marker()
    if pending_setup:
        return RedirectResponse(
            url="/setup/completed?notice=" + quote("安装配置已经写入，请先重启服务，当前不会再次覆盖配置"),
            status_code=303,
        )
    form_data = {
        "database_mode": str(database_mode or "sqlite").strip().lower() or "sqlite",
        "db_host": str(db_host or "").strip(),
        "db_port": str(db_port or "").strip(),
        "db_name": str(db_name or "").strip(),
        "db_user": str(db_user or "").strip(),
        "db_password": str(db_password or "").strip(),
        "admin_username": str(admin_username or "").strip(),
        "admin_display_name": str(admin_display_name or "").strip() or "管理员",
        "secret_key": str(secret_key or "").strip(),
    }
    db = get_platform_session()
    try:
        if not is_setup_required(db):
            return RedirectResponse(url="/login", status_code=303)
    finally:
        db.close()

    if form_data["database_mode"] not in {"sqlite", "mariadb"}:
        form_data["database_mode"] = "sqlite"
    if not form_data["admin_username"]:
        return render(request, "setup.html", page_title="首次安装", notice="管理员用户名不能为空", generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)
    if len(form_data["admin_username"]) > 50:
        return render(request, "setup.html", page_title="首次安装", notice="管理员用户名长度不能超过 50", generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)
    if admin_password != admin_password_confirm:
        return render(request, "setup.html", page_title="首次安装", notice="两次输入的管理员密码不一致", generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)
    password_error = validate_password_change(admin_password)
    if password_error:
        return render(request, "setup.html", page_title="首次安装", notice=password_error, generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)
    if len(form_data["secret_key"]) < 16:
        return render(request, "setup.html", page_title="首次安装", notice="SECRET_KEY 至少需要 16 位", generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)

    if form_data["database_mode"] == "sqlite":
        database_url = build_default_sqlite_url()
    else:
        try:
            db_port_value = int(form_data["db_port"] or 3306)
        except ValueError:
            return render(request, "setup.html", page_title="首次安装", notice="MariaDB 端口格式不正确", generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)
        required_fields = ("db_host", "db_name", "db_user", "db_password")
        if any(not form_data[key] for key in required_fields):
            return render(request, "setup.html", page_title="首次安装", notice="MariaDB 连接信息必须填写完整", generated_secret_key=generate_secret_key(), form_data=form_data, response_status=400)
        database_url = build_mariadb_url(
            form_data["db_host"],
            db_port_value,
            form_data["db_name"],
            form_data["db_user"],
            form_data["db_password"],
        )
        connection_error = validate_database_connection(database_url)
        if connection_error:
            return render(
                request,
                "setup.html",
                page_title="首次安装",
                notice=format_setup_database_error(connection_error),
                generated_secret_key=generate_secret_key(),
                form_data=form_data,
                response_status=400,
            )

    provision_error = provision_database(
        database_url,
        admin_username=form_data["admin_username"],
        admin_password=admin_password,
        admin_display_name=form_data["admin_display_name"],
        secret_key=form_data["secret_key"],
    )
    if provision_error:
        return render(
            request,
            "setup.html",
            page_title="首次安装",
            notice=f"初始化失败：{provision_error}",
            generated_secret_key=generate_secret_key(),
            form_data=form_data,
            response_status=500,
        )

    write_env_file(
        {
            "APP_ENV": settings.app_env,
            "SECRET_KEY": form_data["secret_key"],
            "AUTH_SEED_DEMO_USERS": "false",
            "AUTH_BOOTSTRAP_ADMIN_USERNAME": form_data["admin_username"],
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
            "AUTH_BOOTSTRAP_ADMIN_DISPLAY_NAME": form_data["admin_display_name"],
            "DATABASE_URL": database_url,
        }
    )
    write_setup_pending_restart_marker(
        database_mode=form_data["database_mode"],
        database_url=database_url,
        admin_username=form_data["admin_username"],
    )

    return RedirectResponse(
        url="/setup/completed?notice=" + quote("安装已完成，请重启服务后重新进入后台"),
        status_code=303,
    )


@app.get("/channels/{channel_id}/chatrooms/{chatroom_id}", response_class=HTMLResponse)
async def channel_chatroom_detail_page(request: Request, channel_id: int, chatroom_id: int):
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
        else:
            raise HTTPException(status_code=400, detail="Chatroom detail only supported for wechat_ipad / wechat_winauto / workwechat channels")
        return render(
            request,
            "chatroom_detail.html",
            page_title=f"群详情 · {detail['name']}",
            channel=channel,
            chatroom=chatroom,
            detail=detail,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.get("/channels/{channel_id}/chatrooms", response_class=HTMLResponse)
async def channel_chatroom_list_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type == "wechat_ipad":
            list_view = build_wechat_ipad_chatroom_list(db, channel)
        elif channel.channel_type == "wechat_winauto":
            list_view = build_wechat_winauto_chatroom_list(db, channel)
        elif channel.channel_type == "workwechat":
            list_view = build_workwechat_chatroom_list(db, channel)
        else:
            raise HTTPException(status_code=400, detail="Chatroom list only supported for wechat_ipad / wechat_winauto / workwechat channels")
        return render(
            request,
            "chatroom_list.html",
            page_title=f"群列表 · {channel.name}",
            channel=channel,
            list_view=list_view,
            notice=request.query_params.get("notice", ""),
        )
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
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        login_profile = await resolve_wechat_ipad_login_profile(request)
        start_wechat_ipad_login(db, channel, login_profile=login_profile)
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-flow/refresh")
async def refresh_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        login_profile = await resolve_wechat_ipad_login_profile(request)
        refresh_wechat_ipad_qrcode(db, channel, login_profile=login_profile)
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-flow/logout")
async def logout_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        login_profile = await resolve_wechat_ipad_login_profile(request)
        logout_wechat_ipad(db, channel, login_profile=login_profile)
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-flow/poll")
async def poll_wechat_ipad_login_flow(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        poll_wechat_ipad_login_status(db, channel)
        db.refresh(channel)
        return JSONResponse(build_wechat_ipad_flow_payload(db, channel))
    finally:
        db.close()


@app.post("/channels")
async def create_channel_action(
    request: Request,
    bot_id: int = Form(...),
    name: str = Form(...),
    channel_type: str = Form(...),
    api_base: str = Form(""),
    bot_token: str = Form(""),
    webhook_path: str = Form(""),
    dm_policy: str = Form("allowlist"),
    allow_from: str = Form(""),
    adapter_config_json: str = Form(""),
    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
    is_enabled: str = Form("true"),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        merged_adapter_config_json, adapter_error = merge_channel_adapter_config(
            channel_type=channel_type,
            adapter_config_json=adapter_config_json,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
        if adapter_error:
            return RedirectResponse(url="/channels?notice=" + quote(adapter_error), status_code=303)
        payload = ChannelCreate(
            bot_id=bot_id,
            name=name,
            channel_type=channel_type,
            api_base=api_base,
            bot_token=bot_token,
            webhook_path=webhook_path,
            dm_policy=dm_policy,
            allow_from=allow_from,
            adapter_config_json=merged_adapter_config_json,
            is_enabled=is_enabled == "true",
        )
        channel = create_channel(db, payload)
        record_audit(db, "channel.create", "channel", channel.id, f"创建渠道 {channel.name}")
        return RedirectResponse(url="/channels", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/edit")
async def edit_channel_action(
    request: Request,
    channel_id: int,
    bot_id: int = Form(...),
    name: str = Form(...),
    channel_type: str = Form(...),
    api_base: str = Form(""),
    bot_token: str = Form(""),
    webhook_path: str = Form(""),
    dm_policy: str = Form("allowlist"),
    allow_from: str = Form(""),
    adapter_config_json: str = Form(""),
    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
    is_enabled: str = Form("true"),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        merged_adapter_config_json, adapter_error = merge_channel_adapter_config(
            channel_type=channel_type,
            adapter_config_json=adapter_config_json,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
        if adapter_error:
            return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(adapter_error)}", status_code=303)
        payload = ChannelUpdate(
            bot_id=bot_id,
            name=name,
            channel_type=channel_type,
            api_base=api_base,
            bot_token=bot_token,
            webhook_path=webhook_path,
            dm_policy=dm_policy,
            allow_from=allow_from,
            adapter_config_json=merged_adapter_config_json,
            is_enabled=is_enabled == "true",
        )
        update_channel(db, channel, payload)
        record_audit(db, "channel.update", "channel", channel.id, f"更新渠道 {channel.name}")
        return RedirectResponse(url=f"/channels/{channel_id}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/toggle")
async def toggle_channel_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        toggle_channel(db, channel)
        record_audit(db, "channel.toggle", "channel", channel.id, f"enabled={channel.is_enabled}")
        return RedirectResponse(url=f"/channels/{channel_id}", status_code=303)
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
                "adapter_provider": adapter_result.get("provider"),
                "adapter_details": adapter_result.get("details"),
            },
        )
        if status != "online":
            create_alert(db, "channel-health", f"渠道 {channel.name} 健康异常", message, related_type="channel", related_id=channel.id)
        record_audit(db, "channel.health-check", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/login-init")
async def start_wechat_ipad_login_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        login_profile = await resolve_wechat_ipad_login_profile(request)
        overview = start_wechat_ipad_login(db, channel, login_profile=login_profile)
        message = str(overview.get("last_status_summary") or "已触发微信 iPad 登录初始化")
        record_audit(db, "channel.wechat-ipad.login-init", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/status-sync")
async def refresh_wechat_ipad_status_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = refresh_wechat_ipad_status(db, channel)
        message = str(overview.get("last_status_summary") or "已同步微信 iPad 状态")
        record_audit(db, "channel.wechat-ipad.status-sync", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/qrcode-refresh")
async def refresh_wechat_ipad_qrcode_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        login_profile = await resolve_wechat_ipad_login_profile(request)
        overview = refresh_wechat_ipad_qrcode(db, channel, login_profile=login_profile)
        message = str(overview.get("last_status_summary") or "已刷新微信 iPad 二维码")
        record_audit(db, "channel.wechat-ipad.qrcode-refresh", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/logout")
async def logout_wechat_ipad_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        login_profile = await resolve_wechat_ipad_login_profile(request)
        overview = logout_wechat_ipad(db, channel, login_profile=login_profile)
        message = str(overview.get("last_status_summary") or "已退出微信登录")
        record_audit(db, "channel.wechat-ipad.logout", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/wechat-ipad/reconnect")
async def reconnect_wechat_ipad_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = reconnect_wechat_ipad(db, channel)
        message = str(overview.get("last_status_summary") or "已触发微信 iPad 重连")
        record_audit(db, "channel.wechat-ipad.reconnect", "channel", channel.id, message)
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
        else:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/create")
async def create_channel_chatroom_action(
    request: Request,
    channel_id: int,
    external_id: str = Form(...),
    name: str = Form(""),
    remark: str = Form(""),
    operator_note: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
            raise HTTPException(status_code=404, detail="Channel not found")
        normalized_external_id = str(external_id or "").strip()
        if not normalized_external_id:
            return RedirectResponse(url=f"/channels/{channel_id}/chatrooms?notice={quote('群外部 ID 不能为空')}", status_code=303)
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
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
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
            "enabled": "已启用该群，后续可参与插件调度",
            "disabled": "已停用该群，插件调度会跳过",
            "archived": "已归档该群",
        }.get(chatroom.management_status, "已将该群标记为待启用")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
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
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms?notice={quote(message)}", status_code=303)
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
            return RedirectResponse(url=f"/channels/{channel_id}/chatrooms?notice={quote(message)}", status_code=303)
        # workwechat 群列表同步走 provider 的 11038，失败时服务层会保留本地消息样本视图。
        overview = sync_workwechat_chatrooms(db, channel)
        db.commit()
        message = str(overview.get("last_status_summary") or "已同步企微群列表")
        record_audit(db, "channel.workwechat.sync-chatrooms", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/media-download")
async def update_wechat_ipad_chatroom_media_download_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    enabled: str = Form("false"),
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
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        extra = parse_config_json(chatroom.extra_json)
        is_enabled = str(enabled or "").strip().lower() == "true"
        extra["auto_download_media"] = is_enabled
        chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
        db.commit()
        message = "已开启当前群的附件自动下载" if is_enabled else "已关闭当前群的附件自动下载"
        record_audit(db, "channel.wechat-ipad.chatroom-media-download.update", "chatroom", chatroom.id, f"{message} by {user.username}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
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
        if channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat")
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
                operator_id=user.id,
                audit_note=f"manual enable by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.id,
                audit_note=f"manual enable by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.id,
                audit_note=f"manual enable by {user.username}",
            )
        db.commit()
        message = "已将该群的插件草稿作用域设为启用"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.enable", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
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
        if channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat")
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
                operator_id=user.id,
                audit_note=f"manual disable by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.id,
                audit_note=f"manual disable by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.id,
                audit_note=f"manual disable by {user.username}",
            )
        db.commit()
        message = "已将该群的插件草稿作用域设为停用"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.disable", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
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
        if channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            reset_wechat_ipad_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.id,
                audit_note=f"reset to inherited by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            reset_wechat_winauto_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.id,
                audit_note=f"reset to inherited by {user.username}",
            )
        else:
            reset_workwechat_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.id,
                audit_note=f"reset to inherited by {user.username}",
            )
        db.commit()
        message = "已恢复继承 bot_plugin 绑定状态"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.reset", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
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
        if channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        normalized_override = config_override_json.strip()
        normalized_to_save: str | None = None
        if normalized_override:
            parsed_override, config_error = parse_json_object(normalized_override)
            if config_error:
                return RedirectResponse(
                    url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote('群级配置 JSON 无效：' + config_error)}",
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
                operator_id=user.id,
                audit_note=f"group override updated by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_config_override(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                config_override_json=normalized_to_save,
                operator_id=user.id,
                audit_note=f"group override updated by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_config_override(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                config_override_json=normalized_to_save,
                operator_id=user.id,
                audit_note=f"group override updated by {user.username}",
            )
        db.commit()
        message = "已保存群级插件配置覆盖" if normalized_to_save else "已清空群级插件配置覆盖，恢复继承默认配置"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.config", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@app.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/mode")
async def set_wechat_ipad_chatroom_scope_mode_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    bot_plugin_id: int,
    mode: str = Form(...),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = db.get(Channel, channel_id)
        if not channel or channel.channel_type not in {"wechat_ipad", "wechat_winauto", "workwechat"}:
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
                    operator_id=user.id,
                    audit_note="群级开关切换为启用",
                )
            elif channel.channel_type == "wechat_winauto":
                update_wechat_winauto_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.id,
                    audit_note="群级开关切换为启用",
                )
            else:
                update_workwechat_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.id,
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
                    operator_id=user.id,
                    audit_note="群级开关切换为停用",
                )
            elif channel.channel_type == "wechat_winauto":
                update_wechat_winauto_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.id,
                    audit_note="群级开关切换为停用",
                )
            else:
                update_workwechat_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.id,
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
                    operator_id=user.id,
                    audit_note="群级开关恢复继承",
                )
            elif channel.channel_type == "wechat_winauto":
                reset_wechat_winauto_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.id,
                    audit_note="群级开关恢复继承",
                )
            else:
                reset_workwechat_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.id,
                    audit_note="群级开关恢复继承",
                )
            message = "群插件已恢复继承 bot 级状态"
        else:
            raise HTTPException(status_code=400, detail="Invalid scope mode")
        # 群权限模式是运维人员在页面上直接操作的状态，先显式提交业务变更，再记录审计，避免依赖审计函数的隐式提交。
        db.commit()
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.mode", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}, mode={normalized_mode}")
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote(message)}", status_code=303)
    except ValueError as exc:
        db.rollback()
        # 服务层会在绑定和群不匹配等数据异常时抛 ValueError；这里转成页面提示，避免按钮操作直接落到 500 错误页。
        return RedirectResponse(url=f"/channels/{channel_id}/chatrooms/{chatroom_id}?notice={quote('群权限保存失败：' + str(exc))}", status_code=303)
    finally:
        db.close()


@app.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugins = db.scalars(select(Plugin).order_by(Plugin.id.desc())).all()
        bindings = db.scalars(select(BotPlugin).order_by(BotPlugin.id.desc())).all()
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
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
        plugins = db.scalars(select(Plugin).order_by(Plugin.id.desc())).all()
        bindings = db.scalars(select(BotPlugin).order_by(BotPlugin.id.desc())).all()
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
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
        plugin = db.get(Plugin, plugin_id)
        if not plugin:
            raise HTTPException(status_code=404, detail="Plugin not found")
        bindings = db.scalars(select(BotPlugin).where(BotPlugin.plugin_id == plugin_id).order_by(BotPlugin.id.desc())).all()
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        for binding in bindings:
            binding.runtime_status = compute_binding_runtime(binding)
        return render(
            request,
            "plugin_detail.html",
            page_title=f"插件详情 · {plugin.display_name}",
            plugin=plugin,
            bindings=bindings,
            binding_cards=build_binding_schema_cards_with_dispatch(db, bindings),
            plugin_config_view=build_plugin_config_view(plugin.code, None),
            plugin_runtime_info=get_plugin_runtime_meta(plugin.code),
            bots=bots,
            bot_name_map=bot_name_map,
            runtime_summary=build_plugin_runtime_summary([plugin], bindings),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.post("/plugins/bind")
async def bind_plugin_action(
    request: Request,
    bot_id: int = Form(...),
    plugin_id: int = Form(...),
    status: str = Form("enabled"),
    config_json: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        plugin = db.get(Plugin, plugin_id)
        if not plugin:
            raise HTTPException(status_code=404, detail="Plugin not found")
        form = await request.form()
        effective_config_json = merge_schema_config(
            plugin.code,
            form,
            fallback_config_json=config_json or None,
        )
        payload = BotPluginBindCreate(bot_id=bot_id, plugin_id=plugin_id, status=status, config_json=effective_config_json)
        binding = bind_plugin(db, payload)
        record_audit(db, "plugin.bind", "bot_plugin", binding.id, f"bot={binding.bot_id}, plugin={binding.plugin_id}")
        return RedirectResponse(url=f"/plugins/{plugin_id}?notice=" + quote("插件绑定已保存"), status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/toggle")
async def toggle_plugin_binding_action(request: Request, binding_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        toggle_binding(db, binding)
        record_audit(db, "plugin.toggle", "bot_plugin", binding.id, f"status={binding.status}")
        return RedirectResponse(url="/plugins/runtime?notice=插件绑定状态已切换", status_code=303)
    finally:
        db.close()


@app.post("/plugins/toggle-direct")
async def toggle_plugin_direct_action(
    request: Request,
    bot_id: int = Form(...),
    plugin_id: int = Form(...),
    redirect_to: str = Form("/plugins"),
    open_bot_id: int = Form(0),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        wants_json = "application/json" in str(request.headers.get("accept", "")).lower() or str(
            request.headers.get("x-requested-with", "")
        ).lower() == "xmlhttprequest"
        plugin = db.get(Plugin, plugin_id)
        bot = db.get(Bot, bot_id)
        if not plugin or not bot:
            raise HTTPException(status_code=404, detail="Bot or plugin not found")
        binding = db.scalar(select(BotPlugin).where(BotPlugin.bot_id == bot_id, BotPlugin.plugin_id == plugin_id))
        if binding:
            toggle_binding(db, binding)
            message = f"{bot.name} 的 {plugin.display_name} 已{'启用' if binding.status == 'enabled' else '停用'}"
            record_audit(db, "plugin.toggle-direct", "bot_plugin", binding.id, f"status={binding.status}")
        else:
            payload = BotPluginBindCreate(bot_id=bot_id, plugin_id=plugin_id, status="enabled", config_json=None)
            binding = bind_plugin(db, payload)
            message = f"{bot.name} 的 {plugin.display_name} 已启用"
            record_audit(db, "plugin.toggle-direct", "bot_plugin", binding.id, "created=enabled")
        enabled_count = db.scalar(
            select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot_id, BotPlugin.status == "enabled")
        ) or 0
        total_count = db.scalar(select(func.count(Plugin.id))) or 0
        is_enabled = str(binding.status or "").strip() == "enabled"
        if wants_json:
            return JSONResponse(
                {
                    "ok": True,
                    "notice": message,
                    "bot_id": bot_id,
                    "plugin_id": plugin_id,
                    "binding_id": binding.id,
                    "is_bound": True,
                    "is_enabled": is_enabled,
                    "status_label": "已启用" if is_enabled else "已停用",
                    "status_tone": "success" if is_enabled else "muted",
                    "button_label": "关闭" if is_enabled else "开启",
                    "enabled_count": int(enabled_count),
                    "total_count": int(total_count),
                }
            )
        target = redirect_to if redirect_to.startswith("/") else "/plugins"
        if open_bot_id <= 0:
            open_bot_id = bot_id
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{separator}open_bot_id={open_bot_id}&notice={quote(message)}",
            status_code=303,
        )
    finally:
        db.close()


@app.post("/plugins/{binding_id}/edit")
async def edit_plugin_binding_action(
    request: Request,
    binding_id: int,
    status: str = Form("enabled"),
    config_json: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        form = await request.form()
        plugin_code = binding.plugin.code if binding.plugin else None
        effective_config_json = merge_schema_config(
            plugin_code,
            form,
            existing_config_json=binding.config_json,
            fallback_config_json=config_json or None,
        )
        update_binding_config(db, binding, status=status, config_json=effective_config_json)
        record_audit(db, "plugin.update", "bot_plugin", binding.id, f"status={status}")
        return RedirectResponse(url="/plugins/runtime?notice=插件绑定配置已保存", status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/reload")
async def reload_plugin_binding_action(request: Request, binding_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        _, message = reload_binding(db, binding)
        record_audit(db, "plugin.reload", "bot_plugin", binding.id, message)
        return RedirectResponse(url="/plugins/runtime?notice=" + quote(message), status_code=303)
    finally:
        db.close()


@app.post("/plugins/{binding_id}/execute")
async def execute_plugin_binding_action(request: Request, binding_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        result = execute_binding_action(binding, "execute", payload=build_execution_payload(db, binding, payload))
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


@app.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request, bot_id: int = 0, channel_id: int = 0, chat_type: str = ""):
    db = get_session()
    try:
        normalized_chat_type = chat_type.strip().lower()
        stmt = select(Conversation)
        if bot_id:
            stmt = stmt.where(Conversation.bot_id == bot_id)
        if channel_id:
            stmt = stmt.where(Conversation.channel_id == channel_id)
        if normalized_chat_type in {"group", "direct"}:
            stmt = stmt.where(Conversation.chat_type == normalized_chat_type)
        else:
            normalized_chat_type = ""
        conversations = db.scalars(stmt.order_by(Conversation.id.desc()).limit(50)).all()
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        channels = db.scalars(select(Channel).order_by(Channel.id.desc())).all()
        return render(
            request,
            "conversations.html",
            page_title="会话中心",
            conversations=conversations,
            bots=bots,
            bot_name_map=bot_name_map,
            channels=channels,
            bot_id=bot_id,
            channel_id=channel_id,
            chat_type=normalized_chat_type,
        )
    finally:
        db.close()


@app.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail_page(request: Request, conversation_id: int, page: int = 1):
    db = get_session()
    try:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_bot = db.get(Bot, conversation.bot_id) if conversation.bot_id else None
        current_page = max(int(page or 1), 1)
        page_size = 20
        total = int(
            db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conversation_id)) or 0
        )
        total_pages = max((total + page_size - 1) // page_size, 1)
        current_page = min(current_page, total_pages)
        offset = (current_page - 1) * page_size
        messages = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .offset(offset)
            .limit(page_size)
        ).all()
        message_ids = [int(item.id) for item in messages]
        attachment_rows = db.scalars(
            select(MessageAttachment)
            .where(MessageAttachment.message_id.in_(message_ids) if message_ids else MessageAttachment.id == 0)
            .order_by(MessageAttachment.id.asc())
        ).all()
        attachments_by_message: dict[int, list[MessageAttachment]] = {}
        for row in attachment_rows:
            if row.message_id is None:
                continue
            attachments_by_message.setdefault(int(row.message_id), []).append(row)
        pagination = {
            "page": current_page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
            "prev_page": current_page - 1 if current_page > 1 else 1,
            "next_page": current_page + 1 if current_page < total_pages else total_pages,
            "from_index": offset + 1 if total else 0,
            "to_index": min(offset + len(messages), total),
        }
        return render(
            request,
            "conversation_detail.html",
            page_title=f"会话详情 · {conversation.title or conversation.external_id}",
            conversation=conversation,
            conversation_bot_name=(str(conversation_bot.name).strip() if conversation_bot and conversation_bot.name else ""),
            messages=messages,
            attachments_by_message=attachments_by_message,
            pagination=pagination,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@app.post("/conversations/{conversation_id}/messages")
async def send_manual_message_action(request: Request, conversation_id: int, content: str = Form(...)):
    enforce_role(request, "ops")
    db = get_session()
    try:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        message = append_manual_message(db, conversation, content)
        record_audit(db, "message.manual-send", "message", message.id, f"conversation={conversation.id}")
        return RedirectResponse(url=f"/conversations/{conversation_id}?notice=测试消息已入队", status_code=303)
    finally:
        db.close()


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    keyword: str = "",
    direction: str = "",
    bot_id: int = 0,
    channel_id: int = 0,
    chat_type: str = "",
    page: int = 1,
):
    db = get_session()
    try:
        normalized_keyword = keyword.strip()
        normalized_direction = direction.strip().lower()
        normalized_chat_type = chat_type.strip().lower()
        current_page = max(int(page or 1), 1)
        page_size = 20

        stmt = select(Message).join(Conversation, Message.conversation_id == Conversation.id)

        if normalized_keyword:
            like_keyword = f"%{normalized_keyword}%"
            stmt = stmt.where(
                or_(
                    Message.content.ilike(like_keyword),
                    Message.sender_name.ilike(like_keyword),
                    Message.sender_id.ilike(like_keyword),
                    Message.external_message_id.ilike(like_keyword),
                )
            )

        if normalized_direction in {"in", "out"}:
            stmt = stmt.where(Message.direction == normalized_direction)
        else:
            normalized_direction = ""

        if bot_id:
            stmt = stmt.where(Conversation.bot_id == bot_id)

        if channel_id:
            stmt = stmt.where(Conversation.channel_id == channel_id)

        if normalized_chat_type in {"group", "direct"}:
            stmt = stmt.where(Conversation.chat_type == normalized_chat_type)
        else:
            normalized_chat_type = ""

        total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        total_pages = max((total + page_size - 1) // page_size, 1)
        current_page = min(current_page, total_pages)
        offset = (current_page - 1) * page_size
        messages = db.scalars(stmt.order_by(Message.id.desc()).offset(offset).limit(page_size)).all()
        pagination_query = {
            "keyword": normalized_keyword,
            "direction": normalized_direction,
            "bot_id": bot_id if bot_id else 0,
            "channel_id": channel_id if channel_id else 0,
            "chat_type": normalized_chat_type,
        }
        pagination = {
            "page": current_page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
            "prev_page": current_page - 1 if current_page > 1 else 1,
            "next_page": current_page + 1 if current_page < total_pages else total_pages,
            "from_index": offset + 1 if total else 0,
            "to_index": min(offset + len(messages), total),
            "query": urlencode({key: value for key, value in pagination_query.items() if value not in ("", 0, None)}),
        }
        bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
        channels = db.scalars(select(Channel).order_by(Channel.id.desc())).all()
        return render(
            request,
            "messages.html",
            page_title="消息中心",
            messages=messages,
            pagination=pagination,
            bots=bots,
            channels=channels,
            keyword=normalized_keyword,
            direction=normalized_direction,
            bot_id=bot_id,
            channel_id=channel_id,
            chat_type=normalized_chat_type,
        )
    finally:
        db.close()


@app.get("/lab", response_class=HTMLResponse)
async def message_lab_page(request: Request):
    enforce_role(request, "ops")
    db = get_session()
    try:
        active_conversation = None
        active_messages: list[Message] = []
        return render(
            request,
            "lab.html",
            **build_lab_page_context(
                db,
                notice=request.query_params.get("notice", ""),
                active_conversation=active_conversation,
                active_messages=active_messages,
            ),
        )
    finally:
        db.close()


@app.post("/lab/inbound", response_class=HTMLResponse)
async def message_lab_inbound_action(
    request: Request,
    channel_id: int = Form(...),
    conversation_external_id: str = Form(...),
    conversation_title: str = Form(""),
    chat_type: str = Form("group"),
    sender_id: str = Form(""),
    sender_name: str = Form(""),
    message_type: str = Form("text"),
    content: str = Form(""),
    event_type: str = Form("lab.message"),
    event_key: str = Form(""),
    mentioned_self: bool = Form(False),
    dry_run_outbound: bool = Form(False),
    recent_messages_text: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    inbound_form = {
        "channel_id": channel_id,
        "conversation_external_id": conversation_external_id,
        "conversation_title": conversation_title,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_type": message_type,
        "content": content,
        "event_type": event_type,
        "event_key": event_key,
        "mentioned_self": mentioned_self,
        "dry_run_outbound": dry_run_outbound,
        "recent_messages_text": recent_messages_text,
    }
    try:
        channel = db.get(Channel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        recent_messages = parse_message_lines(recent_messages_text)
        normalized_event = {
            "provider": "lab_ui",
            "event_key": event_key.strip() or None,
            "event_type": event_type.strip() or "lab.message",
            "should_persist_message": bool(content.strip()),
            "conversation": {
                "external_id": conversation_external_id.strip() or f"lab-{channel.id}",
                "title": conversation_title.strip() or None,
                "chat_type": chat_type.strip().lower() or "group",
            },
            "message": {
                "content": content.strip(),
                "sender_id": sender_id.strip() or None,
                "sender_name": sender_name.strip() or None,
                "message_type": message_type.strip().lower() or "text",
                "external_message_id": event_key.strip() or None,
            },
            "details": {
                "headers_used": [],
                "mentioned_self": bool(mentioned_self),
                "dry_run_outbound": bool(dry_run_outbound),
                "source": "lab_ui",
            },
        }
        if recent_messages:
            normalized_event["details"]["recent_messages"] = recent_messages

        result = ingest_normalized_event(
            db,
            channel,
            normalized_event,
            source="lab_ui",
            raw_payload={"lab": normalized_event},
        )
        record_audit(
            db,
            "lab.inbound-simulated",
            "channel",
            channel.id,
            f"channel={channel.id}, raw_event={result['raw_event_id']}",
        )
        active_conversation, active_messages = load_lab_conversation(
            db,
            channel_id=channel.id,
            external_id=str(normalized_event["conversation"]["external_id"]),
        )
        inbound_result = {
            "ok": True,
            "raw_event_id": result["raw_event_id"],
            "mapped": result["mapped"],
            "normalized_json": json.dumps(result["normalized"], ensure_ascii=False, indent=2),
            "dispatch_json": json.dumps(result["dispatch"], ensure_ascii=False, indent=2),
        }
        return render(
            request,
            "lab.html",
            **build_lab_page_context(
                db,
                notice="进站实验已执行。",
                inbound_form=inbound_form,
                inbound_result=inbound_result,
                active_conversation=active_conversation,
                active_messages=active_messages,
            ),
        )
    finally:
        db.close()


@app.post("/lab/outbound", response_class=HTMLResponse)
async def message_lab_outbound_action(
    request: Request,
    channel_id: int = Form(...),
    conversation_external_id: str = Form(...),
    conversation_title: str = Form(""),
    chat_type: str = Form("group"),
    message_type: str = Form("text"),
    content: str = Form(""),
    extra_payload_json: str = Form(""),
    submit_mode: str = Form("preview"),
):
    enforce_role(request, "ops")
    db = get_session()
    outbound_form = {
        "channel_id": channel_id,
        "conversation_external_id": conversation_external_id,
        "conversation_title": conversation_title,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content,
        "extra_payload_json": extra_payload_json,
        "submit_mode": submit_mode,
    }
    try:
        channel = db.get(Channel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        extra_payload, json_error = parse_json_object(extra_payload_json)
        if json_error:
            return render(
                request,
                "lab.html",
                **build_lab_page_context(
                    db,
                    notice=json_error,
                    outbound_form=outbound_form,
                    outbound_result={"ok": False},
                ),
            )

        payload = {
            "external_id": conversation_external_id.strip() or f"lab-{channel.id}",
            "receiver_id": conversation_external_id.strip() or f"lab-{channel.id}",
            "title": conversation_title.strip() or None,
            "chat_type": chat_type.strip().lower() or "group",
            "message_type": message_type.strip().lower() or "text",
            "content": content,
            **extra_payload,
        }
        if submit_mode == "preview":
            adapter = get_channel_adapter(channel)
            active_conversation, active_messages = load_lab_conversation(
                db,
                channel_id=channel.id,
                external_id=str(payload.get("external_id") or ""),
            )
            outbound_result = {
                "ok": True,
                "mode": "preview",
                "adapter_provider": adapter.provider,
                "payload_json": json.dumps(payload, ensure_ascii=False, indent=2),
            }
            return render(
                request,
                "lab.html",
                **build_lab_page_context(
                    db,
                    notice="出站预览已生成，未真实发送。",
                    outbound_form=outbound_form,
                    outbound_result=outbound_result,
                    active_conversation=active_conversation,
                    active_messages=active_messages,
                ),
            )

        conversation = ensure_conversation(
            db,
            channel=channel,
            external_id=str(payload.get("external_id") or f"lab-{channel.id}"),
            title=str(payload.get("title") or conversation_title or conversation_external_id or ""),
            chat_type=str(payload.get("chat_type") or chat_type or "group"),
        )
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
        record_audit(
            db,
            "lab.outbound-queued",
            "task_job",
            job.id,
            f"channel={channel.id}, conversation={conversation.id}",
        )
        outbound_result = {
            "ok": True,
            "mode": "send",
            "job_id": job.id,
            "adapter_provider": "queued",
            "adapter_json": json.dumps(
                {"ok": True, "provider": "queued", "transport_status": "queued", "status": "pending"},
                ensure_ascii=False,
                indent=2,
            ),
            "payload_json": json.dumps(payload, ensure_ascii=False, indent=2),
        }
        active_conversation, active_messages = load_lab_conversation(
            db,
            channel_id=channel.id,
            external_id=str(payload.get("external_id") or ""),
        )
        return render(
            request,
            "lab.html",
            **build_lab_page_context(
                db,
                notice="出站实验已执行。",
                outbound_form=outbound_form,
                outbound_result=outbound_result,
                active_conversation=active_conversation,
                active_messages=active_messages,
            ),
        )
    finally:
        db.close()


@app.get("/errors", response_class=HTMLResponse)
async def errors_page(
    request: Request,
    tab: str = "error",
    source_file: str = "",
    keyword: str = "",
    page: int = 1,
):
    active_tab = "info" if str(tab or "").strip().lower() == "info" else "error"
    normalized_keyword = str(keyword or "").strip().lower()
    available_log_files = resolve_tab_log_paths(active_tab)
    selected_file_name = str(source_file or "").strip()
    selected_path = resolve_selected_log_path(active_tab, selected_file_name=selected_file_name)

    try:
        raw_lines = selected_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        raw_lines = []

    if normalized_keyword:
        filtered_lines = [line for line in raw_lines if normalized_keyword in line.lower()]
    else:
        filtered_lines = list(raw_lines)

    # 日志中心默认按“最新在前”输出：第 1 页就是最新批次，继续翻页才进入更早日志。
    page_size = 1200
    current_page = max(int(page or 1), 1)
    total = len(filtered_lines)
    total_pages = max((total + page_size - 1) // page_size, 1)
    current_page = min(current_page, total_pages)
    start_index = (current_page - 1) * page_size
    end_index = min(start_index + page_size, total)
    paged_lines = list(reversed(filtered_lines))[start_index:end_index]

    overview = {
        "total_lines": total,
        "displayed_lines": len(paged_lines),
        "active_file": selected_path.name,
        "active_file_path": str(selected_path),
        "order_label": "最新在前",
    }
    filter_query = urlencode(
        {
            key: value
            for key, value in {
                "tab": active_tab,
                "source_file": selected_path.name,
                "keyword": keyword.strip(),
            }.items()
            if value
        }
    )
    pagination = {
        "page": current_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
        "prev_page": current_page - 1 if current_page > 1 else 1,
        "next_page": current_page + 1 if current_page < total_pages else total_pages,
        "from_index": start_index + 1 if total else 0,
        "to_index": end_index,
        "query": filter_query,
    }
    return render(
        request,
        "errors.html",
        page_title="日志中心",
        log_lines=paged_lines,
        pagination=pagination,
        overview=overview,
        active_tab=active_tab,
        source_file=selected_path.name,
        keyword=keyword,
        available_log_files=[str(path.name) for path in available_log_files],
    )


def resolve_tab_log_paths(tab: str) -> list[Path]:
    active_tab = "info" if str(tab or "").strip().lower() == "info" else "error"
    log_dir = Path(settings.log_dir)
    base_name = "app.log" if active_tab == "info" else "error.log"
    base_path = log_dir / base_name
    rotated_paths = sorted(log_dir.glob(f"{base_name}.*"), key=lambda path: path.name)
    return [path for path in [base_path, *rotated_paths] if path.exists()]


def resolve_selected_log_path(tab: str, *, selected_file_name: str) -> Path:
    available_paths = resolve_tab_log_paths(tab)
    if not available_paths:
        base_name = "app.log" if str(tab or "").strip().lower() == "info" else "error.log"
        return Path(settings.log_dir) / base_name
    if selected_file_name:
        for path in available_paths:
            if path.name == selected_file_name:
                return path
    return available_paths[0]


@app.post("/webhooks/{channel_id}/inbound")
async def inbound_webhook(channel_id: int, request: Request):
    body = await request.body()
    # URL 中的 channel_id 只能用于定位待校验凭证。平台会话完成签名预检后只保留
    # 可信 tenant_id 和渠道主键，后续事件、日志、告警及审计全部在租户会话中处理。
    with platform_session(SessionLocal) as auth_db:
        channel = get_channel(auth_db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

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
                "provider": normalized.get("provider"),
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


@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    db = get_session()
    try:
        snapshot = build_monitoring_snapshot(db)
        return render(
            request,
            "monitoring.html",
            page_title="监控与审计",
            **snapshot,
        )
    finally:
        db.close()


@app.post("/monitoring/alerts/{alert_id}/resolve")
async def resolve_alert_action(request: Request, alert_id: int):
    # 告警处理入口保持极轻：只改状态、写处理时间和审计，不引入备注或二次流转。
    enforce_role(request, "ops")
    user = require_user(request)
    db = get_session()
    try:
        alert = db.get(AlertRecord, alert_id)
        if not alert:
            return RedirectResponse(url="/monitoring?notice=" + quote("告警不存在"), status_code=303)

        if str(alert.status or "").strip().lower() != "open":
            return RedirectResponse(url="/monitoring?notice=" + quote("该告警已处理"), status_code=303)

        alert.status = "resolved"
        alert.resolved_at = now_shanghai()
        db.commit()
        db.refresh(alert)
        record_audit(
            db,
            "alert.resolve",
            "alert",
            alert.id,
            f"source={alert.source}, title={alert.title}",
            actor=user.username,
        )
        return RedirectResponse(url="/monitoring?notice=" + quote("已标记为已处理"), status_code=303)
    finally:
        db.close()


@app.post("/monitoring/restart-app")
async def restart_app_action(request: Request):
    enforce_role(request, "admin")
    script_path = APP_ROOT / "scripts" / "deploy_restart_mba.sh"
    if not script_path.exists():
        return RedirectResponse(
            url="/monitoring?notice=" + quote("重启脚本不存在") + "&tone=danger",
            status_code=303,
        )

    run_dir = APP_ROOT / ".run"
    run_dir.mkdir(parents=True, exist_ok=True)
    restart_log = run_dir / "deploy-restart-trigger.log"

    user = require_user(request)
    try:
        with restart_log.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] trigger restart by {user.username}; "
                f"cwd={APP_ROOT}; script={script_path}\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                ["bash", str(script_path)],
                cwd=APP_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Give the script a short window. If it exits immediately, surface the error instead of pretending success.
            time_module.sleep(0.6)
            quick_exit_code = process.poll()
        if quick_exit_code not in (None, 0):
            tail_lines = restart_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
            tail_preview = " | ".join(line.strip() for line in tail_lines if line.strip())
            reason = f"脚本快速退出（exit={quick_exit_code}）"
            if tail_preview:
                reason = f"{reason}：{tail_preview[-320:]}"
            return RedirectResponse(
                url="/monitoring?notice=" + quote(f"触发重启失败：{reason}") + "&tone=danger",
                status_code=303,
            )
    except Exception as exc:
        return RedirectResponse(
            url="/monitoring?notice=" + quote(f"触发重启失败：{exc}") + "&tone=danger",
            status_code=303,
        )

    db = get_session()
    try:
        record_audit(
            db,
            "ops.restart-app",
            "system",
            None,
            f"triggered by {user.username}, script={script_path.name}",
            actor=user.username,
        )
    finally:
        db.close()

    return RedirectResponse(
        url="/monitoring?notice=" + quote("已触发重启脚本，页面可能短暂中断"),
        status_code=303,
    )
