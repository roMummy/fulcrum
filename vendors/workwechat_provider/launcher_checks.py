from __future__ import annotations

import ctypes
import json
import os
import socket
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import psutil


def normalize_version(value: str) -> str:
    # 企业微信文件版本正常是四段数字；如果 Windows 返回了额外描述，这里只保留前四段数字。
    text = str(value or "").strip()
    parts = [part for part in text.split(".") if part.isdigit()]
    return ".".join(parts[:4])


def is_required_version(current: str, required: str) -> bool:
    # Hook DLL 和企业微信客户端版本必须精确匹配，不能把临近版本当成兼容版本。
    return bool(required) and normalize_version(current) == normalize_version(required)


def find_workwechat_process(process_name: str) -> dict[str, Any] | None:
    # 启动器首先通过进程名判断企业微信是否已经运行；exe 路径用于版本校验，但不能因为读取不到 exe 就误判未启动。
    expected = str(process_name or "").strip().lower()
    if not expected:
        return None

    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = process.info
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if str(info.get("name") or "").lower() == expected:
            return {"pid": info["pid"], "name": info["name"], "exe": info.get("exe") or ""}
    return None


def resolve_workwechat_executable(process_name: str, process_exe: str = "") -> str:
    """解析企业微信客户端 exe 路径。

    有些现场环境下普通权限进程无法读取已运行 WXWork.exe 的完整路径，
    UI 如果只依赖 psutil 的 exe 字段就会显示“未知版本”。这里先信任进程
    返回的路径，再按 Windows 常见安装目录兜底查找。
    """
    direct_text = str(process_exe or "").strip()
    direct_path = Path(direct_text) if direct_text else None
    if direct_path and direct_path.exists():
        return str(direct_path)

    executable_name = str(process_name or "WXWork.exe").strip() or "WXWork.exe"
    candidate_roots = [
        os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        os.environ.get("ProgramFiles") or r"C:\Program Files",
        os.environ.get("LOCALAPPDATA") or "",
    ]
    candidates: list[Path] = []
    for root in candidate_roots:
        if not root:
            continue
        root_path = Path(root)
        candidates.extend(
            [
                root_path / "WXWork" / executable_name,
                root_path / "Tencent" / "WXWork" / executable_name,
                root_path / "Programs" / "WXWork" / executable_name,
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def get_workwechat_client_version(process_name: str, process_exe: str = "") -> str:
    """读取企业微信客户端文件版本，读取不到时返回空字符串。

    版本校验对 Hook 注入很关键，但读取失败不等价于版本不匹配；调用方会
    保留“未知版本”提示，并阻止错误版本时继续注入。
    """
    executable_path = resolve_workwechat_executable(process_name, process_exe)
    return get_windows_file_version(executable_path) if executable_path else ""


def is_tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    # 端口检测用于区分 provider/hook 是未启动、已启动，还是被其他程序占用。
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_windows_file_version(path: str | Path) -> str:
    # 文件版本只能在 Windows 上通过 version.dll 读取；非 Windows 测试环境直接返回空字符串。
    if os.name != "nt":
        return ""

    file_path = str(path)
    if not file_path:
        return ""

    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(file_path, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(file_path, 0, size, buffer)

        value = ctypes.c_void_p()
        value_size = ctypes.c_uint()
        ctypes.windll.version.VerQueryValueW(
            buffer,
            "\\",
            ctypes.byref(value),
            ctypes.byref(value_size),
        )
        fixed_info = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    except (OSError, ValueError, AttributeError):
        return ""

    major = fixed_info.dwFileVersionMS >> 16
    minor = fixed_info.dwFileVersionMS & 0xFFFF
    build = fixed_info.dwFileVersionLS >> 16
    revision = fixed_info.dwFileVersionLS & 0xFFFF
    return f"{major}.{minor}.{build}.{revision}"


def fetch_provider_health(base_url: str, token: str = "", timeout: float = 3.0) -> dict[str, Any]:
    # 启动器只调用 provider 的高层健康接口，不直接探测 Hook 细节，避免 UI 和底层协议强耦合。
    health_url = f"{str(base_url).rstrip('/')}/health"
    req = urlrequest.Request(health_url, method="GET")
    if token:
        req.add_header("X-Provider-Token", token)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_health_payload"}


def normalize_provider_health(payload: dict[str, Any]) -> dict[str, str]:
    # 归一化后给窗口状态机消费，窗口不用理解 provider 返回的完整技术结构。
    if not isinstance(payload, dict) or payload.get("error"):
        return {
            "provider_state": "error",
            "hook_state": "unknown",
            "error": str(payload.get("error") if isinstance(payload, dict) else "health_check_failed"),
        }

    provider_state = "running" if payload.get("ok") is not False else "degraded"
    hook_payload = payload.get("hook") if isinstance(payload.get("hook"), dict) else payload.get("hook_status")
    hook_runtime = payload.get("hook_runtime") if isinstance(payload.get("hook_runtime"), dict) else {}
    hook_ok = hook_payload.get("ok") if isinstance(hook_payload, dict) else None
    hook_running = bool(hook_runtime.get("running")) if isinstance(hook_runtime, dict) else False

    if hook_ok is True:
        hook_state = "running"
    elif hook_ok is False:
        hook_state = "error"
    elif hook_running:
        hook_state = "starting"
    else:
        hook_state = "unknown"

    callback_state = "configured" if payload.get("callback_configured") else "unconfigured"

    return {
        "provider_state": provider_state,
        "hook_state": hook_state,
        "callback_state": callback_state,
        "error": "" if provider_state == "running" and hook_state != "error" else str(payload.get("status") or ""),
    }


class VS_FIXEDFILEINFO(ctypes.Structure):
    # Windows VS_FIXEDFILEINFO 结构只取文件版本字段，避免启动器依赖 pywin32。
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]
