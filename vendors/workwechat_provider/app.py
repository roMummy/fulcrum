from __future__ import annotations

import base64
import copy
import contextvars
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlparse
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


def resolve_app_root() -> Path:
    # 单文件 UI 启动器会把 provider server 解包到临时目录执行；
    # 配置、日志和 data 必须落在 WORKWECHAT_PROVIDER_APP_ROOT 指向的稳定目录，避免 `_MEI...` 退出清理失败。
    external_root = os.environ.get("WORKWECHAT_PROVIDER_APP_ROOT", "")
    if external_root:
        return Path(external_root).resolve()

    # 独立运行 provider server 时，仍按 exe 同级目录作为应用根目录，兼容旧的目录交付方式。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = resolve_app_root()
CONFIG_PATH = APP_ROOT / "config.json"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import cloud_jobs


def resolve_bundled_root() -> Path | None:
    # 单文件 UI 启动器会先把 Hook runtime 从 _MEIPASS 复制到稳定 runtime；
    # provider server 是启动器拉起的子进程，需要通过环境变量读取这个稳定资源目录。
    inherited_root = os.environ.get("WORKWECHAT_PROVIDER_BUNDLED_ROOT", "")
    if inherited_root:
        inherited_path = Path(inherited_root).resolve()
        if inherited_path.exists():
            return inherited_path

    # provider server 自己也可能被 onefile 打包，保留 sys._MEIPASS 作为独立运行时的资源目录。
    bundled_root = getattr(sys, "_MEIPASS", "")
    if not bundled_root:
        return None
    bundled_path = Path(bundled_root).resolve()
    return bundled_path if bundled_path.exists() else None


class ProviderConfig(BaseModel):
    provider_host: str = "0.0.0.0"
    provider_port: int = 8181
    hook_api_base: str = "http://127.0.0.1:8989"
    hook_api_path: str = "/api"
    auto_start_hook: bool = True
    hook_runtime_dir: str = "hook_runtime"
    hook_python_executable: str = ""
    # 启动器会先检查本机企业微信版本；Hook DLL 对客户端版本敏感，所以默认要求精确匹配。
    required_workwechat_version: str = "5.0.0.6008"
    # 企业微信 Windows 客户端默认进程名，启动器用它定位 exe 路径并读取文件版本。
    workwechat_process_name: str = "WXWork.exe"
    # 打包版优先启动独立 Hook runtime exe；为空时保留现有 python http_py.py 兼容路径。
    hook_runtime_executable: str = ""
    hook_callback_url: str = ""
    hook_startup_timeout_seconds: int = 20
    callback_url: str = ""
    callback_secret: str = ""
    callback_timeout_seconds: int = 10
    provider_token: str = ""
    request_timeout_seconds: int = 12
    inbound_file_download_timeout_seconds: int = Field(default=45, ge=5, le=300)
    inbound_file_download_retries: int = Field(default=2, ge=0, le=5)
    health_probe_type: int | None = 11035
    health_probe_cache_ttl_seconds: int = Field(default=10, ge=1, le=300)
    debug_recent_message_limit: int = Field(default=50, ge=1, le=500)
    debug_log_enabled: bool = True
    debug_log_path: str = "logs/provider-debug.log"
    debug_log_body_max_chars: int = Field(default=20000, ge=1000, le=200000)
    chatroom_sync_page_size: int = Field(default=100, ge=1, le=500)
    chatroom_sync_max_pages: int = Field(default=20, ge=1, le=200)
    # 默认同步客服手动在企微群里发出的消息；服务端会按 out/self_sent 记录并跳过插件派发，避免消息回环。
    push_self_messages: bool = True
    inbound_file_cache_dir: str = "data/inbound_file_cache"
    outbound_file_cache_dir: str = "data/outbound_file_cache"
    cloud_poll_enabled: bool = False
    cloud_base_url: str = ""
    # SaaS 节点协议使用一次性激活码换取长期设备密钥；设备密钥只保存在客户电脑，
    # 不再与单个渠道绑定。旧 authorization_code/client_token 字段仅保留迁移兼容。
    provider_node_name: str = ""
    activation_code: str = ""
    device_token: str = ""
    device_id: str = ""
    heartbeat_interval_seconds: int = Field(default=20, ge=5, le=300)
    poll_timeout_seconds: int = Field(default=25, ge=5, le=120)
    # 一个节点可以管理多个本地 Hook 端点。每项至少包含 hook_api_base；account_key
    # 可以留空，由 Hook /status 返回的 current_user_id 作为稳定账号键。
    provider_accounts: list[dict[str, Any]] = Field(default_factory=list)
    legacy_cloud_registration_detected: bool = False
    authorization_code: str = ""
    client_token: str = ""
    cloud_channel_id: int | None = None
    provider_name: str = ""
    poll_interval_seconds: int = 30
    poll_batch_size: int = 5
    # 企微 Hook 的文本、图片、文件等发送接口没有同步投递回执；
    # 开启后 provider 会等当前账号自发消息回推，再向 MBA 上报真正 delivered。
    delivery_confirmation_enabled: bool = True
    delivery_confirmation_timeout_seconds: int = Field(default=180, ge=10, le=1800)
    delivery_confirmation_retry_interval_seconds: int = Field(default=10, ge=1, le=300)


def load_config() -> ProviderConfig:
    if not CONFIG_PATH.exists():
        return ProviderConfig()
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    migrated, _changed = migrate_legacy_cloud_config(raw if isinstance(raw, dict) else {})
    return ProviderConfig.model_validate(migrated)


def migrate_legacy_cloud_config(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """把旧的每渠道云端配置收口为节点配置，并标记需要重新激活的现场。

    旧 client_token 与新 device_token 的信任范围不同，不能直接改名复用。只有现场已经
    把新 pact_ 激活码填进旧授权码输入框时可以无损迁移；真正的 wwauth_/client_token
    只能清理后提示管理员重新生成节点激活码，避免把渠道凭证升级成节点凭证。
    """

    migrated = dict(raw or {})
    changed = False
    legacy_authorization = str(migrated.get("authorization_code") or "").strip()
    legacy_client_token = str(migrated.get("client_token") or "").strip()
    legacy_channel_id = migrated.get("cloud_channel_id")
    if not str(migrated.get("activation_code") or "").strip() and legacy_authorization.startswith("pact_"):
        migrated["activation_code"] = legacy_authorization
        changed = True
    if legacy_authorization or legacy_client_token or legacy_channel_id not in (None, ""):
        migrated["legacy_cloud_registration_detected"] = True
        migrated["authorization_code"] = ""
        migrated["client_token"] = ""
        migrated["cloud_channel_id"] = None
        changed = True
    return migrated, changed


config = load_config()
recent_messages: deque[dict[str, Any]] = deque(maxlen=config.debug_recent_message_limit)
hook_process: subprocess.Popen | None = None
hook_process_owned = False
hook_python_used = ""
logger = logging.getLogger("workwechat_provider")
standard_stream_handles: list[Any] = []
cloud_ws_thread: threading.Thread | None = None
cloud_poll_fallback_thread: threading.Thread | None = None
cloud_ws_stop_event = threading.Event()
cloud_poll_lock = threading.Lock()
active_hook_api_base: contextvars.ContextVar[str] = contextvars.ContextVar("active_hook_api_base", default="")
active_provider_account_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_provider_account_key",
    default="",
)
delivery_confirmation_lock = threading.Lock()
cloud_status: dict[str, Any] = {
    "enabled": False,
    "ws_connected": False,
    "last_ws_event_at": "",
    "last_poll_at": "",
    "pending_delivery_count": 0,
    "last_delivery_confirmation_at": "",
    "last_error": "",
}
inbound_callback_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workwechat-inbound")
inbound_callback_lock = threading.Lock()
inbound_callback_status: dict[str, Any] = {
    "submitted": 0,
    "completed": 0,
    "failed": 0,
    "pending": 0,
    "last_submitted_at": "",
    "last_completed_at": "",
    "last_failed_at": "",
    "last_error": "",
}
hook_health_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workwechat-hook-health")
hook_health_lock = threading.Lock()
hook_health_status: dict[str, Any] = {
    "refreshing": False,
    "last_started_at": "",
    "last_checked_at": "",
    "last_duration_ms": None,
    "last_error": "",
    "hook_status": {"ok": None, "reason": "probe_pending"},
    "hook_runtime_status": {"ok": None, "reason": "probe_pending"},
    "_last_checked_epoch": 0.0,
}

# Hook runtime 明确声明这些发送类型没有同步返回；SendWeChatData 返回成功只表示提交给客户端，
# 不能作为群内真实投递依据，所以云端任务需要等自发消息回推后再 complete。
DELIVERY_CONFIRMATION_COMMAND_TYPES = {
    11029,  # 文本
    11069,  # 群 @ 文本
    11030,  # 图片
    11031,  # 文件
    11070,  # 动图
    11067,  # 视频
}
DELIVERY_CONFIRMATION_MESSAGE_TYPES = {
    11029: "text",
    11069: "text",
    11030: "image",
    11031: "file",
    11070: "emoji",
    11067: "video",
}


def ensure_standard_streams() -> None:
    # PyInstaller 使用 --noconsole 打包时，Windows 会让 stdout/stderr/stdin 变成 None；
    # uvicorn 默认日志 formatter 会调用 stderr.isatty()，所以这里统一兜底到 os.devnull。
    stream_specs = (
        ("stdin", "r"),
        ("stdout", "w"),
        ("stderr", "w"),
    )
    for stream_name, mode in stream_specs:
        if getattr(sys, stream_name) is not None:
            continue
        stream = open(os.devnull, mode, encoding="utf-8")
        standard_stream_handles.append(stream)
        setattr(sys, stream_name, stream)


def configure_debug_logging() -> None:
    # 调试日志默认同时输出到控制台和 provider 自己的滚动日志文件，便于现场直接看窗口和事后追查。
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if config.debug_log_enabled else logging.INFO)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.DEBUG if config.debug_log_enabled else logging.INFO)
    logger.addHandler(stream_handler)

    log_path = resolve_debug_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG if config.debug_log_enabled else logging.INFO)
    logger.addHandler(file_handler)


def resolve_debug_log_path() -> Path:
    log_path = Path(config.debug_log_path)
    if not log_path.is_absolute():
        log_path = APP_ROOT / log_path
    return log_path


def safe_json_dumps(value: Any) -> str:
    # ensure_ascii=False 能保留企微消息里的中文，调试时不用再手动转义。
    try:
        return json.dumps(value, ensure_ascii=False, default=str, indent=2)
    except TypeError:
        return str(value)


def truncate_for_log(text: str) -> str:
    limit = max(1000, config.debug_log_body_max_chars)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... <truncated {len(text) - limit} chars>"


def mask_sensitive_headers(headers: Any) -> dict[str, str]:
    # 请求体完整保留；header 中的 token、签名、cookie 做脱敏，避免日志文件泄漏认证信息。
    sensitive_names = {
        "authorization",
        "cookie",
        "set-cookie",
        "x-provider-token",
        "x-signature",
        "x-api-key",
        "x-access-token",
        "x-provider-device-token",
    }
    masked: dict[str, str] = {}
    for key, value in dict(headers or {}).items():
        key_text = str(key)
        value_text = str(value)
        if key_text.lower() in sensitive_names:
            masked[key_text] = mask_secret(value_text)
        else:
            masked[key_text] = value_text
    return masked


def redact_sensitive_payload(value: Any) -> Any:
    """递归脱敏请求体中的凭证字段，避免调试日志记录激活码或设备密钥原文。"""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(marker in normalized_key for marker in ("token", "secret", "password", "activation_code", "authorization_code")):
                redacted[str(key)] = mask_secret(str(item or ""))
            else:
                redacted[str(key)] = redact_sensitive_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_payload(item) for item in value]
    return value


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


def log_debug_event(label: str, payload: Any) -> None:
    if not config.debug_log_enabled:
        return
    logger.debug("%s\n%s", label, truncate_for_log(safe_json_dumps(payload)))


def log_info_event(label: str, payload: Any) -> None:
    logger.info("%s\n%s", label, truncate_for_log(safe_json_dumps(payload)))


async def log_incoming_request(request: Request, label: str, body: Any | None = None) -> None:
    # 每个入口显式调用，避免中间件提前消费 body 后影响 FastAPI 正常解析 JSON。
    payload = {
        "method": request.method,
        "url": str(request.url),
        "client": request.client.host if request.client else None,
        "headers": mask_sensitive_headers(request.headers),
        "body": body,
    }
    log_debug_event(label, payload)


def config_debug_snapshot() -> dict[str, Any]:
    # 启动和 reload 时输出关键配置，但 token/secret 只显示掩码，方便核对端口和回调地址。
    return {
        "provider_host": config.provider_host,
        "provider_port": config.provider_port,
        "hook_api_base": config.hook_api_base,
        "hook_api_path": config.hook_api_path,
        "auto_start_hook": config.auto_start_hook,
        "hook_runtime_dir": str(resolve_hook_runtime_dir()),
        "hook_python_executable": config.hook_python_executable,
        "hook_callback_url": build_hook_callback_url(),
        "callback_url": config.callback_url,
        "callback_secret": mask_secret(config.callback_secret),
        "provider_token": mask_secret(config.provider_token),
        "health_probe_type": config.health_probe_type,
        "health_probe_cache_ttl_seconds": config.health_probe_cache_ttl_seconds,
        "debug_log_enabled": config.debug_log_enabled,
        "debug_log_path": str(resolve_debug_log_path()),
        "debug_log_body_max_chars": config.debug_log_body_max_chars,
        "push_self_messages": config.push_self_messages,
        "inbound_file_cache_dir": str(resolve_inbound_file_cache_dir()),
        "outbound_file_cache_dir": str(resolve_outbound_file_cache_dir()),
        "cloud_poll_enabled": config.cloud_poll_enabled,
        "cloud_base_url": config.cloud_base_url,
        "provider_node_name": config.provider_node_name,
        "device_id": config.device_id,
        "device_token": mask_secret(config.device_token),
        "activation_code": mask_secret(config.activation_code),
        "provider_account_count": len(config.provider_accounts),
        "cloud_channel_id": config.cloud_channel_id,
        "provider_name": config.provider_name,
        "client_token": mask_secret(config.client_token),
        "authorization_code": mask_secret(config.authorization_code),
        "delivery_confirmation_enabled": config.delivery_confirmation_enabled,
        "delivery_confirmation_timeout_seconds": config.delivery_confirmation_timeout_seconds,
        "delivery_confirmation_retry_interval_seconds": config.delivery_confirmation_retry_interval_seconds,
    }


