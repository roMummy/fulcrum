from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "WorkWechatProvider"


def resolve_distribution_root() -> Path:
    """解析启动器所在目录，用于兼容旧版本 exe 同级配置迁移和开发环境源码运行。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_local_app_root() -> Path:
    """解析稳定运行目录。

    单文件 exe 的 `_MEI...` 是 PyInstaller 临时解包目录，退出时会被删除；
    provider server、Hook runtime、config、logs、data 都放到用户级稳定目录，才能避免
    子进程仍占用文件时触发“临时目录删除失败”，同时保持客服侧只看到一个 exe。
    """
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_DIR_NAME
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"
