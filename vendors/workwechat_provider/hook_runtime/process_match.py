# -*- coding: utf8 -*-
from __future__ import annotations

import os


def normalize_process_name(value: str) -> str:
    """统一进程名格式，避免大小写或完整路径影响判断。"""
    return os.path.basename(str(value or "").strip()).lower()


def is_workwechat_window_candidate(process_path: str, title: str, target_name: str = "WXWork.exe") -> bool:
    """判断枚举到的窗口是否可以作为企业微信注入目标。

    启动器窗口标题是“企业微信 Provider”，如果只用“标题包含企业微信”判断，
    Hook runtime 会把自己的 UI 子进程误当成 WXWork.exe 注入，导致 8989 一直处于
    “服务未连接”状态。这里优先要求进程名匹配 WXWork.exe；只有在 Windows 权限导致
    读取不到进程路径时，才允许标题精确等于“企业微信”作为兜底。
    """
    normalized_target = normalize_process_name(target_name or "WXWork.exe") or "wxwork.exe"
    process_name = normalize_process_name(process_path)
    window_title = str(title or "").strip()
    if process_name:
        return process_name == normalized_target
    return window_title == "企业微信"


def workwechat_window_priority(candidate: dict) -> tuple[int, int]:
    """给企业微信窗口候选排序，优先选择标题精确的主窗口，其次按 PID 稳定排序。

    新版企业微信在部分机器上 `MainWindowTitle` 可能短时间为空；这类窗口只在没有
    精确标题候选时兜底使用，避免错过真实的 WXWork.exe 主进程。
    """
    title = str(candidate.get("title") or "").strip()
    if title == "企业微信":
        title_rank = 0
    elif title:
        title_rank = 1
    else:
        title_rank = 2
    return (title_rank, int(candidate.get("pid") or 0))