ensure_standard_streams()
configure_debug_logging()


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    # provider 启动时自动准备内置 Hook runtime，关闭时只回收自己拉起的 Hook 子进程。
    log_info_event("workwechat provider startup config", config_debug_snapshot())
    hook_start = start_embedded_hook_if_needed()
    log_info_event("workwechat provider embedded hook startup result", hook_start)
    start_cloud_ws_if_needed()
    try:
        yield
    finally:
        stop_cloud_ws()
        # 退出时停止接收入站后台任务；已提交任务继续自行收尾，避免强杀导致消息上报中断。
        inbound_callback_executor.shutdown(wait=False, cancel_futures=False)
        # 健康探测只服务于 UI 状态展示，退出时不等待探测线程，避免关闭 provider 被 8989 阻塞。
        hook_health_executor.shutdown(wait=False, cancel_futures=False)
        stop_embedded_hook()


app = FastAPI(title="workwechat_provider", version="0.1.0", lifespan=lifespan)


def assert_provider_authorized(request: Request) -> None:
    if not config.provider_token:
        return
    provided = str(request.headers.get("X-Provider-Token") or "").strip()
    if not provided or not hmac.compare_digest(config.provider_token, provided):
        raise HTTPException(status_code=401, detail="invalid_provider_token")


def build_hook_url() -> str:
    path = config.hook_api_path if config.hook_api_path.startswith("/") else f"/{config.hook_api_path}"
    hook_api_base = active_hook_api_base.get().strip() or config.hook_api_base
    return f"{hook_api_base.rstrip('/')}{path}"


def build_hook_status_url() -> str:
    # Hook runtime 的状态接口用于排查真实消息是否能推到 provider；
    # 与 /api 同源，只把路径固定替换为 /status。
    hook_api_base = active_hook_api_base.get().strip() or config.hook_api_base
    return f"{hook_api_base.rstrip('/')}/status"


def resolve_hook_runtime_dir() -> Path:
    runtime_dir = Path(config.hook_runtime_dir)
    if not runtime_dir.is_absolute():
        external_runtime_dir = APP_ROOT / runtime_dir
        bundled_root = resolve_bundled_root()
        bundled_runtime_dir = bundled_root / runtime_dir if bundled_root else None

        # 客服交付版只有一个可见 UI exe，Hook runtime 会被启动器复制到 LocalAppData 的稳定 runtime；
        # 如果 app_root 下存在 hook_runtime 目录，则仍优先使用它，便于现场替换 DLL 排查问题。
        if external_runtime_dir.exists():
            runtime_dir = external_runtime_dir
        elif bundled_runtime_dir and bundled_runtime_dir.exists():
            runtime_dir = bundled_runtime_dir
        else:
            runtime_dir = external_runtime_dir
    return runtime_dir


def resolve_hook_runtime_executable() -> Path | None:
    # 打包版会把 Hook runtime 编译成独立 exe，provider 优先启动它，避免客服机器安装 32 位 Python。
    configured = Path(config.hook_runtime_executable) if config.hook_runtime_executable else None
    if configured:
        if not configured.is_absolute():
            configured = APP_ROOT / configured
        return configured if configured.exists() else None

    bundled = resolve_hook_runtime_dir() / "WorkWechatHookRuntime.exe"
    return bundled if bundled.exists() else None


def build_hook_runtime_command(runtime_dir: Path) -> tuple[list[str], str]:
    runtime_exe = resolve_hook_runtime_executable()
    if runtime_exe:
        return [str(runtime_exe)], "executable"

    script_path = runtime_dir / "http_py.py"
    return [choose_hook_python_executable(), str(script_path)], "python"


def build_hook_callback_url() -> str:
    if config.hook_callback_url:
        return config.hook_callback_url
    # Hook 子进程和 provider 在同一台机器，使用 127.0.0.1 可以避免 0.0.0.0 不能作为回调目标的问题。
    return f"http://127.0.0.1:{config.provider_port}/hook-callback"


def parse_hook_endpoint() -> tuple[str, int]:
    parsed = urlparse(config.hook_api_base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def is_tcp_port_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def choose_hook_python_executable() -> str:
    if config.hook_python_executable:
        return config.hook_python_executable

    # 企微 Hook 资料标注的测试环境是 Python 3.10 32 位，优先复用本机常见 32 位安装路径。
    candidates = [
        Path(r"D:\Program Files (x86)\Python310\python.exe"),
        Path(r"C:\Program Files (x86)\Python310\python.exe"),
        Path(r"D:\Python310-32\python.exe"),
        Path(r"C:\Python310-32\python.exe"),
        Path(r"D:\Program Files\Python310\python.exe"),
        Path(r"C:\Program Files\Python310\python.exe"),
        Path(r"C:\Python310\python.exe"),
    ]
    for candidate in candidates:
        if candidate.exists() and python_bitness(str(candidate)) == 32 and python_can_import_flask(str(candidate)):
            return str(candidate)
    return sys.executable


def python_bitness(python_executable: str) -> int | None:
    try:
        # GUI 打包进程没有控制台，探测外部 python 时也不应弹出临时 cmd 窗口。
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(
            [python_executable, "-c", "import struct; print(struct.calcsize('P') * 8)"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=creationflags,
        )
        return int(str(completed.stdout or "").strip())
    except Exception:
        return None


def python_can_import_flask(python_executable: str) -> bool:
    try:
        # 这里在启动检查阶段可能被频繁调用，Windows 下必须无窗口执行。
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(
            [python_executable, "-c", "import flask"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            creationflags=creationflags,
        )
        return completed.returncode == 0
    except Exception:
        return False


def start_embedded_hook_if_needed() -> dict[str, Any]:
    global hook_process, hook_process_owned, hook_python_used
    if not config.auto_start_hook:
        return {"started": False, "reason": "auto_start_hook_disabled"}

    host, port = parse_hook_endpoint()
    if is_tcp_port_open(host, port):
        return {"started": False, "reason": "hook_api_already_listening", "host": host, "port": port}

    runtime_dir = resolve_hook_runtime_dir()
    command, runtime_mode = build_hook_runtime_command(runtime_dir)
    if runtime_mode == "python" and not Path(command[-1]).exists():
        return {"started": False, "reason": "missing_hook_runtime", "script_path": command[-1]}

    python_executable = command[0] if runtime_mode == "python" else ""
    hook_python_used = python_executable
    log_dir = APP_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "hook-runtime.out.log"
    stderr_path = log_dir / "hook-runtime.err.log"
    env = os.environ.copy()
    env["MSG_PUSH_URL"] = build_hook_callback_url()
    env["API_HOST"] = host
    env["API_PORT"] = str(port)
    env["WORKWECHAT_PUSH_SELF_MESSAGES"] = "1" if config.push_self_messages else "0"
    # Hook runtime 会记录企微中文消息和少量状态符号；显式指定 UTF-8，避免 Windows GUI 子进程继承 GBK 后写日志报错。
    env["PYTHONIOENCODING"] = "utf-8"
    # 把 UI/provider 配置的企业微信进程名传给 hook runtime，runtime 在注入失败时
    # 会用它判断是否已有客户端，避免误调用会启动新客户端的 MultiOpen 分支。
    env["WORKWECHAT_PROCESS_NAME"] = config.workwechat_process_name
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("MSG_PORT", "0")

    creationflags = 0
    if os.name == "nt":
        # 同时保留进程组能力和无窗口启动，避免 Hook runtime 失败重试时闪出 cmd 框。
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    # Hook runtime 独立成子进程，避免 DLL 注入和 Flask 开发服务器污染 provider 主进程。
    hook_process = subprocess.Popen(
        command,
        cwd=str(runtime_dir),
        env=env,
        stdout=stdout_path.open("ab"),
        stderr=stderr_path.open("ab"),
        creationflags=creationflags,
    )
    hook_process_owned = True

    deadline = time.time() + max(1, config.hook_startup_timeout_seconds)
    while time.time() < deadline:
        if hook_process.poll() is not None:
            return {
                "started": False,
                "reason": "hook_process_exited",
                "returncode": hook_process.returncode,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
        if is_tcp_port_open(host, port):
            return {
                "started": True,
                "pid": hook_process.pid,
                "host": host,
                "port": port,
                "runtime_mode": runtime_mode,
                "python": python_executable,
                "python_bitness": python_bitness(python_executable) if python_executable else None,
                "runtime_dir": str(runtime_dir),
            }
        time.sleep(0.5)

    return {
        "started": True,
        "reason": "hook_startup_pending",
        "pid": hook_process.pid,
        "host": host,
        "port": port,
        "runtime_mode": runtime_mode,
        "python": python_executable,
        "python_bitness": python_bitness(python_executable) if python_executable else None,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }


def stop_embedded_hook() -> None:
    global hook_process, hook_process_owned, hook_python_used
    if not hook_process or not hook_process_owned:
        return
    if hook_process.poll() is None:
        hook_process.terminate()
        try:
            hook_process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            hook_process.kill()
    hook_process = None
    hook_process_owned = False
    hook_python_used = ""


def post_json(url: str, body: dict[str, Any], *, timeout: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "workwechat-provider/0.1")
    for key, value in (headers or {}).items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            req.add_header(key_text, value_text)
    log_debug_event(
        "outbound request to hook api",
        {
            "url": url,
            "timeout": timeout,
            "headers": mask_sensitive_headers(req.headers),
            "body": redact_sensitive_payload(body),
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = parse_response_body(raw, getattr(response, "status", 200), getattr(response, "url", url))
            log_debug_event("outbound response from hook api", parsed)
            return parsed
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = parse_response_body(raw, exc.code, url)
        log_debug_event("outbound http error response from hook api", parsed)
        return parsed
    except URLError as exc:
        log_debug_event("outbound transport error from hook api", {"url": url, "error": str(exc.reason or exc)})
        raise RuntimeError(str(exc.reason or exc)) from exc


def get_json(url: str, *, timeout: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urlrequest.Request(url, method="GET")
    req.add_header("User-Agent", "workwechat-provider/0.1")
    for key, value in (headers or {}).items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            req.add_header(key_text, value_text)
    log_debug_event(
        "outbound get request",
        {"url": url, "timeout": timeout, "headers": mask_sensitive_headers(req.headers)},
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = parse_response_body(raw, getattr(response, "status", 200), getattr(response, "url", url))
            log_debug_event("outbound get response", parsed)
            return parsed
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = parse_response_body(raw, exc.code, url)
        log_debug_event("outbound get http error response", parsed)
        return parsed
    except URLError as exc:
        log_debug_event("outbound get transport error", {"url": url, "error": str(exc.reason or exc)})
        raise RuntimeError(str(exc.reason or exc)) from exc


def cloud_post_json(
    path: str,
    body: dict[str, Any],
    token: str = "",
    *,
    device_token: str = "",
    timeout: int | None = None,
) -> dict[str, Any]:
    if not config.cloud_base_url:
        raise RuntimeError("missing_cloud_base_url")
    normalized_path = path if path.startswith("/") else f"/{path}"
    headers = {}
    if device_token:
        headers["X-Provider-Device-Token"] = device_token
    elif token:
        headers["X-WorkWechat-Client-Token"] = token
    return post_json(
        f"{config.cloud_base_url.rstrip('/')}{normalized_path}",
        body,
        timeout=int(timeout or config.request_timeout_seconds),
        headers=headers,
    )


def save_config_updates(updates: dict[str, Any]) -> None:
    global config
    # 激活成功后把设备凭证原子保存到稳定目录；临时文件 chmod 为仅当前用户可读写，
    # Windows 会忽略部分 POSIX 权限位，但仍保留原子 replace，避免断电写坏配置。
    current: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            parsed = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            current = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            current = {}
    current, _changed = migrate_legacy_cloud_config(current)
    current.update(updates)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    temp_path.replace(CONFIG_PATH)
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    config = ProviderConfig.model_validate(current)
    log_info_event(
        "workwechat provider local config updated",
        {key: mask_secret(str(value)) if "token" in key or "code" in key else value for key, value in updates.items()},
    )


def uses_provider_node_protocol() -> bool:
    """判断当前是否使用节点协议；全新配置默认进入节点协议，旧 client_token 仍可短期兼容。"""

    return bool(config.device_token or config.activation_code or not config.client_token)


def build_device_fingerprint() -> str:
    """生成不含本机明文标识的稳定设备指纹，只把摘要提交给云端。"""

    raw = "|".join(
        [
            socket.gethostname(),
            str(uuid.getnode()),
            os.environ.get("COMPUTERNAME", ""),
            os.environ.get("PROCESSOR_IDENTIFIER", ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def activate_provider_node_if_needed() -> dict[str, Any]:
    """用一次性激活码换取节点设备凭证，成功后立即清空激活码。"""

    if config.device_token:
        return {"ok": True, "activated": False, "reason": "device_token_exists"}
    if not config.activation_code:
        return {"ok": False, "activated": False, "error": "missing_activation_code"}
    response = cloud_post_json(
        "/api/provider/activate",
        {
            "activation_code": config.activation_code,
            "fingerprint": build_device_fingerprint(),
            "client_version": config.required_workwechat_version,
        },
    )
    if not response_ok(response):
        return {"ok": False, "activated": False, "error": response.get("detail") or response.get("error") or response}
    device_token = str(response.get("device_token") or "").strip()
    device_id = str(response.get("device_id") or "").strip()
    if not device_token or not device_id:
        return {"ok": False, "activated": False, "error": "activation_response_missing_device_identity"}
    save_config_updates(
        {
            "activation_code": "",
            "device_token": device_token,
            "device_id": device_id,
            "authorization_code": "",
            "client_token": "",
            "cloud_channel_id": None,
            "legacy_cloud_registration_detected": False,
            "heartbeat_interval_seconds": int(response.get("heartbeat_interval_seconds") or config.heartbeat_interval_seconds),
            "poll_timeout_seconds": int(response.get("poll_timeout_seconds") or config.poll_timeout_seconds),
        }
    )
    return {"ok": True, "activated": True, "device_id": device_id}


def list_local_provider_accounts() -> list[dict[str, Any]]:
    """探测本机 Hook 账号并返回云端心跳摘要，不用配置内容冒充在线账号。"""

    configured_accounts = config.provider_accounts or [{"hook_api_base": config.hook_api_base}]
    accounts: list[dict[str, Any]] = []
    for configured in configured_accounts:
        if not isinstance(configured, dict):
            continue
        hook_api_base = str(configured.get("hook_api_base") or config.hook_api_base).strip().rstrip("/")
        configured_key = str(configured.get("account_key") or "").strip()
        status_payload: dict[str, Any] = {}
        status = "offline"
        try:
            status_payload = get_json(f"{hook_api_base}/status", timeout=config.request_timeout_seconds)
            discovered_key = str(status_payload.get("current_user_id") or "").strip()
            account_key = configured_key or discovered_key
            if discovered_key and account_key and (not configured_key or configured_key == discovered_key):
                status = "online" if response_ok(status_payload) else "offline"
            else:
                status = "unknown"
        except Exception as exc:
            account_key = configured_key
            status_payload = {"error": str(exc)}
        if not account_key:
            continue
        accounts.append(
            {
                "account_key": account_key,
                "display_name": str(configured.get("display_name") or account_key).strip(),
                "external_user_id": str(status_payload.get("current_user_id") or account_key).strip(),
                "status": status,
                # hook_api_base 只用于本地任务分派，不发送给云端，避免暴露客户内网端口布局。
                "_hook_api_base": hook_api_base,
            }
        )
    return accounts


def send_provider_heartbeat_once() -> dict[str, Any]:
    """上报节点和本机账号摘要；到期租户仍可正常心跳。"""

    activated = activate_provider_node_if_needed()
    if not activated.get("ok") or not config.device_token:
        return activated
    local_accounts = list_local_provider_accounts()
    response = cloud_post_json(
        "/api/provider/heartbeat",
        {
            "client_version": config.required_workwechat_version,
            "hook_status": "running" if any(item.get("status") == "online" for item in local_accounts) else "degraded",
            "workwechat_status": "running" if local_accounts else "unknown",
            "accounts": [{key: value for key, value in item.items() if not key.startswith("_")} for item in local_accounts],
            "details": {"provider_node_name": config.provider_node_name},
        },
        device_token=config.device_token,
    )
    return response


def resolve_local_provider_account(job: dict[str, Any]) -> dict[str, Any] | None:
    """按云端签发的账号键唯一匹配本地在线 Hook，禁止客户端自行改选账号。"""

    account_key = str(job.get("provider_account_key") or "").strip()
    if not account_key:
        return None
    matches = [item for item in list_local_provider_accounts() if item.get("account_key") == account_key and item.get("status") == "online"]
    return matches[0] if len(matches) == 1 else None


def current_local_account_key() -> str:
    """返回当前回调对应的唯一在线账号键；多账号无法判定时拒绝猜测。"""

    callback_account_key = active_provider_account_key.get().strip()
    if callback_account_key:
        return callback_account_key
    online_accounts = [item for item in list_local_provider_accounts() if item.get("status") == "online"]
    if len(online_accounts) != 1:
        return ""
    return str(online_accounts[0].get("account_key") or "").strip()


def register_cloud_client_if_needed() -> dict[str, Any]:
    if not config.cloud_poll_enabled:
        return {"ok": True, "registered": False, "reason": "cloud_poll_disabled"}
    if config.client_token:
        return {"ok": True, "registered": False, "reason": "client_token_exists"}
    if not config.authorization_code:
        return {"ok": False, "registered": False, "error": "missing_authorization_code"}

    response = cloud_post_json(
        "/api/workwechat/provider/register",
        {
            "authorization_code": config.authorization_code,
            "provider_name": config.provider_name,
            "client_version": config.required_workwechat_version,
        },
    )
    if not response_ok(response):
        return {"ok": False, "registered": False, "error": response.get("detail") or response.get("error") or response}

    updates = {
        "authorization_code": "",
        "client_token": str(response.get("client_token") or ""),
        "cloud_channel_id": response.get("channel_id"),
        # 云端注册模式已经改为 client_token 入站；这里主动清空旧 callback 字段，
        # 避免历史配置残留的 /webhooks/{channel_id}/inbound 继续触发 Bot Token 签名失败。
        "callback_url": str(response.get("callback_url") or ""),
        "callback_secret": str(response.get("callback_secret") or ""),
        "poll_interval_seconds": int(response.get("poll_interval_seconds") or config.poll_interval_seconds),
        "poll_batch_size": int(response.get("poll_batch_size") or config.poll_batch_size),
    }
    save_config_updates(updates)
    return {"ok": True, "registered": True, "channel_id": updates["cloud_channel_id"]}


def poll_cloud_jobs_once() -> dict[str, Any]:
    if not cloud_poll_lock.acquire(blocking=False):
        return {"ok": True, "skipped": True, "reason": "poll_already_running"}
    try:
        return _poll_cloud_jobs_once_locked()
    finally:
        cloud_poll_lock.release()


def _poll_cloud_jobs_once_locked() -> dict[str, Any]:
    if uses_provider_node_protocol():
        activation = activate_provider_node_if_needed()
        if not activation.get("ok"):
            return activation
        if not config.device_token:
            return {"ok": False, "error": "missing_device_token"}
        delivery_flush = flush_pending_delivery_confirmations()
        response = cloud_post_json(
            "/api/provider/poll",
            {"batch_size": config.poll_batch_size},
            device_token=config.device_token,
            timeout=max(config.request_timeout_seconds, config.poll_timeout_seconds + 5),
        )
        http_status = int(response.get("http_status") or 200)
        if http_status == 401:
            # 设备密钥被吊销后立即停止后台轮询，避免持续打满云端日志；UI 通过该状态提示重新激活。
            cloud_status["authentication_stopped"] = True
            cloud_status["last_error"] = "device_reactivation_required"
            cloud_ws_stop_event.set()
            return {"ok": False, "error": "device_reactivation_required", "http_status": 401}
        jobs = response.get("jobs") if isinstance(response.get("jobs"), list) else []
        results = [process_cloud_job(job) for job in jobs if isinstance(job, dict)]
        return {
            "ok": response_ok(response),
            "http_status": http_status,
            "job_count": len(jobs),
            "results": results,
            "read_only": bool(response.get("read_only")),
            "delivery_flush": delivery_flush,
        }

    registration = register_cloud_client_if_needed()
    if not registration.get("ok"):
        return registration
    if not config.client_token:
        return {"ok": False, "error": "missing_client_token"}
    delivery_flush = flush_pending_delivery_confirmations()
    response = cloud_post_json(
        "/api/workwechat/provider/poll",
        {"batch_size": config.poll_batch_size},
        token=config.client_token,
    )
    jobs = response.get("jobs") if isinstance(response.get("jobs"), list) else []
    results = [process_cloud_job(job) for job in jobs if isinstance(job, dict)]
    return {"ok": response_ok(response), "job_count": len(jobs), "results": results, "delivery_flush": delivery_flush}


def process_cloud_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = int(job.get("job_id") or 0)
    payload_hash = str(job.get("payload_hash") or "").strip()
    lease_token = str(job.get("lease_token") or "").strip()
    if not job_id or not payload_hash:
        return {"ok": False, "status": "invalid_job"}
    local_account: dict[str, Any] | None = None
    is_node_job = bool(str(job.get("provider_account_key") or "").strip() or lease_token)
    if is_node_job:
        local_account = resolve_local_provider_account(job)
        if local_account is None:
            result = {
                "ok": False,
                "status": "provider_account_not_uniquely_available",
                "job_id": job_id,
                "provider_account_key": str(job.get("provider_account_key") or ""),
            }
            report_cloud_job_fail_for_lease(job_id, "provider_account_not_uniquely_available", result, lease_token)
            return result
    state_dir = resolve_cloud_jobs_state_dir()
    if cloud_jobs.has_processed_payload_conflict(state_dir, job_id=job_id, payload_hash=payload_hash):
        result = {"ok": False, "status": "payload_hash_conflict", "job_id": job_id}
        report_cloud_job_fail_for_lease(job_id, "payload_hash_conflict", result, lease_token)
        return result
    if cloud_jobs.is_job_processed(state_dir, job_id=job_id, payload_hash=payload_hash):
        result = {"ok": True, "status": "already_processed", "job_id": job_id}
        report_cloud_job_complete_for_lease(job_id, {"adapter_result": result}, lease_token)
        return result
    if not cloud_jobs.try_acquire_job_lock(state_dir, job_id=job_id, payload_hash=payload_hash, event_id=str(job.get("event_id") or "")):
        return {"ok": False, "status": "locked", "job_id": job_id}

    wait_for_delivery = False
    hook_context = active_hook_api_base.set(str((local_account or {}).get("_hook_api_base") or ""))
    try:
        action = str(job.get("action") or "").strip()
        if action:
            return process_cloud_control_job(
                job,
                action,
                state_dir=state_dir,
                job_id=job_id,
                payload_hash=payload_hash,
                lease_token=lease_token,
            )
        command = build_hook_command(job.get("payload") if isinstance(job.get("payload"), dict) else {})
        wait_for_delivery = should_wait_for_delivery_confirmation(command)
        if wait_for_delivery:
            pending_record = build_pending_delivery_record(
                job_id=job_id,
                payload_hash=payload_hash,
                lease_token=lease_token,
                provider_account_key=str(job.get("provider_account_key") or ""),
                hook_api_base=str((local_account or {}).get("_hook_api_base") or ""),
                command=command,
                accepted_response={},
                adapter_result={
                    "ok": True,
                    "status": "pending_delivery",
                    "external_status": "sending",
                    "external_message_id": None,
                    "hook_command_type": command.get("type"),
                    "hook_response": {},
                },
            )
            register_pending_delivery_confirmation(pending_record)
        response = post_json(build_hook_url(), command, timeout=config.request_timeout_seconds)
        ok = response_ok(response)
        adapter_result = {
            "ok": ok,
            "status": "sent" if ok else "failed",
            "external_status": "sent" if ok else "failed",
            "external_message_id": response.get("external_message_id"),
            "hook_command_type": command.get("type"),
            "hook_response": response,
        }
        if ok:
            if wait_for_delivery:
                pending_summary = update_pending_delivery_acceptance(
                    job_id,
                    accepted_response=response,
                    adapter_result={
                        **adapter_result,
                        "status": "pending_delivery",
                        "external_status": "accepted",
                    },
                )
                return {
                    **adapter_result,
                    "status": "pending_delivery",
                    "external_status": "accepted",
                    "delivery_confirmation": pending_summary,
                }
            cloud_jobs.mark_job_processed(state_dir, job_id=job_id, payload_hash=payload_hash, result=adapter_result)
            report_cloud_job_complete_for_lease(job_id, {"adapter_result": adapter_result}, lease_token)
        else:
            if wait_for_delivery:
                remove_pending_delivery_confirmation(job_id)
            cloud_jobs.release_job_lock(state_dir, job_id=job_id)
            report_cloud_job_fail_for_lease(job_id, "hook_send_failed", adapter_result, lease_token)
        return adapter_result
    except Exception as exc:
        if wait_for_delivery:
            remove_pending_delivery_confirmation(job_id)
        cloud_jobs.release_job_lock(state_dir, job_id=job_id)
        result = {"ok": False, "status": "failed", "error": str(exc)}
        report_cloud_job_fail_for_lease(job_id, "hook_send_exception", result, lease_token)
        return result
    finally:
        active_hook_api_base.reset(hook_context)


def process_cloud_control_job(
    job: dict[str, Any],
    action: str,
    *,
    state_dir: Path,
    job_id: int,
    payload_hash: str,
    lease_token: str = "",
) -> dict[str, Any]:
    # 云端控制任务不是发消息，而是让客服电脑本地执行 Hook 查询；结果仍通过 complete 接口回传服务端统一落库。
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    if action == "sync_chatrooms":
        page_result = fetch_hook_pages(
            hook_type=11038,
            request_data={},
            list_key="room_list",
            page_size=payload.get("page_size"),
            max_pages=payload.get("max_pages"),
        )
        rooms = page_result.pop("items", [])
        adapter_result = {
            "ok": bool(page_result.get("ok")),
            "action": action,
            "rooms": rooms,
            "total": page_result.get("total") or len(rooms),
            "total_page": page_result.get("total_page"),
            "page_size": page_result.get("page_size"),
            "hook_response": page_result,
        }
        if adapter_result["ok"]:
            cloud_jobs.mark_job_processed(state_dir, job_id=job_id, payload_hash=payload_hash, result=adapter_result)
            report_cloud_job_complete_for_lease(job_id, {"adapter_result": adapter_result}, lease_token)
        else:
            cloud_jobs.release_job_lock(state_dir, job_id=job_id)
            report_cloud_job_fail_for_lease(job_id, "hook_chatroom_sync_failed", adapter_result, lease_token)
        return adapter_result

    if action == "sync_chatroom_members":
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            cloud_jobs.release_job_lock(state_dir, job_id=job_id)
            result = {"ok": False, "status": "missing_conversation_id", "action": action}
            report_cloud_job_fail_for_lease(job_id, "missing_conversation_id", result, lease_token)
            return result
        page_result = fetch_hook_pages(
            hook_type=11040,
            request_data={"conversation_id": conversation_id},
            list_key="member_list",
            page_size=payload.get("page_size"),
            max_pages=payload.get("max_pages"),
        )
        members = page_result.pop("items", [])
        adapter_result = {
            "ok": bool(page_result.get("ok")),
            "action": action,
            "conversation_id": conversation_id,
            "members": members,
            "total": page_result.get("total") or len(members),
            "total_page": page_result.get("total_page"),
            "page_size": page_result.get("page_size"),
            "hook_response": page_result,
        }
        if adapter_result["ok"]:
            cloud_jobs.mark_job_processed(state_dir, job_id=job_id, payload_hash=payload_hash, result=adapter_result)
            report_cloud_job_complete_for_lease(job_id, {"adapter_result": adapter_result}, lease_token)
        else:
            cloud_jobs.release_job_lock(state_dir, job_id=job_id)
            report_cloud_job_fail_for_lease(job_id, "hook_chatroom_member_sync_failed", adapter_result, lease_token)
        return adapter_result

    if action == "sync_contacts":
        requested_kind = str(payload.get("contact_kind") or payload.get("kind") or "all").strip()
        kinds = ["internal_member", "external_contact"] if requested_kind in {"", "all"} else [requested_kind]
        contacts: list[dict[str, Any]] = []
        hook_responses: dict[str, Any] = {}
        ok = True
        for kind in kinds:
            if kind == "internal_member":
                hook_type = 11036
            elif kind == "external_contact":
                hook_type = 11037
            else:
                cloud_jobs.release_job_lock(state_dir, job_id=job_id)
                result = {"ok": False, "status": "unsupported_contact_kind", "action": action, "contact_kind": kind}
                report_cloud_job_fail_for_lease(job_id, "unsupported_contact_kind", result, lease_token)
                return result
            page_result = fetch_hook_pages(
                hook_type=hook_type,
                request_data={},
                list_key="user_list",
                page_size=payload.get("page_size"),
                max_pages=payload.get("max_pages"),
            )
            items = page_result.pop("items", [])
            for item in items:
                enriched = dict(item)
                enriched.setdefault("contact_kind", kind)
                enriched.setdefault("source", "provider_sync")
                contacts.append(enriched)
            hook_responses[kind] = page_result
            ok = ok and bool(page_result.get("ok"))
        adapter_result = {
            "ok": ok,
            "action": action,
            "contacts": contacts,
            "total": len(contacts),
            "contact_kind": requested_kind or "all",
            "hook_response": hook_responses,
        }
        if adapter_result["ok"]:
            cloud_jobs.mark_job_processed(state_dir, job_id=job_id, payload_hash=payload_hash, result=adapter_result)
            report_cloud_job_complete_for_lease(job_id, {"adapter_result": adapter_result}, lease_token)
        else:
            cloud_jobs.release_job_lock(state_dir, job_id=job_id)
            report_cloud_job_fail_for_lease(job_id, "hook_contact_sync_failed", adapter_result, lease_token)
        return adapter_result

    if action == "download_inbound_media":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_type = str(payload.get("message_type") or data.get("message_type") or "file").strip() or "file"
        # 云端补偿任务由主服务在附件本地路径失效时下发；provider 本地拥有 Hook Runtime，
        # 可以重新按 CDN 元数据下载并上传回主服务。
        result = download_inbound_hook_media_if_needed(data, message_type=message_type)
        adapter_result = {
            "ok": not bool(result.get("download_error")),
            "action": action,
            "attachment_id": payload.get("attachment_id"),
            **result,
        }
        if adapter_result["ok"]:
            cloud_jobs.mark_job_processed(state_dir, job_id=job_id, payload_hash=payload_hash, result=adapter_result)
            report_cloud_job_complete_for_lease(job_id, {"adapter_result": adapter_result}, lease_token)
        else:
            cloud_jobs.release_job_lock(state_dir, job_id=job_id)
            report_cloud_job_fail_for_lease(job_id, "inbound_media_download_failed", adapter_result, lease_token)
        return adapter_result

    cloud_jobs.release_job_lock(state_dir, job_id=job_id)
    result = {"ok": False, "status": "unsupported_action", "action": action}
    report_cloud_job_fail_for_lease(job_id, "unsupported_cloud_control_action", result, lease_token)
    return result


def report_cloud_job_complete_for_lease(job_id: int, result: dict[str, Any], lease_token: str) -> dict[str, Any]:
    """新任务附加租约，旧任务保持原调用形态以兼容历史客户端测试和协议。"""

    if lease_token:
        return report_cloud_job_complete(job_id, result, lease_token=lease_token)
    return report_cloud_job_complete(job_id, result)


def report_cloud_job_fail_for_lease(
    job_id: int,
    error_code: str,
    adapter_result: dict[str, Any],
    lease_token: str,
) -> dict[str, Any]:
    """按任务是否携带租约选择节点或旧协议回报参数。"""

    if lease_token:
        return report_cloud_job_fail(job_id, error_code, adapter_result, lease_token=lease_token)
    return report_cloud_job_fail(job_id, error_code, adapter_result)


def report_cloud_job_complete(job_id: int, result: dict[str, Any], *, lease_token: str = "") -> dict[str, Any]:
    if uses_provider_node_protocol():
        if not config.device_token:
            return {"ok": False, "error": "missing_device_token"}
        return cloud_post_json(
            f"/api/provider/jobs/{int(job_id)}/complete",
            {**result, "lease_token": lease_token},
            device_token=config.device_token,
        )
    if not config.client_token:
        return {"ok": False, "error": "missing_client_token"}
    return cloud_post_json(f"/api/workwechat/provider/jobs/{int(job_id)}/complete", result, token=config.client_token)


def report_cloud_job_fail(
    job_id: int,
    error_code: str,
    adapter_result: dict[str, Any],
    *,
    lease_token: str = "",
) -> dict[str, Any]:
    if uses_provider_node_protocol():
        if not config.device_token:
            return {"ok": False, "error": "missing_device_token"}
        return cloud_post_json(
            f"/api/provider/jobs/{int(job_id)}/fail",
            {
                "lease_token": lease_token,
                "error_code": error_code,
                "error_message": str(adapter_result.get("error") or adapter_result.get("status") or error_code),
                "adapter_result": adapter_result,
            },
            device_token=config.device_token,
        )
    if not config.client_token:
        return {"ok": False, "error": "missing_client_token"}
    return cloud_post_json(
        f"/api/workwechat/provider/jobs/{int(job_id)}/fail",
        {
            "error_code": error_code,
            "error_message": str(adapter_result.get("error") or adapter_result.get("status") or error_code),
            "adapter_result": adapter_result,
        },
        token=config.client_token,
    )


def resolve_cloud_jobs_state_dir() -> Path:
    return APP_ROOT / "data" / "cloud_jobs"


def resolve_pending_delivery_path() -> Path:
    # 投递确认状态必须落在稳定 data 目录；provider 退出重启后仍可继续补报 delivered/timeout。
    return resolve_cloud_jobs_state_dir() / "pending_delivery.json"


def _load_pending_delivery_records() -> dict[str, dict[str, Any]]:
    path = resolve_pending_delivery_path()
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    records = parsed.get("records") if isinstance(parsed.get("records"), dict) else parsed
    return {str(key): value for key, value in records.items() if isinstance(value, dict)}


def _store_pending_delivery_records(records: dict[str, dict[str, Any]]) -> None:
    path = resolve_pending_delivery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    payload = {"version": 1, "records": records}
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temp_path.replace(path)
    cloud_status["pending_delivery_count"] = len(records)


def should_wait_for_delivery_confirmation(command: dict[str, Any]) -> bool:
    # 只有 Hook 无同步回执的发送类型才进入 pending delivery；查询类和有响应的发送类型仍按原逻辑 complete。
    if not config.delivery_confirmation_enabled:
        return False
    try:
        command_type = int(command.get("type") or 0)
    except (TypeError, ValueError):
        return False
    return command_type in DELIVERY_CONFIRMATION_COMMAND_TYPES


def build_pending_delivery_record(
    *,
    job_id: int,
    payload_hash: str,
    lease_token: str = "",
    provider_account_key: str = "",
    hook_api_base: str = "",
    command: dict[str, Any],
    accepted_response: dict[str, Any],
    adapter_result: dict[str, Any],
) -> dict[str, Any]:
    data = command.get("data") if isinstance(command.get("data"), dict) else {}
    command_type = int(command.get("type") or 0)
    file_path = pick_first(data.get("file"), data.get("file_path"), data.get("path"))
    now_epoch = time.time()
    timeout_seconds = max(10, int(config.delivery_confirmation_timeout_seconds or 180))
    return {
        "job_id": int(job_id),
        "payload_hash": payload_hash,
        # 租约和账号键必须随 pending 状态持久化；Provider 重启后补报 complete/fail
        # 仍要使用云端签发的原始租约，不能退回旧的无租约回报方式。
        "lease_token": lease_token,
        "provider_account_key": provider_account_key,
        "hook_api_base": hook_api_base,
        "command_type": command_type,
        "message_type": DELIVERY_CONFIRMATION_MESSAGE_TYPES.get(command_type, "message"),
        "conversation_external_id": str(data.get("conversation_id") or "").strip(),
        "content": str(data.get("content") or "").strip(),
        "file_name": _extract_delivery_file_name(file_path),
        "file_path": file_path,
        "accepted_at": now_epoch,
        "expires_at": now_epoch + timeout_seconds,
        "timeout_seconds": timeout_seconds,
        "accepted_response": accepted_response,
        "accepted_adapter_result": adapter_result,
        "last_report_error": "",
        "next_report_at": now_epoch,
    }


def register_pending_delivery_confirmation(record: dict[str, Any]) -> dict[str, Any]:
    # Hook 已接受但尚未看到群内自发回推时，保持云端 job running，避免 MBA 过早回调 message.sent。
    with delivery_confirmation_lock:
        records = _load_pending_delivery_records()
        records[str(int(record["job_id"]))] = record
        _store_pending_delivery_records(records)
    cloud_status["last_delivery_confirmation_at"] = _now_text()
    return {
        "pending": True,
        "job_id": int(record["job_id"]),
        "message_type": record.get("message_type"),
        "conversation_external_id": record.get("conversation_external_id"),
        "file_name": record.get("file_name"),
        "timeout_seconds": record.get("timeout_seconds"),
    }


def update_pending_delivery_acceptance(job_id: int, *, accepted_response: dict[str, Any], adapter_result: dict[str, Any]) -> dict[str, Any]:
    # pending 先于 Hook 调用写入以规避极速回推竞态；HTTP 返回后再补齐 Hook accepted 结果。
    with delivery_confirmation_lock:
        records = _load_pending_delivery_records()
        key = str(int(job_id))
        record = records.get(key)
        if not record:
            return {"updated": False, "reason": "pending_delivery_missing"}
        record["accepted_response"] = accepted_response
        record["accepted_adapter_result"] = adapter_result
        record["last_report_error"] = ""
        records[key] = record
        _store_pending_delivery_records(records)
    return {"updated": True, "job_id": int(job_id)}


def remove_pending_delivery_confirmation(job_id: int) -> None:
    # Hook 明确返回失败时不应继续等待自发回推，避免本地 pending 文件误导后续补偿。
    with delivery_confirmation_lock:
        records = _load_pending_delivery_records()
        records.pop(str(int(job_id)), None)
        _store_pending_delivery_records(records)


def confirm_pending_delivery_from_echo(normalized: dict[str, Any]) -> dict[str, Any]:
    # 自发消息回推是当前 Hook 能拿到的最强投递证明；匹配成功后才向 MBA complete。
    if not config.delivery_confirmation_enabled:
        return {"ok": True, "matched": False, "reason": "delivery_confirmation_disabled"}
    details = normalized.get("details") if isinstance(normalized.get("details"), dict) else {}
    if not bool(details.get("is_self_sent")):
        return {"ok": True, "matched": False, "reason": "not_self_sent"}

    with delivery_confirmation_lock:
        records = _load_pending_delivery_records()
        matched_key = _find_pending_delivery_match(records, normalized)
        if not matched_key:
            cloud_status["pending_delivery_count"] = len(records)
            return {"ok": True, "matched": False, "reason": "no_pending_delivery_match"}
        record = records[matched_key]
        record["matched_echo"] = _build_delivery_echo_summary(normalized)
        record["matched_at"] = time.time()
        result = _report_pending_delivery_completed(record)
        if response_ok(result):
            cloud_jobs.mark_job_processed(
                resolve_cloud_jobs_state_dir(),
                job_id=int(record["job_id"]),
                payload_hash=str(record.get("payload_hash") or ""),
                result=result.get("adapter_result") if isinstance(result.get("adapter_result"), dict) else result,
            )
            records.pop(matched_key, None)
            _store_pending_delivery_records(records)
            cloud_status["last_delivery_confirmation_at"] = _now_text()
            return {"ok": True, "matched": True, "job_id": int(record["job_id"]), "report": result}
        record["last_report_error"] = str(result.get("error") or result.get("detail") or result)
        record["next_report_at"] = time.time() + max(1, int(config.delivery_confirmation_retry_interval_seconds or 10))
        records[matched_key] = record
        _store_pending_delivery_records(records)
        return {"ok": False, "matched": True, "job_id": int(record["job_id"]), "report": result}


def flush_pending_delivery_confirmations() -> dict[str, Any]:
    # 周期性补偿两类情况：已匹配但 complete 上报失败、以及长时间没有自发回推的 accepted 任务。
    if not config.delivery_confirmation_enabled:
        return {"ok": True, "checked": 0, "completed": 0, "timed_out": 0}
    now_epoch = time.time()
    checked = completed = timed_out = retry_waiting = 0
    with delivery_confirmation_lock:
        records = _load_pending_delivery_records()
        for key, record in list(records.items()):
            checked += 1
            next_report_at = float(record.get("next_report_at") or 0)
            if next_report_at and now_epoch < next_report_at:
                retry_waiting += 1
                continue
            if isinstance(record.get("matched_echo"), dict):
                result = _report_pending_delivery_completed(record)
                if response_ok(result):
                    cloud_jobs.mark_job_processed(
                        resolve_cloud_jobs_state_dir(),
                        job_id=int(record["job_id"]),
                        payload_hash=str(record.get("payload_hash") or ""),
                        result=result.get("adapter_result") if isinstance(result.get("adapter_result"), dict) else result,
                    )
                    records.pop(key, None)
                    completed += 1
                else:
                    record["last_report_error"] = str(result.get("error") or result.get("detail") or result)
                    record["next_report_at"] = now_epoch + max(1, int(config.delivery_confirmation_retry_interval_seconds or 10))
                    records[key] = record
                continue
            expires_at = float(record.get("expires_at") or 0)
            if expires_at and now_epoch >= expires_at:
                result = _report_pending_delivery_timeout(record)
                if _cloud_job_report_accepted(result):
                    cloud_jobs.release_job_lock(resolve_cloud_jobs_state_dir(), job_id=int(record["job_id"]))
                    records.pop(key, None)
                    timed_out += 1
                else:
                    record["last_report_error"] = str(result.get("error") or result.get("detail") or result)
                    record["next_report_at"] = now_epoch + max(1, int(config.delivery_confirmation_retry_interval_seconds or 10))
                    records[key] = record
        _store_pending_delivery_records(records)
    if completed or timed_out:
        cloud_status["last_delivery_confirmation_at"] = _now_text()
    return {
        "ok": True,
        "checked": checked,
        "completed": completed,
        "timed_out": timed_out,
        "retry_waiting": retry_waiting,
        "pending": int(cloud_status.get("pending_delivery_count") or 0),
    }


def _report_pending_delivery_completed(record: dict[str, Any]) -> dict[str, Any]:
    echo = record.get("matched_echo") if isinstance(record.get("matched_echo"), dict) else {}
    accepted = record.get("accepted_adapter_result") if isinstance(record.get("accepted_adapter_result"), dict) else {}
    adapter_result = {
        **accepted,
        "ok": True,
        "status": "delivered",
        "external_status": "delivered",
        "external_message_id": str(echo.get("external_message_id") or "") or accepted.get("external_message_id"),
        "delivery_confirmation": {
            "confirmed": True,
            "confirmed_at": _now_text(),
            "echo": echo,
            "accepted_at": record.get("accepted_at"),
        },
    }
    result = report_cloud_job_complete_for_lease(
        int(record["job_id"]),
        {"adapter_result": adapter_result},
        str(record.get("lease_token") or ""),
    )
    if isinstance(result, dict):
        result.setdefault("adapter_result", adapter_result)
    return result


def _report_pending_delivery_timeout(record: dict[str, Any]) -> dict[str, Any]:
    accepted = record.get("accepted_adapter_result") if isinstance(record.get("accepted_adapter_result"), dict) else {}
    adapter_result = {
        **accepted,
        "ok": False,
        "status": "delivery_confirmation_timeout",
        "external_status": "accepted_unconfirmed",
        "error": "delivery_confirmation_timeout",
        "error_message": "Hook 已接受发送请求，但超时未收到当前账号自发消息回推",
        "delivery_confirmation": {
            "confirmed": False,
            "timeout_seconds": record.get("timeout_seconds"),
            "conversation_external_id": record.get("conversation_external_id"),
            "message_type": record.get("message_type"),
            "file_name": record.get("file_name"),
            "accepted_at": record.get("accepted_at"),
        },
    }
    return report_cloud_job_fail_for_lease(
        int(record["job_id"]),
        "delivery_confirmation_timeout",
        adapter_result,
        str(record.get("lease_token") or ""),
    )


def _cloud_job_report_accepted(result: dict[str, Any]) -> bool:
    # /complete 用 ok=true 表示上报成功；/fail 的 ok=false 表示任务失败结果本身，
    # 只要云端返回 job_id/status，就说明失败结果已经被 MBA 接收。
    if response_ok(result):
        return True
    return bool(result.get("job_id") and str(result.get("status") or "").strip())


def _build_delivery_echo_summary(normalized: dict[str, Any]) -> dict[str, Any]:
    conversation = normalized.get("conversation") if isinstance(normalized.get("conversation"), dict) else {}
    message = normalized.get("message") if isinstance(normalized.get("message"), dict) else {}
    details = normalized.get("details") if isinstance(normalized.get("details"), dict) else {}
    return {
        "event_key": normalized.get("event_key"),
        "conversation_external_id": conversation.get("external_id"),
        "message_type": message.get("message_type"),
        "content": message.get("content"),
        "external_message_id": message.get("external_message_id"),
        "file_name": details.get("file_name"),
        "file_size": details.get("file_size"),
        "timestamp": details.get("timestamp"),
    }


def _find_pending_delivery_match(records: dict[str, dict[str, Any]], normalized: dict[str, Any]) -> str:
    conversation = normalized.get("conversation") if isinstance(normalized.get("conversation"), dict) else {}
    message = normalized.get("message") if isinstance(normalized.get("message"), dict) else {}
    details = normalized.get("details") if isinstance(normalized.get("details"), dict) else {}
    echo_conversation_id = str(conversation.get("external_id") or "").strip()
    echo_message_type = _normalize_delivery_message_type(str(message.get("message_type") or ""))
    echo_content = _normalize_delivery_text(str(message.get("content") or ""))
    echo_file_name = _normalize_delivery_file_name(str(details.get("file_name") or ""))
    echo_account_key = str(details.get("self_user_id") or normalized.get("provider_account_key") or "").strip()
    exact_matches: list[tuple[float, str]] = []
    fallback_matches: list[tuple[float, str]] = []
    for key, record in records.items():
        record_account_key = str(record.get("provider_account_key") or "").strip()
        if record_account_key and echo_account_key and record_account_key != echo_account_key:
            continue
        if str(record.get("conversation_external_id") or "").strip() != echo_conversation_id:
            continue
        record_message_type = _normalize_delivery_message_type(str(record.get("message_type") or ""))
        if record_message_type != echo_message_type:
            continue
        accepted_at = float(record.get("accepted_at") or 0)
        if record_message_type == "text":
            if _normalize_delivery_text(str(record.get("content") or "")) == echo_content:
                exact_matches.append((accepted_at, key))
            continue
        record_file_name = _normalize_delivery_file_name(str(record.get("file_name") or ""))
        if record_file_name and (record_file_name == echo_file_name or record_file_name in echo_content):
            exact_matches.append((accepted_at, key))
        elif not record_file_name:
            # 少数 Hook 文件回推可能缺文件名；只有同群同类型唯一候选时才走弱匹配，避免误确认。
            fallback_matches.append((accepted_at, key))
    if exact_matches:
        return sorted(exact_matches)[0][1]
    if len(fallback_matches) == 1:
        return fallback_matches[0][1]
    return ""


def _normalize_delivery_message_type(message_type: str) -> str:
    normalized = str(message_type or "").strip().lower()
    if normalized in {"gif", "emoji"}:
        return "emoji"
    return normalized


def _normalize_delivery_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_delivery_file_name(value: str) -> str:
    return _extract_delivery_file_name(value).lower()


def _extract_delivery_file_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def build_cloud_ws_url() -> str:
    if not config.cloud_base_url:
        raise RuntimeError("missing_cloud_base_url")
    if not config.client_token:
        raise RuntimeError("missing_client_token")
    parsed = urlparse(config.cloud_base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base = f"{scheme}://{parsed.netloc}"
    path = "/api/workwechat/provider/ws"
    return f"{base}{path}?{urlencode({'client_token': config.client_token})}"


def handle_cloud_ws_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "").strip()
    cloud_status["last_ws_event_at"] = _now_text()
    if event_type == "job_available":
        result = poll_cloud_jobs_once()
        cloud_status["last_poll_at"] = _now_text()
        cloud_status["last_error"] = "" if result.get("ok") else str(result.get("error") or result)
        return result
    if event_type == "job":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
        result = process_cloud_job(payload)
        cloud_status["last_error"] = "" if result.get("ok") else str(result.get("error") or result.get("status") or result)
        return result
    if event_type in {"heartbeat", "status"}:
        return {"ok": True, "status": "ignored"}
    return {"ok": False, "status": "unsupported_event", "event_type": event_type}


def handle_cloud_ws_open() -> dict[str, Any]:
    cloud_status["ws_connected"] = True
    cloud_status["last_error"] = ""
    # WS 连接成功后立即补拉一次，覆盖服务端重启、网络抖动或 job_available 通知丢失时遗留的 pending 任务。
    result = poll_cloud_jobs_once()
    cloud_status["last_poll_at"] = _now_text()
    cloud_status["last_error"] = "" if result.get("ok") else str(result.get("error") or result)
    return result


def start_cloud_ws_if_needed() -> dict[str, Any]:
    global cloud_ws_thread, cloud_poll_fallback_thread
    cloud_status["enabled"] = bool(config.cloud_poll_enabled)
    if not config.cloud_poll_enabled:
        return {"started": False, "reason": "cloud_poll_disabled"}
    cloud_ws_stop_event.clear()
    cloud_status["authentication_stopped"] = False
    started = False
    if uses_provider_node_protocol():
        # 节点协议使用带超时的 HTTP 长轮询，不再为每个渠道建立 WebSocket；一个线程
        # 即可领取节点下全部已绑定账号任务，并按 provider_account_key 分派到本地 Hook。
        if not (cloud_poll_fallback_thread and cloud_poll_fallback_thread.is_alive()):
            cloud_poll_fallback_thread = threading.Thread(
                target=run_provider_node_poll_loop,
                name="workwechat-provider-node-poll",
                daemon=True,
            )
            cloud_poll_fallback_thread.start()
            started = True
        return {"started": started, "reason": "" if started else "already_running", "protocol": "provider_node"}
    if not (cloud_ws_thread and cloud_ws_thread.is_alive()):
        cloud_ws_thread = threading.Thread(target=run_cloud_ws_loop, name="workwechat-cloud-ws", daemon=True)
        cloud_ws_thread.start()
        started = True
    if not (cloud_poll_fallback_thread and cloud_poll_fallback_thread.is_alive()):
        cloud_poll_fallback_thread = threading.Thread(target=run_cloud_poll_fallback_loop, name="workwechat-cloud-poll-fallback", daemon=True)
        cloud_poll_fallback_thread.start()
        started = True
    return {"started": started, "reason": "" if started else "already_running"}


def stop_cloud_ws() -> None:
    cloud_ws_stop_event.set()


def compute_cloud_retry_delay(attempt: int, http_status: int = 0, *, random_value: float | None = None) -> float:
    """为限流、服务端错误和网络异常计算带抖动退避，避免大量节点同步重试。"""

    bounded_attempt = max(1, min(8, int(attempt or 1)))
    base = min(60.0, float(2 ** bounded_attempt))
    if int(http_status or 0) == 429:
        base = max(5.0, base)
    jitter_source = random.random() if random_value is None else max(0.0, min(1.0, float(random_value)))
    return round(base * (0.75 + jitter_source * 0.5), 3)


def run_provider_node_poll_loop() -> None:
    """持续执行节点激活、心跳和长轮询；401 停止，429/5xx 与网络错误退避。"""

    failure_count = 0
    next_heartbeat_at = 0.0
    while not cloud_ws_stop_event.is_set():
        try:
            now_epoch = time.time()
            if now_epoch >= next_heartbeat_at:
                heartbeat = send_provider_heartbeat_once()
                heartbeat_status = int(heartbeat.get("http_status") or 200)
                next_heartbeat_at = now_epoch + max(5, int(config.heartbeat_interval_seconds or 20))
                if heartbeat_status == 401:
                    cloud_status["authentication_stopped"] = True
                    cloud_status["last_error"] = "device_reactivation_required"
                    cloud_ws_stop_event.set()
                    break
            result = poll_cloud_jobs_once()
            cloud_status["last_poll_at"] = _now_text()
            http_status = int(result.get("http_status") or 200)
            if http_status == 401 or result.get("error") == "device_reactivation_required":
                cloud_status["authentication_stopped"] = True
                cloud_status["last_error"] = "device_reactivation_required"
                cloud_ws_stop_event.set()
                break
            if result.get("ok"):
                failure_count = 0
                cloud_status["last_error"] = ""
                interval = 20 if result.get("read_only") else 3
            else:
                failure_count += 1
                cloud_status["last_error"] = str(result.get("error") or result)
                interval = compute_cloud_retry_delay(failure_count, http_status)
            if cloud_ws_stop_event.wait(interval):
                break
        except Exception as exc:
            failure_count += 1
            cloud_status["last_error"] = str(exc)
            if cloud_ws_stop_event.wait(compute_cloud_retry_delay(failure_count)):
                break


def run_cloud_ws_loop() -> None:
    while not cloud_ws_stop_event.is_set():
        try:
            registration = register_cloud_client_if_needed()
            if not registration.get("ok"):
                cloud_status["last_error"] = str(registration.get("error") or registration)
                _sleep_with_stop(min(30, max(3, config.poll_interval_seconds)))
                continue
            if not config.client_token:
                cloud_status["last_error"] = "missing_client_token"
                _sleep_with_stop(min(30, max(3, config.poll_interval_seconds)))
                continue
            _run_cloud_ws_once()
            cloud_status["ws_connected"] = False
            # WebSocketApp.run_forever 在云端主动正常关闭连接时会直接返回；
            # 返回后也必须按轮询间隔退避，避免异常断线场景变成高速重连占用 CPU。
            _sleep_with_stop(min(30, max(3, config.poll_interval_seconds)))
        except Exception as exc:
            cloud_status["ws_connected"] = False
            cloud_status["last_error"] = str(exc)
            try:
                fallback = poll_cloud_jobs_once()
                cloud_status["last_poll_at"] = _now_text()
                if fallback.get("ok"):
                    cloud_status["last_error"] = ""
            except Exception as poll_exc:
                cloud_status["last_error"] = str(poll_exc)
            _sleep_with_stop(min(30, max(3, config.poll_interval_seconds)))


def run_cloud_poll_fallback_loop() -> None:
    while not cloud_ws_stop_event.is_set():
        interval = min(30, max(3, int(config.poll_interval_seconds or 3)))
        if cloud_ws_stop_event.wait(interval):
            break
        if not config.cloud_poll_enabled:
            continue
        try:
            # 兜底轮询不是实时通道，只负责把漏掉通知的 pending 任务重新拉起来。
            result = poll_cloud_jobs_once()
            cloud_status["last_poll_at"] = _now_text()
            if result.get("ok"):
                cloud_status["last_error"] = ""
            else:
                cloud_status["last_error"] = str(result.get("error") or result)
        except Exception as exc:
            cloud_status["last_error"] = str(exc)


def _run_cloud_ws_once() -> None:
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError("missing_websocket_client_dependency") from exc

    def _on_open(_ws):
        handle_cloud_ws_open()

    def _on_close(_ws, *_args):
        cloud_status["ws_connected"] = False

    def _on_error(_ws, error):
        cloud_status["ws_connected"] = False
        cloud_status["last_error"] = str(error)

    def _on_message(_ws, message):
        try:
            parsed = json.loads(str(message or "{}"))
        except json.JSONDecodeError:
            parsed = {"event_type": "invalid", "raw": str(message or "")}
        if isinstance(parsed, dict):
            handle_cloud_ws_event(parsed)

    ws = websocket.WebSocketApp(
        build_cloud_ws_url(),
        on_open=_on_open,
        on_close=_on_close,
        on_error=_on_error,
        on_message=_on_message,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


def _sleep_with_stop(seconds: int) -> None:
    cloud_ws_stop_event.wait(max(1, int(seconds or 1)))


def _now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_response_body(raw: str, status: int, url: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"text": raw}
    if not isinstance(parsed, dict):
        parsed = {"data": parsed}
    parsed.setdefault("http_status", status)
    parsed.setdefault("url", url)
    return parsed


def response_ok(payload: dict[str, Any]) -> bool:
    if int(payload.get("http_status") or 200) >= 400:
        return False
    if "success" in payload:
        return bool(payload.get("success"))
    if "ok" in payload:
        return bool(payload.get("ok"))
    if str(payload.get("error") or "").strip():
        return False
    return True


def build_hook_command(body: dict[str, Any]) -> dict[str, Any]:
    message_type = str(body.get("message_type") or (body.get("payload") or {}).get("message_type") or "text").lower()
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    conversation_id = str(
        body.get("receiver_id")
        or body.get("conversation_external_id")
        or body.get("external_id")
        or payload.get("receiver_id")
        or payload.get("external_id")
        or ""
    ).strip()
    if not conversation_id:
        raise ValueError("missing_conversation_id")

    # Hook 文档中发送消息统一是 type + data。
    if message_type == "text":
        content = str(body.get("content") or payload.get("content") or "").strip()
        if not content:
            raise ValueError("missing_content")
        return {"type": 11029, "data": {"conversation_id": conversation_id, "content": content}}

    if message_type == "image":
        path = resolve_outbound_local_file_path(
            body,
            payload,
            path_keys=("image_path", "file_path", "local_path", "path"),
            url_keys=("image_url", "file_url", "download_url", "url"),
        )
        if not path:
            raise ValueError("missing_image_path")
        return {"type": 11030, "data": {"conversation_id": conversation_id, "file_path": path}}

    if message_type == "file":
        path = resolve_outbound_local_file_path(
            body,
            payload,
            path_keys=("file_path", "local_path", "path"),
            url_keys=("file_url", "download_url", "url"),
        )
        if not path:
            raise ValueError("missing_file_path")
        # Hook API 文档里 11031 文件消息字段名是 file，不是 file_path。
        # 11031 属于“无返回发送类型”，字段错时 Hook runtime 也会返回成功，导致服务端误记 sent。
        return {"type": 11031, "data": {"conversation_id": conversation_id, "file": path}}

    if message_type in {"link_card", "card"}:
        data = {
            "conversation_id": conversation_id,
            "url": pick_first(body.get("url"), payload.get("url")),
            "image_url": pick_first(body.get("image_url"), payload.get("image_url")),
            "title": pick_first(body.get("title"), payload.get("title")),
            "desc": pick_first(body.get("desc"), payload.get("desc")),
        }
        if not data["url"] or not data["title"]:
            raise ValueError("missing_link_card_fields")
        return {"type": 11159, "data": data}

    if message_type in {"mini_program", "miniprogram"}:
        data = {
            "conversation_id": conversation_id,
            "username": pick_first(body.get("username"), payload.get("username")),
            "appid": pick_first(body.get("appid"), payload.get("appid")),
            "appname": pick_first(body.get("appname"), payload.get("appname")),
            "appicon": pick_first(body.get("appicon"), payload.get("appicon")),
            "title": pick_first(body.get("title"), payload.get("title")),
            "page_path": pick_first(body.get("page_path"), body.get("pagepath"), payload.get("page_path"), payload.get("pagepath")),
            "file_id": pick_first(body.get("file_id"), payload.get("file_id")),
            "aes_key": pick_first(body.get("aes_key"), payload.get("aes_key")),
            "md5": pick_first(body.get("md5"), payload.get("md5")),
            "size": pick_first(body.get("size"), payload.get("size")),
        }
        missing = [key for key, value in data.items() if key != "conversation_id" and value in (None, "")]
        if missing:
            raise ValueError(f"missing_mini_program_fields:{','.join(missing)}")
        return {"type": 11162, "data": data}

    raise ValueError(f"unsupported_message_type:{message_type}")


def resolve_inbound_file_cache_dir() -> Path:
    cache_dir = Path(config.inbound_file_cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = APP_ROOT / cache_dir
    return cache_dir


def build_main_service_upload_url() -> str:
    if config.cloud_poll_enabled and config.cloud_base_url and config.device_token:
        return f"{config.cloud_base_url.rstrip('/')}/api/provider/inbound-upload"
    if config.cloud_poll_enabled and config.cloud_base_url and config.client_token:
        # 云端模式下文件上传也走 provider 专用 client_token 入口，不再反推传统 callback_url。
        return f"{config.cloud_base_url.rstrip('/')}/api/workwechat/provider/inbound-upload"

    # provider 通过 callback_url 反推出主服务所在的基址，再把回调路径替换成文件上传路径。
    # 这样不需要额外再配一套主服务地址，沿用已有回调配置即可。
    callback_url = str(config.callback_url or "").strip()
    if not callback_url:
        return ""
    parsed = urlparse(callback_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path or ""
    if path.endswith("/inbound"):
        path = f"{path[:-len('/inbound')]}/inbound-upload"
    else:
        path = f"{path.rstrip('/')}/inbound-upload"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def resolve_outbound_local_file_path(
    body: dict[str, Any],
    payload: dict[str, Any],
    *,
    path_keys: tuple[str, ...],
    url_keys: tuple[str, ...],
) -> str:
    # 企微底层 Hook 只能接收本机文件路径。主服务和插件更适合传公开下载 URL，
    # 因此 provider 在发送前负责把 URL 下载到本机缓存目录，再把本地路径交给 Hook。
    direct_path = pick_string(payload, *path_keys) or pick_string(body, *path_keys)
    if direct_path:
        return direct_path

    source_url = pick_string(payload, *url_keys) or pick_string(body, *url_keys)
    if not source_url:
        return ""
    return download_outbound_file_to_cache(
        source_url,
        file_name=pick_string(payload, "file_name", "name") or pick_string(body, "file_name", "name"),
    )


def extract_hook_file_name(data: dict[str, Any]) -> str:
    # 企微文件消息的真实名称常常藏在 cdn / c2c_cdn 里，顶层 file_name 可能为空。
    # 这里统一从顶层和嵌套 CDN 结构里兜底提取，避免附件内容和下载缓存都丢掉文件名。
    cdn = data.get("cdn") if isinstance(data.get("cdn"), dict) else {}
    c2c_cdn = data.get("c2c_cdn") if isinstance(data.get("c2c_cdn"), dict) else {}
    return (
        pick_string(data, "file_name", "filename")
        or pick_string(cdn, "file_name", "filename")
        or pick_string(c2c_cdn, "file_name", "filename")
        or ""
    )


def extract_hook_file_size(data: dict[str, Any]) -> int | None:
    cdn = data.get("cdn") if isinstance(data.get("cdn"), dict) else {}
    c2c_cdn = data.get("c2c_cdn") if isinstance(data.get("c2c_cdn"), dict) else {}
    return pick_int(
        data,
        "file_size",
        "size",
    ) or pick_int(cdn, "file_size", "size") or pick_int(c2c_cdn, "file_size", "size")


def download_inbound_hook_media_if_needed(data: dict[str, Any], *, message_type: str) -> dict[str, Any]:
    # 文件消息的回调只是“通知”，真正可交给业务系统处理的，是落到本地缓存后的文件路径。
    # 这里先在 provider 侧完成下载，再把内容上传回主服务，主服务才能统一落库和给插件消费。
    cdn = data.get("cdn") if isinstance(data.get("cdn"), dict) else {}
    c2c_cdn = data.get("c2c_cdn") if isinstance(data.get("c2c_cdn"), dict) else {}
    source = dict(cdn or c2c_cdn)
    if not source:
        return {"download_error": "missing_cdn_payload"}
    if pick_int(source, "cdn_type", "type") is None:
        top_level_cdn_type = pick_int(data, "cdn_type")
        if top_level_cdn_type is not None:
            # 现场企微文件回调会把 cdn_type 放在 data 顶层，而下载参数放在 data.cdn 内。
            # 下载命令仍然需要类型字段参与分支判断，所以这里先补齐到源对象副本里。
            source["cdn_type"] = top_level_cdn_type

    if str(data.get("download_path") or data.get("save_path") or "").strip():
        # 某些环境里底层已经返回了落盘路径，这里不重复下载。
        existing_path = str(data.get("download_path") or data.get("save_path") or "").strip()
        upload_result = upload_inbound_file_to_main_service(
            existing_path,
            file_name=extract_hook_file_name(data),
            message_type=message_type,
            source_event_key=pick_string(data, "server_id", "local_id"),
        )
        if upload_result.get("download_error"):
            return upload_result
        return upload_result

    file_name = extract_hook_file_name(data)
    save_path = build_inbound_cache_file_path(file_name=file_name)

    command = build_inbound_cdn_download_command(source, save_path=save_path, message_type=message_type, file_name=file_name)
    if not command:
        return {"download_error": "unsupported_cdn_payload"}
    if isinstance(command.get("data"), dict):
        command["data"]["timeout_seconds"] = max(config.request_timeout_seconds, config.inbound_file_download_timeout_seconds)

    response = download_inbound_file_via_hook(command)
    if response.get("download_error"):
        return response

    if not response_ok(response):
        return {
            "download_error": str(response.get("error") or response.get("message") or response.get("text") or "download_inbound_file_failed"),
        }

    response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
    resolved_path = str(
        response_data.get("save_path")
        or response_data.get("file_path")
        or response.get("save_path")
        or response.get("file_path")
        or save_path
    ).strip()
    if not resolved_path:
        return {
            "download_error": "download_inbound_file_missing_save_path",
        }
    if not wait_for_inbound_download_file(resolved_path):
        return {
            "download_error": f"download_inbound_file_not_found: {resolved_path}",
        }
    return upload_inbound_file_to_main_service(
        resolved_path,
        file_name=file_name,
        message_type=message_type,
        source_event_key=pick_string(data, "server_id", "local_id"),
    )


def wait_for_inbound_download_file(path: str, *, timeout_seconds: float = 2.0) -> bool:
    """等待 Hook Runtime 把入站附件写入本地缓存。

    企微 CDN 下载接口有时会先返回响应，文件随后才完成落盘；这里做短轮询，
    避免刚收到响应就检查路径导致误报 `download_inbound_file_not_found`。
    """

    target = Path(path)
    deadline = time.time() + max(0.1, float(timeout_seconds or 0.1))
    while time.time() <= deadline:
        if target.exists() and target.is_file():
            return True
        time.sleep(0.1)
    return target.exists() and target.is_file()


def download_inbound_file_via_hook(command: dict[str, Any]) -> dict[str, Any]:
    timeout = max(config.request_timeout_seconds, config.inbound_file_download_timeout_seconds)
    attempts = max(1, int(config.inbound_file_download_retries or 0) + 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = post_json(build_hook_url(), command, timeout=timeout)
        except Exception as exc:
            last_error = str(exc)
            if attempt >= attempts or not is_transient_download_error(last_error):
                return {"download_error": f"download_inbound_file_failed: {last_error}"}
            time.sleep(min(2, attempt))
            continue
        if response_ok(response) or not is_transient_download_error(response):
            return response
        last_error = str(response.get("error") or response.get("message") or response.get("text") or "download_inbound_file_failed")
        if attempt < attempts:
            time.sleep(min(2, attempt))
    return {"download_error": last_error or "download_inbound_file_failed"}


def is_transient_download_error(value: Any) -> bool:
    if isinstance(value, dict):
        text = " ".join(str(value.get(key) or "") for key in ("error", "message", "text", "download_error"))
    else:
        text = str(value or "")
    normalized = text.lower()
    return any(marker in normalized for marker in ("timeout", "timed out", "temporarily", "connection reset", "connection aborted"))


def upload_inbound_file_to_main_service(
    source_path: str,
    *,
    file_name: str,
    message_type: str,
    source_event_key: str = "",
) -> dict[str, Any]:
    source_file = Path(source_path)
    if not source_file.exists() or not source_file.is_file():
        return {"download_error": f"inbound_file_source_missing: {source_path}"}

    upload_url = build_main_service_upload_url()
    if not upload_url:
        return {"download_error": "missing_callback_url"}
    upload_headers: dict[str, str] = {}
    if config.cloud_poll_enabled and config.device_token:
        upload_headers["X-Provider-Device-Token"] = config.device_token
    elif config.cloud_poll_enabled and config.client_token:
        # 云端上传使用注册后保存的 client_token，和消息入站、任务领取保持同一套鉴权。
        upload_headers["X-WorkWechat-Client-Token"] = config.client_token
    elif config.provider_token:
        upload_headers["X-Provider-Token"] = config.provider_token
    else:
        return {"download_error": "missing_upload_token"}

    try:
        content = source_file.read_bytes()
    except OSError as exc:
        return {"download_error": f"inbound_file_read_failed: {exc}"}
    if not content:
        return {"download_error": "inbound_file_empty"}

    payload = {
        "provider_account_key": current_local_account_key(),
        "message_type": message_type,
        "file_name": file_name or source_file.name,
        "content_type": "application/octet-stream",
        "source_path": str(source_file),
        "source_event_key": source_event_key or None,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    try:
        response = post_json(
            upload_url,
            payload,
            timeout=config.request_timeout_seconds,
            headers=upload_headers,
        )
    except Exception as exc:
        return {"download_error": f"inbound_file_upload_failed: {exc}"}
    if not response_ok(response):
        return {
            "download_error": str(response.get("error") or response.get("message") or response.get("text") or "inbound_file_upload_failed"),
        }

    response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
    storage_path = str(
        response_data.get("storage_path")
        or response_data.get("download_path")
        or response_data.get("file_path")
        or response.get("storage_path")
        or response.get("download_path")
        or response.get("file_path")
        or ""
    ).strip()
    if not storage_path:
        return {"download_error": "inbound_file_upload_missing_storage_path"}
    return {
        "download_path": storage_path,
        "file_path": storage_path,
        # 主服务入站上传接口当前直接把字段放在响应顶层；旧 mock/兼容接口可能放在 data 内。
        # 这里两种结构都读取，避免文件已上传成功却被 provider 误判为失败。
        "download_url": response_data.get("download_url") or response.get("download_url"),
        "file_size": response_data.get("file_size") or response.get("file_size") or len(content),
        "mime_type": response_data.get("content_type") or response.get("content_type") or "application/octet-stream",
        "source_path": str(source_file),
    }


def build_inbound_cache_file_path(*, file_name: str) -> str:
    cache_dir = resolve_inbound_file_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(file_name or "upload.bin")
    stamp = int(time.time() * 1000)
    return str(cache_dir / f"{stamp}-{safe_name}")


def build_inbound_cdn_download_command(source: dict[str, Any], *, save_path: str, message_type: str = "", file_name: str = "") -> dict[str, Any]:
    # hook 文档把 CDN 下载分成 WxCDN 和 C2cCDN 两种，这里按字段自动选命令。
    cdn_type = pick_int(source, "cdn_type") or pick_int(source, "type")
    if cdn_type == 1 and pick_string(source, "url") and pick_string(source, "auth_key") and pick_string(source, "aes_key"):
        return {
            "type": 11171,
            "data": {
                "url": pick_string(source, "url"),
                "auth_key": pick_string(source, "auth_key"),
                "aes_key": pick_string(source, "aes_key"),
                "size": pick_int(source, "size"),
                "save_path": save_path,
            },
        }
    if cdn_type == 2 and pick_string(source, "file_id") and pick_string(source, "aes_key"):
        payload: dict[str, Any] = {
            "aes_key": pick_string(source, "aes_key"),
            "file_id": pick_string(source, "file_id"),
            "save_path": save_path,
        }
        file_size = pick_int(source, "file_size", "size")
        if file_size is not None:
            payload["file_size"] = file_size
        file_type = pick_int(source, "file_type")
        if file_type is not None:
            payload["file_type"] = file_type
        else:
            payload["file_type"] = infer_c2c_cdn_file_type(message_type=message_type, file_name=file_name)
        return {"type": 11170, "data": payload}
    return {}


def infer_c2c_cdn_file_type(*, message_type: str = "", file_name: str = "") -> int:
    """推断 C2cCDN 下载接口需要的 file_type。

    Hook 文档中 11170 的 file_type 含义是“要下载的资源类型”，不是消息 content_type。
    第三方机器人发到群里的 Excel 文件通常只有 file_id/aes_key，没有显式 file_type；
    如果继续按原图 1 下载，Hook 可能返回成功但文件不会落盘，所以文件消息必须按 5 处理。
    """

    normalized_message_type = str(message_type or "").strip().lower()
    suffix = Path(str(file_name or "").strip()).suffix.lower()
    if normalized_message_type == "video" or suffix in {".mp4", ".mov", ".avi", ".mkv", ".wmv"}:
        return 4
    if normalized_message_type == "image" or suffix in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}:
        return 1
    if normalized_message_type == "file" or suffix:
        return 5
    return 1


def download_outbound_file_to_cache(source_url: str, *, file_name: str = "") -> str:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_file_url_scheme")

    cache_dir = resolve_outbound_file_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(file_name or infer_file_name_from_url(source_url))
    # 文件发送时企微会读取本机路径的 basename 作为附件名；如果把时间戳拼到文件名前面，
    # 群里看到的附件名也会带前缀。这里改成唯一子目录承载原始文件名，既避免覆盖又保留业务文件名。
    cache_key = f"{int(time.time() * 1000)}-{hashlib.sha1(source_url.encode('utf-8')).hexdigest()[:10]}"
    target_dir = cache_dir / cache_key
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name

    req = urlrequest.Request(source_url, method="GET")
    req.add_header("User-Agent", "workwechat-provider/0.1")
    log_debug_event(
        "downloading outbound file before hook send",
        {
            "source_url": source_url,
            "target_path": str(target_path),
            "file_name": safe_name,
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=config.request_timeout_seconds) as response:
            content = response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"download_outbound_file_failed: {exc}") from exc
    if not content:
        raise ValueError("downloaded_file_empty")
    target_path.write_bytes(content)
    return str(target_path)


def resolve_outbound_file_cache_dir() -> Path:
    cache_dir = Path(config.outbound_file_cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = APP_ROOT / cache_dir
    return cache_dir


def infer_file_name_from_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    tail = Path(unquote(parsed.path or "")).name
    return tail or "upload.bin"


def sanitize_filename(value: str) -> str:
    normalized = str(value or "").strip() or "upload.bin"
    # Windows 下企业微信运行环境通常不接受这些保留字符；这里统一替换，避免下载缓存落盘失败。
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", normalized)
    cleaned = cleaned.replace("..", "_").strip(" .")
    return cleaned or "upload.bin"


def fetch_hook_pages(
    *,
    hook_type: int,
    request_data: dict[str, Any],
    list_key: str,
    page_size: int | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    # 企微 Hook 的群/成员接口都使用 page_num + page_size，这里集中分页，避免列表和成员同步各写一套。
    resolved_page_size = _bounded_positive_int(page_size, config.chatroom_sync_page_size, 1, 500)
    resolved_max_pages = _bounded_positive_int(max_pages, config.chatroom_sync_max_pages, 1, 200)
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    total = 0
    total_page = 1
    for page_num in range(1, resolved_max_pages + 1):
        body = {
            "type": hook_type,
            "data": {
                **request_data,
                "page_num": page_num,
                "page_size": resolved_page_size,
            },
        }
        response = post_json(build_hook_url(), body, timeout=config.request_timeout_seconds)
        pages.append(response)
        if not response_ok(response):
            return {
                "ok": False,
                "error": response.get("error") or response.get("error_message") or response.get("message") or "hook_response_failed",
                "items": items,
                "pages": pages,
            }
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        if _coerce_positive_int(data.get("error_code"), 0) > 0:
            return {
                "ok": False,
                "error": data.get("error_message") or data.get("message") or "hook_data_error",
                "items": items,
                "pages": pages,
            }
        raw_items = data.get(list_key) if isinstance(data.get(list_key), list) else []
        items.extend(item for item in raw_items if isinstance(item, dict))
        total = _coerce_positive_int(data.get("total"), total)
        total_page = _coerce_positive_int(data.get("total_page"), total_page)
        if page_num >= total_page:
            break
        if not raw_items and total <= len(items):
            break
    return {
        "ok": True,
        "items": items,
        "total": total if total else len(items),
        "total_page": total_page,
        "page_size": resolved_page_size,
        "pages": pages,
    }


def _bounded_positive_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(minimum, min(parsed, maximum))


def _coerce_positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def pick_string(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def pick_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def normalize_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    hook_type = coerce_int(payload.get("type"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    callback_account_token = active_provider_account_key.set(str(data.get("self_user_id") or "").strip())
    contact_event_type = {
        11063: "contact_request",
        11076: "contact_added",
        11077: "contact_deleted",
    }.get(hook_type)
    message_type = {
        11041: "text",
        11042: "image",
        11045: "file",
        11043: "video",
        11044: "voice",
        11046: "location",
        11047: "link",
        11048: "emoji",
        11068: "rich_text",
    }.get(hook_type, "event")

    conversation_id = pick_first(
        data.get("conversation_id"),
        data.get("room_conversation_id"),
        data.get("room_id"),
        payload.get("conversation_id"),
        payload.get("chat_id"),
    ) or "manual"
    content = build_message_content(data, message_type)
    sender_id = pick_first(data.get("sender"), data.get("sender_id"), data.get("op_user_id"))
    sender_name = pick_first(data.get("sender_name"), data.get("op_user_name"), data.get("nickname"))
    timestamp = data.get("send_time") or data.get("timestamp") or payload.get("timestamp")
    external_message_id = pick_first(data.get("server_id"), data.get("local_id"), data.get("message_server_id"))
    event_key = pick_first(payload.get("event_key"), external_message_id) or build_event_key(
        hook_type,
        conversation_id,
        sender_id,
        timestamp,
        content,
    )

    download_meta = {}
    try:
        if message_type in {"image", "file", "video", "voice"}:
            download_meta = download_inbound_hook_media_if_needed(data, message_type=message_type)
    finally:
        # 文件下载和上传可能需要账号键；归一化完成后立即恢复上下文，避免线程池复用时串账号。
        active_provider_account_key.reset(callback_account_token)
    is_self_sent = bool(data.get("is_self_sent"))

    # 统一结构直接交给主服务 workwechat adapter；主服务仍会再做一次容错归一化。
    normalized = {
        "event_key": event_key,
        "event_type": contact_event_type or "message",
        "conversation": {
            "external_id": conversation_id,
            "title": pick_first(data.get("room_name"), data.get("conversation_title"), data.get("title")),
            "chat_type": "group" if str(conversation_id).startswith("R:") or data.get("room_conversation_id") else "direct",
        },
        "message": {
            "content": content or (f"[{contact_event_type}]" if contact_event_type else ""),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_type": contact_event_type or message_type,
            "external_message_id": external_message_id,
        },
        "details": {
            "source_protocol": "workwechat_hook",
            "hook_type": hook_type,
            "is_self_sent": is_self_sent,
            "self_user_id": data.get("self_user_id"),
            # 自发消息只做云端留痕，不触发入站插件，避免 provider 出站消息被 Hook 回流成新任务。
            "skip_plugin_dispatch": is_self_sent,
            "direction": "out" if is_self_sent else "in",
            "timestamp": timestamp,
            "file_name": extract_hook_file_name(data),
            "file_size": extract_hook_file_size(data),
            "file_path": data.get("file_path") or data.get("download_path") or data.get("save_path"),
            "download_path": download_meta.get("download_path") or data.get("download_path") or data.get("save_path"),
            "download_url": download_meta.get("download_url"),
            "source_path": download_meta.get("source_path"),
            "download_error": download_meta.get("download_error") or data.get("download_error"),
            "cdn": data.get("cdn") or data.get("c2c_cdn"),
            "contact_event_type": contact_event_type,
            "raw_data": data,
        },
    }
    # Hook runtime 会把当前登录账号写入 self_user_id；节点协议用它把入站事件路由到
    # 云端已绑定渠道。字段为空时由 push_callback 再做一次本地状态探测。
    normalized["provider_account_key"] = str(data.get("self_user_id") or "").strip()
    return normalized


def build_message_content(data: dict[str, Any], message_type: str) -> str:
    text = pick_first(data.get("content"), data.get("text_content"), data.get("desc"), data.get("title"))
    if text:
        return text
    if message_type in {"image", "file", "video", "voice"}:
        file_name = extract_hook_file_name(data)
        return f"[{message_type}] {file_name}" if file_name else f"[{message_type}]"
    return ""


def pick_first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def build_event_key(hook_type: int | None, conversation_id: str, sender_id: str, timestamp: Any, content: str) -> str:
    raw = "|".join([str(hook_type or ""), conversation_id, sender_id, str(timestamp or ""), content])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sign_callback(body: bytes, timestamp: str) -> str:
    if not config.callback_secret:
        return ""
    message = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(config.callback_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def push_callback(payload: dict[str, Any]) -> dict[str, Any]:
    if config.cloud_poll_enabled and config.cloud_base_url and config.device_token:
        node_payload = dict(payload)
        node_payload["provider_account_key"] = str(
            node_payload.get("provider_account_key") or current_local_account_key()
        ).strip()
        if not node_payload["provider_account_key"]:
            return {"ok": False, "error": "provider_account_not_uniquely_available"}
        response = cloud_post_json(
            "/api/provider/inbound",
            node_payload,
            device_token=config.device_token,
        )
        log_debug_event("provider node inbound response from main service", response)
        return response
    if config.cloud_poll_enabled and config.cloud_base_url and config.client_token:
        # 云端 WS 负责唤醒和任务通知；入站消息仍用短 HTTP 请求上报，但鉴权统一使用 client_token。
        log_debug_event(
            "cloud inbound request to main service",
            {
                "url": f"{config.cloud_base_url.rstrip('/')}/api/workwechat/provider/inbound",
                "timeout": config.request_timeout_seconds,
                "headers": mask_sensitive_headers({"X-WorkWechat-Client-Token": config.client_token}),
                "body": payload,
            },
        )
        response = cloud_post_json("/api/workwechat/provider/inbound", payload, token=config.client_token)
        log_debug_event("cloud inbound response from main service", response)
        return response

    if not config.callback_url:
        return {"ok": False, "error": "missing_callback_url"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    req = urlrequest.Request(config.callback_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-Timestamp", timestamp)
    signature = sign_callback(body, timestamp)
    if signature:
        req.add_header("X-Signature", signature)
    log_debug_event(
        "outbound callback request to main service",
        {
            "url": config.callback_url,
            "timeout": config.callback_timeout_seconds,
            "headers": mask_sensitive_headers(req.headers),
            "body": payload,
        },
    )
    with urlrequest.urlopen(req, timeout=config.callback_timeout_seconds) as response:
        raw = response.read().decode("utf-8", errors="replace")
        parsed = parse_response_body(raw, getattr(response, "status", 200), getattr(response, "url", config.callback_url))
        log_debug_event("outbound callback response from main service", parsed)
        return parsed


def push_callback_background(payload: dict[str, Any]) -> None:
    """后台上报 Hook 入站消息，避免阻塞 Hook runtime 的 HTTP 回调。

    Hook runtime 推送到 8181 的超时较短；云端入站接口偶发需要数秒到十几秒。
    如果在 `/hook-callback` 请求里同步等待云端响应，Hook 会误判 8181 异常。
    因此这里在独立线程池中执行真实上报，只把结果写入日志。
    """
    delivery_confirmation = {"ok": True, "matched": False, "reason": "not_checked"}
    try:
        # 真实投递确认也放到后台线程执行，避免 complete/fail 回云端时拖慢 8181 的 Hook 回调响应。
        delivery_confirmation = confirm_pending_delivery_from_echo(payload)
    except Exception as exc:
        delivery_confirmation = {"ok": False, "matched": False, "error": str(exc)}
        log_debug_event("hook callback delivery confirmation exception", {"error": str(exc), "normalized": payload})
    try:
        callback_response = push_callback(payload)
        log_debug_event(
            "provider hook-callback background response",
            {
                "ok": response_ok(callback_response),
                "callback_response": callback_response,
                "delivery_confirmation": delivery_confirmation,
                "normalized": payload,
            },
        )
    except Exception as exc:
        log_debug_event(
            "hook callback background push exception",
            {"error": str(exc), "delivery_confirmation": delivery_confirmation, "normalized": payload},
        )


def submit_inbound_callback(payload: dict[str, Any]) -> dict[str, Any]:
    """把入站消息提交到独立线程池，确保 Hook 回调请求可以立即返回。

    不能依赖 `/hook-callback` 同步等待云端入站接口：现场云端接口可能耗时十几秒，
    Hook runtime 对 8181 的等待只有几秒。这里先记录排队状态，再由线程池完成真实上报。
    """
    queued_payload = copy.deepcopy(payload)
    with inbound_callback_lock:
        inbound_callback_status["submitted"] = int(inbound_callback_status.get("submitted") or 0) + 1
        inbound_callback_status["pending"] = int(inbound_callback_status.get("pending") or 0) + 1
        inbound_callback_status["last_submitted_at"] = _now_text()
        inbound_callback_status["last_error"] = ""
        sequence = int(inbound_callback_status["submitted"])
        pending = int(inbound_callback_status["pending"])
    future = inbound_callback_executor.submit(push_callback_background, queued_payload)
    future.add_done_callback(lambda done: mark_inbound_callback_done(done, sequence=sequence))
    return {"sequence": sequence, "pending": pending}


def mark_inbound_callback_done(done: Future, *, sequence: int) -> None:
    """记录后台入站上报任务完成状态，供 /health 排查是否存在积压或失败。"""
    error = ""
    try:
        done.result()
    except Exception as exc:
        error = str(exc)
    with inbound_callback_lock:
        inbound_callback_status["pending"] = max(0, int(inbound_callback_status.get("pending") or 0) - 1)
        if error:
            inbound_callback_status["failed"] = int(inbound_callback_status.get("failed") or 0) + 1
            inbound_callback_status["last_failed_at"] = _now_text()
            inbound_callback_status["last_error"] = error
        else:
            inbound_callback_status["completed"] = int(inbound_callback_status.get("completed") or 0) + 1
            inbound_callback_status["last_completed_at"] = _now_text()
            inbound_callback_status["last_error"] = ""
    if error:
        log_debug_event("provider hook-callback background task failed", {"sequence": sequence, "error": error})


def inbound_callback_status_snapshot() -> dict[str, Any]:
    """返回入站后台上报队列状态快照，避免调用方读到正在修改的 dict。"""
    with inbound_callback_lock:
        return dict(inbound_callback_status)


def hook_health_status_snapshot() -> dict[str, Any]:
    """返回 Hook 健康探测缓存快照，过滤掉仅供内部 TTL 判断的字段。"""
    with hook_health_lock:
        return {key: copy.deepcopy(value) for key, value in hook_health_status.items() if not key.startswith("_")}


def submit_hook_health_probe_if_needed(*, force: bool = False) -> dict[str, Any]:
    """按 TTL 触发一次后台 Hook 健康探测，不阻塞 /health 请求。

    发送文件时 Hook runtime 可能正在处理 11031 文件消息；如果 /health 同步探测 8989，
    UI 会把 Hook 忙碌误报成 8181 异常。这里改成后台刷新缓存，让健康接口始终快速返回。
    """
    now_epoch = time.time()
    with hook_health_lock:
        if bool(hook_health_status.get("refreshing")):
            return {"submitted": False, "reason": "already_refreshing"}
        last_checked = float(hook_health_status.get("_last_checked_epoch") or 0.0)
        ttl = max(1, int(config.health_probe_cache_ttl_seconds or 10))
        if not force and last_checked and now_epoch - last_checked < ttl:
            return {"submitted": False, "reason": "cache_fresh"}
        hook_health_status["refreshing"] = True
        hook_health_status["last_started_at"] = _now_text()
        hook_health_status["last_error"] = ""
    future = hook_health_executor.submit(run_hook_health_probe)
    future.add_done_callback(mark_hook_health_probe_done)
    return {"submitted": True, "reason": "scheduled"}


def run_hook_health_probe() -> dict[str, Any]:
    """在后台线程执行真实 Hook 探测，保留原 /health 的探测语义。"""
    started = time.time()
    hook_status: dict[str, Any] = {"ok": None, "reason": "probe_disabled"}
    if config.health_probe_type:
        try:
            response = post_json(build_hook_url(), {"type": config.health_probe_type, "data": {}}, timeout=config.request_timeout_seconds)
            hook_status = {"ok": response_ok(response), "response": response}
        except Exception as exc:
            hook_status = {"ok": False, "error": str(exc)}
    hook_runtime_status: dict[str, Any] = {"ok": None, "reason": "status_probe_unavailable"}
    try:
        hook_runtime_status = get_json(build_hook_status_url(), timeout=config.request_timeout_seconds)
    except Exception as exc:
        hook_runtime_status = {"ok": False, "error": str(exc)}
    return {
        "hook_status": hook_status,
        "hook_runtime_status": hook_runtime_status,
        "duration_ms": int((time.time() - started) * 1000),
    }


def mark_hook_health_probe_done(done: Future) -> None:
    """记录后台 Hook 探测结果；失败只影响缓存状态，不拖慢 8181 健康接口。"""
    error = ""
    result: dict[str, Any] = {}
    try:
        probe_result = done.result()
        result = probe_result if isinstance(probe_result, dict) else {}
    except Exception as exc:
        error = str(exc)
    with hook_health_lock:
        hook_health_status["refreshing"] = False
        hook_health_status["last_checked_at"] = _now_text()
        hook_health_status["_last_checked_epoch"] = time.time()
        hook_health_status["last_duration_ms"] = result.get("duration_ms")
        hook_health_status["last_error"] = error
        if error:
            hook_health_status["hook_status"] = {"ok": False, "error": error}
            hook_health_status["hook_runtime_status"] = {"ok": False, "error": error}
        else:
            hook_health_status["hook_status"] = result.get("hook_status") or {"ok": None, "reason": "probe_unavailable"}
            hook_health_status["hook_runtime_status"] = result.get("hook_runtime_status") or {"ok": None, "reason": "status_probe_unavailable"}
    if error:
        log_debug_event("provider hook health background probe failed", {"error": error})


@app.get("/health")
async def health(request: Request):
    await log_incoming_request(request, "incoming provider health request")
    assert_provider_authorized(request)
    hook_probe_refresh = submit_hook_health_probe_if_needed()
    hook_health = hook_health_status_snapshot()
    hook_status = hook_health.get("hook_status") if isinstance(hook_health.get("hook_status"), dict) else {"ok": None, "reason": "probe_pending"}
    hook_runtime_status = hook_health.get("hook_runtime_status") if isinstance(hook_health.get("hook_runtime_status"), dict) else {"ok": None, "reason": "probe_pending"}
    result = {
        "ok": hook_status.get("ok") is not False,
        "provider": "workwechat",
        "status": "online" if hook_status.get("ok") is not False else "degraded",
        "hook_api_base": config.hook_api_base,
        "hook_runtime": {
            "auto_start_hook": config.auto_start_hook,
            "owned_process": hook_process_owned,
            "pid": hook_process.pid if hook_process else None,
            "running": bool(hook_process and hook_process.poll() is None),
            "runtime_dir": str(resolve_hook_runtime_dir()),
            "python": hook_python_used,
            "python_bitness": python_bitness(hook_python_used) if hook_python_used else None,
        },
        "callback_configured": bool(config.callback_url),
        "cloud_poll": dict(cloud_status),
        "inbound_callback": inbound_callback_status_snapshot(),
        "hook_health_cache": hook_health,
        "hook_health_refresh": hook_probe_refresh,
        "hook_status": hook_status,
        "hook_runtime_status": hook_runtime_status,
        "recent_message_count": len(recent_messages),
    }
    log_debug_event("provider health response", result)
    return result


@app.post("/send-message")
async def send_message(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming provider send-message request", body)
    assert_provider_authorized(request)
    command = build_hook_command(body if isinstance(body, dict) else {})
    log_debug_event("normalized send-message hook command", command)
    try:
        response = post_json(build_hook_url(), command, timeout=config.request_timeout_seconds)
    except Exception as exc:
        log_debug_event("send-message hook api exception", {"error": str(exc), "command": command})
        raise HTTPException(status_code=502, detail=f"hook_api_error: {exc}") from exc
    ok = response_ok(response)
    result = {
        "ok": ok,
        "status": "sent" if ok else "failed",
        "external_status": "sent" if ok else "failed",
        "external_message_id": None,
        "hook_command_type": command.get("type"),
        "hook_response": response,
    }
    log_debug_event("provider send-message response", result)
    return result


@app.post("/inbound-media/download")
async def download_inbound_media(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming provider inbound-media download request", body)
    assert_provider_authorized(request)
    payload = body if isinstance(body, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    message_type = str(payload.get("message_type") or data.get("message_type") or "file").strip() or "file"
    # 主服务在附件本地路径失效时会把原始 CDN 元数据发回来；这里复用入站回调同一套下载和上传逻辑，
    # 成功后返回主服务自己的 storage_path，避免业务系统继续依赖客服电脑本地缓存。
    result = download_inbound_hook_media_if_needed(data, message_type=message_type)
    ok = not bool(result.get("download_error"))
    response = {
        "ok": ok,
        "status": "downloaded" if ok else "failed",
        **result,
    }
    log_debug_event("provider inbound-media download response", response)
    return response


@app.post("/hook-callback")
async def hook_callback(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming hook callback raw request", body)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be object")
    normalized = normalize_hook_payload(body)
    log_debug_event("normalized hook callback payload", normalized)
    recent_messages.appendleft(normalized)
    # 先 ACK 给 Hook runtime，再用独立线程池转发到云端/主服务，避免云端耗时拖住 FastAPI 响应。
    queued = submit_inbound_callback(normalized)
    result = {"ok": True, "accepted": True, "callback_mode": "threadpool", "callback_queue": queued, "normalized": normalized}
    log_debug_event("provider hook-callback accepted", result)
    return result


@app.post("/chatrooms/list")
async def chatroom_list(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming provider chatrooms list request", body)
    assert_provider_authorized(request)
    payload = body if isinstance(body, dict) else {}
    page_result = fetch_hook_pages(
        hook_type=11038,
        request_data={},
        list_key="room_list",
        page_size=payload.get("page_size"),
        max_pages=payload.get("max_pages"),
    )
    rooms = page_result.pop("items", [])
    result = {
        "ok": bool(page_result.get("ok")),
        "rooms": rooms,
        "total": page_result.get("total") or len(rooms),
        "total_page": page_result.get("total_page"),
        "page_size": page_result.get("page_size"),
        "hook_response": page_result,
    }
    log_debug_event("provider chatrooms list response", result)
    return result


@app.post("/chatrooms/members")
async def chatroom_members(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming provider chatroom members request", body)
    assert_provider_authorized(request)
    payload = body if isinstance(body, dict) else {}
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        raise HTTPException(status_code=400, detail="missing_conversation_id")
    page_result = fetch_hook_pages(
        hook_type=11040,
        request_data={"conversation_id": conversation_id},
        list_key="member_list",
        page_size=payload.get("page_size"),
        max_pages=payload.get("max_pages"),
    )
    members = page_result.pop("items", [])
    result = {
        "ok": bool(page_result.get("ok")),
        "conversation_id": conversation_id,
        "members": members,
        "total": page_result.get("total") or len(members),
        "total_page": page_result.get("total_page"),
        "page_size": page_result.get("page_size"),
        "hook_response": page_result,
    }
    log_debug_event("provider chatroom members response", result)
    return result


@app.post("/contacts/list")
async def contact_list(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming provider contacts list request", body)
    assert_provider_authorized(request)
    payload = body if isinstance(body, dict) else {}
    requested_kind = str(payload.get("contact_kind") or payload.get("kind") or "all").strip()
    kinds = ["internal_member", "external_contact"] if requested_kind in {"", "all"} else [requested_kind]
    contacts: list[dict[str, Any]] = []
    hook_responses: dict[str, Any] = {}
    ok = True
    for kind in kinds:
        if kind == "internal_member":
            hook_type = 11036
        elif kind == "external_contact":
            hook_type = 11037
        else:
            raise HTTPException(status_code=400, detail=f"unsupported_contact_kind:{kind}")
        page_result = fetch_hook_pages(
            hook_type=hook_type,
            request_data={},
            list_key="user_list",
            page_size=payload.get("page_size"),
            max_pages=payload.get("max_pages"),
        )
        items = page_result.pop("items", [])
        for item in items:
            enriched = dict(item)
            enriched.setdefault("contact_kind", kind)
            enriched.setdefault("source", "provider_sync")
            contacts.append(enriched)
        hook_responses[kind] = page_result
        ok = ok and bool(page_result.get("ok"))
    result = {
        "ok": ok,
        "contacts": contacts,
        "total": len(contacts),
        "contact_kind": requested_kind or "all",
        "hook_response": hook_responses,
    }
    log_debug_event("provider contacts list response", result)
    return result


@app.post("/contacts/detail")
async def contact_detail(request: Request):
    body = await request.json()
    await log_incoming_request(request, "incoming provider contact detail request", body)
    assert_provider_authorized(request)
    payload = body if isinstance(body, dict) else {}
    user_id = str(payload.get("user_id") or payload.get("userid") or payload.get("contact_external_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="missing_user_id")
    response = post_json(build_hook_url(), {"type": 11039, "data": {"user_id": user_id}}, timeout=config.request_timeout_seconds)
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    contact = dict(data)
    if payload.get("contact_kind"):
        contact.setdefault("contact_kind", str(payload.get("contact_kind")))
    contact.setdefault("source", "provider_sync")
    result = {
        "ok": response_ok(response),
        "contact": contact,
        "hook_response": response,
    }
    log_debug_event("provider contact detail response", result)
    return result


@app.get("/debug/recent-messages")
async def debug_recent_messages(request: Request):
    await log_incoming_request(request, "incoming provider recent-messages request")
    assert_provider_authorized(request)
    result = {"ok": True, "items": list(recent_messages)}
    log_debug_event("provider recent-messages response", result)
    return result


@app.post("/reload")
async def reload_config(request: Request):
    await log_incoming_request(request, "incoming provider reload request")
    assert_provider_authorized(request)
    global config, recent_messages
    config = load_config()
    configure_debug_logging()
    recent_messages = deque(list(recent_messages), maxlen=config.debug_recent_message_limit)
    hook_start = start_embedded_hook_if_needed()
    result = {"ok": True, "provider": "workwechat", "config_path": str(CONFIG_PATH), "config": config_debug_snapshot(), "hook_start": hook_start}
    log_info_event("provider reload response", result)
    return result


if __name__ == "__main__":
    uvicorn.run(app, host=config.provider_host, port=config.provider_port)
