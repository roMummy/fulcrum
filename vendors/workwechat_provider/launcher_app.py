from __future__ import annotations

import json
import os
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
from threading import Thread
from pathlib import Path

try:  # pragma: no cover - 当前 Linux 测试环境通常不安装 PySide2，Windows 打包环境会安装。
    from PySide2.QtCore import QPoint, Qt, QTimer
    from PySide2.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap, QPolygon
    from PySide2.QtWidgets import (
        QAction,
        QApplication,
        QCheckBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMenu,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QStyle,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - 让纯辅助函数在非 GUI 环境仍可被单元测试导入。
    Qt = None
    QPoint = None
    QTimer = None
    QColor = None
    QFont = None
    QIcon = None
    QLinearGradient = None
    QPainter = None
    QPen = None
    QPixmap = None
    QPolygon = None
    QAction = None
    QApplication = None
    QCheckBox = None
    QFrame = None
    QGridLayout = None
    QHBoxLayout = None
    QLabel = None
    QLineEdit = None
    QMenu = None
    QMessageBox = None
    QPushButton = None
    QSizePolicy = None
    QStyle = None
    QSystemTrayIcon = None
    QVBoxLayout = None
    QWidget = object

from launcher_checks import (
    fetch_provider_health,
    find_workwechat_process,
    get_workwechat_client_version,
    is_required_version,
    normalize_provider_health,
)
from launcher_controller import ProviderPortCleanupError, ProviderPortConflictError, ProviderProcessController
from launcher_paths import resolve_distribution_root, resolve_local_app_root
from launcher_state import LauncherStatus


APP_ROOT = resolve_local_app_root()
DISTRIBUTION_ROOT = resolve_distribution_root()
CONFIG_PATH = APP_ROOT / "config.json"
AUTOSTART_NAME = "WorkWechatProvider"
AUTHORIZATION_CODE_PATTERN = re.compile(r"^wwauth[-_][A-Za-z0-9_-]{6,}$")
DEFAULT_CONFIG_TEXT = """{
  "provider_host": "0.0.0.0",
  "provider_port": 8181,
  "hook_api_base": "http://127.0.0.1:8989",
  "hook_api_path": "/api",
  "auto_start_hook": true,
  "hook_runtime_dir": "hook_runtime",
  "hook_python_executable": "",
  "required_workwechat_version": "5.0.0.6008",
  "workwechat_process_name": "WXWork.exe",
  "hook_runtime_executable": "",
  "hook_callback_url": "",
  "hook_startup_timeout_seconds": 20,
  "callback_url": "http://127.0.0.1:8180/webhooks/CHANNEL_ID/inbound",
  "callback_secret": "replace-with-channel-bot-token",
  "callback_timeout_seconds": 10,
  "provider_token": "replace-with-provider-token",
  "request_timeout_seconds": 12,
  "health_probe_type": 11035,
  "debug_recent_message_limit": 50,
  "debug_log_enabled": true,
  "debug_log_path": "logs/provider-debug.log",
  "debug_log_body_max_chars": 20000,
  "chatroom_sync_page_size": 100,
  "chatroom_sync_max_pages": 20,
  "outbound_file_cache_dir": "data/outbound_file_cache",
  "cloud_poll_enabled": false,
  "cloud_base_url": "",
  "authorization_code": "",
  "client_token": "",
  "cloud_channel_id": null,
  "provider_name": "",
  "poll_interval_seconds": 30,
  "poll_batch_size": 5
}
"""
APP_STYLE = """
QWidget#LauncherRoot {
    background: #eef5f8;
    color: #16323f;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
    font-size: 13px;
}
QLabel#BrandBadge {
    min-width: 46px;
    min-height: 46px;
    max-width: 46px;
    max-height: 46px;
}
QLabel#BrandTitle {
    color: #102a36;
    font-size: 18px;
    font-weight: 700;
}
QLabel#BrandSubtitle,
QLabel#CardHelper {
    color: #6d8590;
    font-size: 12px;
}
QFrame#SummaryPanel {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f6fd6, stop:1 #17b584);
    border-radius: 14px;
}
QLabel#Eyebrow {
    color: rgba(255, 255, 255, 190);
    font-size: 12px;
    font-weight: 600;
}
QLabel#SummaryTitle {
    color: white;
    font-size: 28px;
    font-weight: 800;
}
QLabel#SummaryDetail {
    color: rgba(255, 255, 255, 220);
    font-size: 13px;
    line-height: 150%;
}
QLabel#SummaryBadge {
    border-radius: 13px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 700;
}
QLabel#SummaryBadge[tone="ok"] {
    background: #dff8ea;
    color: #08763d;
}
QLabel#SummaryBadge[tone="busy"] {
    background: #e1efff;
    color: #145fb5;
}
QLabel#SummaryBadge[tone="bad"] {
    background: #ffe8e3;
    color: #b63324;
}
QLabel#SummaryBadge[tone="idle"] {
    background: #e8eef2;
    color: #526a75;
}
QFrame#StatusCard {
    background: #ffffff;
    border: 1px solid #d8e5eb;
    border-radius: 10px;
}
QLabel#CardTitle {
    color: #55707b;
    font-size: 12px;
    font-weight: 700;
}
QLabel#CardValue {
    color: #173441;
    font-size: 18px;
    font-weight: 800;
}
QPushButton {
    min-height: 34px;
    border-radius: 8px;
    padding: 0 14px;
    font-weight: 700;
}
QPushButton#PrimaryButton {
    background: #1f7ae0;
    color: white;
    border: 1px solid #1f7ae0;
}
QPushButton#PrimaryButton:hover {
    background: #176bc8;
}
QPushButton#PrimaryButton:pressed {
    background: #1059aa;
    padding-top: 1px;
}
QPushButton#PrimaryButton:disabled {
    background: #8fb9e8;
    border-color: #8fb9e8;
    color: rgba(255, 255, 255, 190);
}
QPushButton#SecondaryButton {
    background: #ffffff;
    color: #244552;
    border: 1px solid #cbdbe2;
}
QPushButton#SecondaryButton:hover {
    background: #f5fbfd;
    border-color: #9fc1cf;
}
QPushButton#SecondaryButton:pressed {
    background: #e5f1f6;
    padding-top: 1px;
}
QPushButton#SecondaryButton:disabled {
    background: #eef3f6;
    color: #8aa0aa;
    border-color: #d7e2e7;
}
QCheckBox {
    color: #385764;
    font-weight: 600;
}
QLineEdit {
    min-height: 32px;
    border-radius: 8px;
    border: 1px solid #cbdbe2;
    padding: 0 10px;
    background: #ffffff;
    color: #173441;
}
QLineEdit:focus {
    border-color: #1f7ae0;
}
"""


def build_logo_pixmap(size: int = 96):
    """绘制内置企业微信 Provider LOGO。

    LOGO 使用 Qt 矢量绘制，打包时不需要额外图片资源；蓝绿色渐变和对话气泡
    保持企业微信的识别感，同时叠加连接节点表达 provider/hook 的桥接能力。
    """
    if QPixmap is None or QPainter is None:
        return None
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, QColor("#12b886"))
    gradient.setColorAt(1, QColor("#1677ff"))
    painter.setBrush(gradient)
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, size - 8, size - 8, int(size * 0.24), int(size * 0.24))

    painter.setBrush(QColor(255, 255, 255, 235))
    painter.drawRoundedRect(int(size * 0.22), int(size * 0.25), int(size * 0.48), int(size * 0.34), int(size * 0.11), int(size * 0.11))
    painter.drawPolygon(
        QPolygon(
            [
                QPoint(int(size * 0.37), int(size * 0.58)),
                QPoint(int(size * 0.45), int(size * 0.58)),
                QPoint(int(size * 0.36), int(size * 0.69)),
            ]
        )
    )

    painter.setBrush(QColor("#0d5bd7"))
    painter.setPen(Qt.NoPen)
    node_radius = max(3, int(size * 0.055))
    for x, y in ((0.34, 0.40), (0.50, 0.40), (0.66, 0.40)):
        painter.drawEllipse(int(size * x) - node_radius, int(size * y) - node_radius, node_radius * 2, node_radius * 2)

    pen = QPen(QColor(255, 255, 255, 220), max(2, int(size * 0.035)))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(int(size * 0.50), int(size * 0.63), int(size * 0.72), int(size * 0.78))
    painter.drawLine(int(size * 0.72), int(size * 0.78), int(size * 0.82), int(size * 0.60))
    painter.setBrush(QColor(255, 255, 255, 245))
    painter.setPen(Qt.NoPen)
    for x, y in ((0.50, 0.63), (0.72, 0.78), (0.82, 0.60)):
        painter.drawEllipse(int(size * x) - node_radius, int(size * y) - node_radius, node_radius * 2, node_radius * 2)

    painter.end()
    return pixmap


def build_logo_icon():
    """生成窗口和托盘共用图标，避免托盘继续显示系统默认电脑图标。"""
    pixmap = build_logo_pixmap(128)
    return QIcon(pixmap) if QIcon is not None and pixmap is not None else None


def resolve_bundled_root() -> Path | None:
    # onefile 启动器会把 config.example.json 和内置二进制解压到 sys._MEIPASS；
    # UI 只在需要生成默认配置或启动内置 server 时读取这个临时目录。
    bundled_root = getattr(sys, "_MEIPASS", "")
    if not bundled_root:
        return None
    bundled_path = Path(bundled_root).resolve()
    return bundled_path if bundled_path.exists() else None


def ensure_config_file() -> None:
    # 单文件交付时默认不附带 config.json；首次启动写入稳定运行目录，避免 exe 所在目录不可写或被误删。
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    legacy_config = DISTRIBUTION_ROOT / "config.json"
    if legacy_config.exists() and legacy_config.resolve() != CONFIG_PATH.resolve():
        # 兼容旧版本：之前 config.json 生成在 exe 同级；升级后自动迁移到 LocalAppData，避免现场 token/回调地址丢失。
        shutil.copy2(legacy_config, CONFIG_PATH)
        return
    bundled_root = resolve_bundled_root()
    bundled_example = bundled_root / "config.example.json" if bundled_root else None
    if bundled_example and bundled_example.exists():
        CONFIG_PATH.write_text(bundled_example.read_text(encoding="utf-8"), encoding="utf-8")
        return
    CONFIG_PATH.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


def load_config_dict(config_path: Path) -> dict:
    # 启动器和单元测试都通过这个纯函数读取配置；读取失败时回退为空对象，
    # 避免现场 config.json 被误编辑后导致窗口无法打开。
    if not config_path.exists():
        try:
            parsed = json.loads(DEFAULT_CONFIG_TEXT)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_cloud_authorization_config(config_path: Path, cloud_base_url: str, authorization_code: str) -> dict:
    # 客户端只保存云端地址和一次性授权码，不再要求客服或运维填写 channel_id、bot_id、API Base。
    # Provider 首次连接云端时会用授权码换取 client_token，云端根据授权码哈希自行匹配渠道。
    config = load_config_dict(config_path)
    normalized_base_url = str(cloud_base_url or "").strip().rstrip("/")
    normalized_authorization_code = str(authorization_code or "").strip()
    # 云端地址和授权码在 UI 上是相邻输入项，现场最容易把云端地址误粘到授权码里；
    # 保存前先做格式校验，避免 provider 后台一直拿错误授权码注册并停留在“待注册”。
    if not normalized_base_url.startswith(("http://", "https://")):
        raise ValueError("cloud_base_url_invalid")
    if not AUTHORIZATION_CODE_PATTERN.match(normalized_authorization_code):
        raise ValueError("authorization_code_invalid")
    previous_base_url = str(config.get("cloud_base_url") or "").strip().rstrip("/")
    previous_authorization_code = str(config.get("authorization_code") or "").strip()

    config["cloud_poll_enabled"] = True
    config["cloud_base_url"] = normalized_base_url
    config["authorization_code"] = normalized_authorization_code

    # 授权码或云端域名变化时，旧 client_token 可能已经绑定到另一个渠道或服务器；
    # 这里主动清空，保证下一次启动会重新注册，避免拿旧 token 连接错渠道。
    if normalized_base_url != previous_base_url or normalized_authorization_code != previous_authorization_code:
        config["client_token"] = ""
        config["cloud_channel_id"] = None

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return config


def build_cloud_registration_label(config: dict) -> str:
    # 云端注册成功后 authorization_code 会被 Provider 主动清空，避免一次性授权码继续留在客服电脑上；
    # 因此 UI 必须以长期 client_token 作为“已注册”的判断依据，不能再依赖授权码是否存在。
    client_token = str(config.get("client_token") or "").strip()
    authorization_code = str(config.get("authorization_code") or "").strip()
    channel_id = config.get("cloud_channel_id")
    if client_token:
        if channel_id not in (None, ""):
            return f"已注册 · 渠道 {channel_id}"
        return "已注册"
    if authorization_code:
        return "待注册"
    return "未配置"


def ensure_runtime_dirs() -> None:
    # 稳定运行目录启动时一次性创建，确保“打开日志”在 provider 产生日志前也能正常打开目录。
    for path in (APP_ROOT, APP_ROOT / "logs", APP_ROOT / "data"):
        path.mkdir(parents=True, exist_ok=True)


def open_logs_dir() -> None:
    ensure_runtime_dirs()
    open_path(APP_ROOT / "logs")


def build_autostart_command(launcher_path: Path) -> str:
    # Windows Run 注册表值需要完整引用 exe 路径，目录包含空格时也能正常开机启动。
    return f'"{launcher_path.as_posix()}"'


def get_launcher_executable_path() -> Path:
    # PyInstaller 打包后 sys.executable 指向启动器 exe；开发环境则回退到当前脚本路径。
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(__file__).resolve()


def set_autostart_enabled(enabled: bool) -> None:
    # 只写当前用户的开机启动项，不需要管理员权限，也不会影响其他 Windows 用户。
    if os.name != "nt":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, build_autostart_command(get_launcher_executable_path()))
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def is_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
    except FileNotFoundError:
        return False
    return str(value or "") == build_autostart_command(get_launcher_executable_path())


def open_path(path: Path) -> None:
    # 打开日志和配置都走系统默认程序，客服不需要知道具体编辑器或资源管理器命令。
    target = str(path)
    if os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def should_auto_start_from_status(status: LauncherStatus) -> bool:
    # 启动器打开后只要已经检测到企业微信进程且版本匹配，就直接进入启动流程；
    # 找不到进程或版本不匹配时仍停留在提示状态，避免错误版本继续注入。
    return status.workwechat_state == "ready"


def format_status_text(value: str) -> str:
    # 状态字段来自内部状态机，统一转换成客服能快速理解的中文。
    mapping = {
        "unknown": "检测中",
        "missing": "未打开",
        "ready": "已就绪",
        "version_mismatch": "版本不匹配",
        "stopped": "未启动",
        "starting": "启动中",
        "running": "运行中",
        "degraded": "部分异常",
        "error": "异常",
        "configured": "已配置",
        "unconfigured": "未配置",
    }
    return mapping.get(value or "", value or "检测中")


def status_tone(value: str) -> str:
    # QSS 通过 tone 属性渲染颜色，避免在业务代码里散落硬编码样式。
    if value in {"running", "ready", "configured"}:
        return "ok"
    if value in {"starting", "unknown"}:
        return "busy"
    if value in {"missing", "version_mismatch", "error", "degraded"}:
        return "bad"
    return "idle"


class LauncherWindow(QWidget):
    def __init__(self):
        if QApplication is None:
            raise RuntimeError("PySide2 is required to run WorkWechatProvider launcher")
        super().__init__()
        ensure_runtime_dirs()
        self.controller = ProviderProcessController(APP_ROOT, DISTRIBUTION_ROOT)
        self.logo_icon = build_logo_icon()

        self.status_label = QLabel("等待启动")
        self.status_label.setObjectName("SummaryTitle")
        self.detail_label = QLabel("请打开并登录企业微信，启动器会自动继续。")
        self.detail_label.setObjectName("SummaryDetail")
        self.detail_label.setWordWrap(True)
        self.brand_badge = QLabel()
        self.brand_badge.setObjectName("BrandBadge")
        logo_pixmap = build_logo_pixmap(92)
        if logo_pixmap is not None:
            self.brand_badge.setPixmap(logo_pixmap.scaled(46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.brand_badge.setAlignment(Qt.AlignCenter)
        self.brand_title = QLabel("企业微信 Provider")
        self.brand_title.setObjectName("BrandTitle")
        self.brand_subtitle = QLabel("客服工作台连接器")
        self.brand_subtitle.setObjectName("BrandSubtitle")
        self.summary_badge = QLabel("待检测")
        self.summary_badge.setObjectName("SummaryBadge")
        self.workwechat_value_label = QLabel("检测中")
        self.version_value_label = QLabel("要求 5.0.0.6008")
        self.provider_value_label = QLabel("未启动")
        self.hook_value_label = QLabel("未启动")
        self.callback_value_label = QLabel("未配置")
        self.current_version_text = "未知"
        self.required_version_text = "5.0.0.6008"
        self.start_button = QPushButton("启动")
        self.restart_button = QPushButton("重启")
        self.log_button = QPushButton("打开日志")
        self.config_button = QPushButton("打开配置")
        self.cloud_base_input = QLineEdit()
        self.cloud_base_input.setPlaceholderText("https://server.example.com")
        self.authorization_code_input = QLineEdit()
        self.authorization_code_input.setPlaceholderText("wwauth_xxx")
        self.authorization_code_input.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.cloud_save_button = QPushButton("保存授权")
        self.cloud_status_label = QLabel("未配置")
        self.cloud_status_label.setObjectName("CardHelper")
        self.autostart_checkbox = QCheckBox("开机自启")
        self.health_timer = QTimer(self)
        self.async_result_timer = QTimer(self)
        self.async_result_queue: Queue = Queue()
        self.pending_async_jobs = 0
        self.operation_active = False
        self.health_poll_active = False
        self.tray = self.build_tray()

        self.setObjectName("LauncherRoot")
        self.setStyleSheet(APP_STYLE)

        # 主界面按客服日常排障顺序组织：先看企业微信，再看 provider、Hook 和回调状态。
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(14)
        layout.addWidget(self.build_header())
        layout.addWidget(self.build_summary_panel())
        layout.addWidget(self.build_cloud_authorization_panel())
        layout.addLayout(self.build_status_grid())
        layout.addLayout(self.build_action_bar())
        self.setLayout(layout)

        self.start_button.clicked.connect(self.start_provider)
        self.restart_button.clicked.connect(self.restart_provider)
        self.log_button.clicked.connect(open_logs_dir)
        self.config_button.clicked.connect(self.open_config)
        self.cloud_save_button.clicked.connect(self.save_cloud_authorization)
        self.autostart_checkbox.setChecked(is_autostart_enabled())
        self.autostart_checkbox.stateChanged.connect(lambda state: set_autostart_enabled(bool(state)))
        self.health_timer.timeout.connect(self.poll_provider_health)
        self.health_timer.setInterval(3000)
        self.async_result_timer.timeout.connect(self.drain_async_results)
        self.async_result_timer.setInterval(60)
        self.setWindowTitle("企业微信 Provider")
        if self.logo_icon is not None:
            self.setWindowIcon(self.logo_icon)
        self.load_cloud_authorization_fields()
        self.apply_button_cursors()
        self.resize(560, 520)

        # 窗口先完成显示，再异步做企业微信探测和自动启动，避免启动器打开时因为探测过程卡住界面。
        QTimer.singleShot(300, self.auto_start_if_workwechat_running)

    def build_header(self):
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(3)
        title_layout.addWidget(self.brand_title)
        title_layout.addWidget(self.brand_subtitle)

        layout.addWidget(self.brand_badge)
        layout.addLayout(title_layout)
        layout.addStretch(1)
        layout.addWidget(self.summary_badge)
        return header

    def build_summary_panel(self):
        panel = QFrame()
        panel.setObjectName("SummaryPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)
        eyebrow = QLabel("企业微信已启动时会自动进入连接流程")
        eyebrow.setObjectName("Eyebrow")
        layout.addWidget(eyebrow)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        return panel

    def build_status_grid(self):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.addWidget(self.build_status_card("企业微信", self.workwechat_value_label, "进程与登录状态"), 0, 0)
        grid.addWidget(self.build_status_card("客户端版本", self.version_value_label, "Hook 版本校验"), 0, 1)
        grid.addWidget(self.build_status_card("Provider", self.provider_value_label, "本机 8181 服务"), 1, 0)
        grid.addWidget(self.build_status_card("Hook", self.hook_value_label, "企微消息通道"), 1, 1)
        grid.addWidget(self.build_status_card("回调", self.callback_value_label, "主服务入站连通性"), 2, 0, 1, 2)
        return grid

    def build_cloud_authorization_panel(self):
        panel = QFrame()
        panel.setObjectName("StatusCard")
        layout = QGridLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        title_label = QLabel("云端授权")
        title_label.setObjectName("CardTitle")
        base_label = QLabel("云端地址")
        base_label.setObjectName("CardHelper")
        code_label = QLabel("授权码")
        code_label.setObjectName("CardHelper")
        self.cloud_save_button.setObjectName("PrimaryButton")

        layout.addWidget(title_label, 0, 0, 1, 3)
        layout.addWidget(base_label, 1, 0)
        layout.addWidget(self.cloud_base_input, 1, 1, 1, 2)
        layout.addWidget(code_label, 2, 0)
        layout.addWidget(self.authorization_code_input, 2, 1)
        layout.addWidget(self.cloud_save_button, 2, 2)
        layout.addWidget(self.cloud_status_label, 3, 0, 1, 3)
        return panel

    def build_status_card(self, title: str, value_label, helper: str):
        card = QFrame()
        card.setObjectName("StatusCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        value_label.setObjectName("CardValue")
        helper_label = QLabel(helper)
        helper_label.setObjectName("CardHelper")
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(helper_label)
        return card

    def build_action_bar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 4, 0, 0)
        bar.setSpacing(8)
        self.start_button.setObjectName("PrimaryButton")
        self.restart_button.setObjectName("SecondaryButton")
        self.log_button.setObjectName("SecondaryButton")
        self.config_button.setObjectName("SecondaryButton")
        bar.addWidget(self.start_button)
        bar.addWidget(self.restart_button)
        bar.addWidget(self.log_button)
        bar.addWidget(self.config_button)
        bar.addStretch(1)
        bar.addWidget(self.autostart_checkbox)
        return bar

    def build_tray(self):
        # 托盘让窗口关闭时继续后台运行，客服可以从托盘恢复窗口或执行重启。
        if QSystemTrayIcon is None or QApplication is None:
            return None
        tray = QSystemTrayIcon(self)
        icon = self.logo_icon or QApplication.style().standardIcon(QStyle.SP_ComputerIcon)
        tray.setIcon(icon)
        tray.setToolTip("企业微信 Provider")

        menu = QMenu()
        show_action = QAction("显示窗口", self)
        restart_action = QAction("重启", self)
        logs_action = QAction("打开日志", self)
        quit_action = QAction("退出", self)
        show_action.triggered.connect(self.show_from_tray)
        restart_action.triggered.connect(self.restart_provider)
        logs_action.triggered.connect(open_logs_dir)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addAction(restart_action)
        menu.addAction(logs_action)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.show()
        return tray

    def load_config(self) -> dict:
        # config.json 统一保留在 LocalAppData 稳定目录，UI “打开配置”负责定位，客服不需要进 exe 目录找文件。
        ensure_config_file()
        return load_config_dict(CONFIG_PATH)

    def load_cloud_authorization_fields(self) -> None:
        # 窗口启动时回显已保存的云端地址；授权码兑换成功后会被 provider 清空，
        # 这时状态文案显示“已注册”，客服不需要再看到历史授权码。
        cfg = self.load_config()
        self.cloud_base_input.setText(str(cfg.get("cloud_base_url") or ""))
        self.authorization_code_input.setText(str(cfg.get("authorization_code") or ""))
        self.render_cloud_registration_status(cfg)

    def render_cloud_registration_status(self, cfg: dict | None = None) -> None:
        # 健康轮询期间只刷新状态标签，不回写输入框，避免客服正在粘贴新授权码时被后台轮询覆盖。
        self.cloud_status_label.setText(build_cloud_registration_label(cfg or self.load_config()))

    def save_cloud_authorization(self) -> None:
        # 保存动作只写本地配置，不向云端探测；注册由 provider 启动后的 WS/HTTP 流程统一处理。
        try:
            save_cloud_authorization_config(
                CONFIG_PATH,
                self.cloud_base_input.text(),
                self.authorization_code_input.text(),
            )
        except ValueError as exc:
            # 这里给客服看可执行的中文提示，而不是后端枚举值；注册错误仍由 provider 日志记录细节。
            message_map = {
                "cloud_base_url_invalid": "云端地址需要以 http:// 或 https:// 开头。",
                "authorization_code_invalid": "授权码需要填写后台生成的 wwauth_xxx，不要填写云端地址。",
            }
            QMessageBox.warning(self, "授权配置有误", message_map.get(str(exc), "授权配置格式不正确。"))
            return
        self.load_cloud_authorization_fields()
        self.cloud_status_label.setText("已保存")

    def open_config(self) -> None:
        # 打开配置前先创建默认文件，避免单文件首次运行时系统提示找不到 config.json。
        ensure_config_file()
        open_path(CONFIG_PATH)

    def preflight(self) -> LauncherStatus:
        # Hook 注入前先确认企业微信已经启动且版本匹配，避免版本不对时继续注入造成更难理解的错误。
        cfg = self.load_config()
        required_version = str(cfg.get("required_workwechat_version") or "5.0.0.6008")
        process_name = str(cfg.get("workwechat_process_name") or "WXWork.exe")
        process = find_workwechat_process(process_name)
        if not process:
            return LauncherStatus(
                workwechat_state="missing",
                required_version=required_version,
                error="请先打开企业微信并登录，然后点击“启动”。",
            )

        current_version = get_workwechat_client_version(process_name, process.get("exe", ""))
        if current_version and not is_required_version(current_version, required_version):
            return LauncherStatus(
                workwechat_state="version_mismatch",
                current_version=current_version or "未知",
                required_version=required_version,
            )
        return LauncherStatus(
            workwechat_state="ready",
            current_version=current_version,
            required_version=required_version,
        )

    def start_provider(self) -> None:
        # 启动前检查企业微信进程、复制 onefile 内置 runtime、清理旧端口和拉起 provider 都可能耗时；
        # 这些工作必须放到后台线程，否则客服拖动窗口和点击按钮时会感觉整个窗口卡住。
        if self.operation_active:
            return
        self.render_status(LauncherStatus(provider_state="starting", hook_state="starting"))
        self.run_async_operation("启动中", self.start_provider_worker, self.handle_provider_start_result, self.handle_provider_operation_error)

    def auto_start_if_workwechat_running(self) -> None:
        # 自动启动也不能同步 preflight；psutil 枚举进程和读取版本信息在现场机器上可能明显卡顿。
        if self.operation_active:
            return
        self.run_async_operation("检测中", self.auto_start_worker, self.handle_auto_start_result, self.handle_provider_operation_error)

    def start_provider_worker(self) -> dict:
        status = self.preflight()
        if status.workwechat_state != "ready":
            return {"started": False, "status": status}
        self.controller.start()
        return {"started": True, "status": status}

    def auto_start_worker(self) -> dict:
        status = self.preflight()
        if not should_auto_start_from_status(status):
            return {"started": False, "status": status}
        self.controller.start()
        return {"started": True, "status": status}

    def restart_provider(self) -> None:
        # stop() 最长会等待 provider 退出 8 秒，必须后台执行；否则重启时窗口一定会失去响应。
        if self.operation_active:
            return
        self.render_status(LauncherStatus(provider_state="starting", hook_state="starting"))
        self.run_async_operation("重启中", self.restart_provider_worker, self.handle_provider_start_result, self.handle_provider_operation_error)

    def restart_provider_worker(self) -> dict:
        self.controller.stop()
        return self.start_provider_worker()

    def handle_provider_start_result(self, result: dict) -> None:
        status = result.get("status")
        if not result.get("started"):
            self.render_status(status if isinstance(status, LauncherStatus) else LauncherStatus(error="Provider 未启动"))
            return
        if isinstance(status, LauncherStatus):
            self.render_status(
                LauncherStatus(
                    workwechat_state="ready",
                    provider_state="starting",
                    hook_state="starting",
                    current_version=status.current_version,
                    required_version=status.required_version,
                )
            )
        self.health_timer.start()
        self.poll_provider_health()

    def handle_auto_start_result(self, result: dict) -> None:
        status = result.get("status")
        if result.get("started"):
            self.handle_provider_start_result(result)
            return
        self.render_status(status if isinstance(status, LauncherStatus) else LauncherStatus())

    def handle_provider_operation_error(self, exc: Exception) -> None:
        if isinstance(exc, ProviderPortConflictError):
            # 端口被非本项目进程占用时不自动关闭，直接把 PID/路径展示给现场人员，避免误杀其他服务。
            detail = "；".join(owner.description for owner in exc.conflicts)
            self.render_status(LauncherStatus(workwechat_state="ready", error=f"端口被其他程序占用：{detail}"))
            return
        if isinstance(exc, ProviderPortCleanupError):
            # 旧的企微 provider/hook 无法清理时停止启动，让现场先处理残留进程，避免新旧实例继续抢端口。
            self.render_status(LauncherStatus(workwechat_state="ready", error=str(exc)))
            return
        if isinstance(exc, OSError):
            self.render_status(LauncherStatus(workwechat_state="ready", error=f"Provider 启动失败：{exc}"))
            return
        self.render_status(LauncherStatus(error=f"Provider 操作失败：{exc}"))

    def poll_provider_health(self) -> None:
        # provider 启动后定时读取 /health，窗口状态以真实接口结果为准，而不是只看进程是否存在。
        if self.health_poll_active or self.operation_active:
            return
        self.health_poll_active = True
        self.run_async_job("health", self.poll_provider_health_worker, self.handle_provider_health_result, self.handle_provider_health_error)

    def poll_provider_health_worker(self) -> dict:
        cfg = self.load_config()
        port = int(cfg.get("provider_port") or 8181)
        token = str(cfg.get("provider_token") or "")
        payload = fetch_provider_health(f"http://127.0.0.1:{port}", token=token)
        normalized = normalize_provider_health(payload)
        # 健康轮询会频繁刷新整块 UI；这里同步刷新企业微信版本，避免自动启动时
        # “启动中/运行中”状态覆盖掉 preflight 刚读取到的客户端版本。
        workwechat_status = self.preflight()
        return {"config": cfg, "normalized": normalized, "workwechat_status": workwechat_status}

    def handle_provider_health_result(self, result: dict) -> None:
        normalized = result["normalized"]
        workwechat_status = result["workwechat_status"]
        self.render_status(
            LauncherStatus(
                workwechat_state=workwechat_status.workwechat_state,
                provider_state=normalized["provider_state"],
                hook_state=normalized["hook_state"],
                callback_state=normalized.get("callback_state", "unknown"),
                current_version=workwechat_status.current_version,
                required_version=workwechat_status.required_version,
                error=normalized.get("error", ""),
            )
        )
        self.render_cloud_registration_status(result["config"])

    def handle_provider_health_error(self, exc: Exception) -> None:
        self.render_status(LauncherStatus(provider_state="error", hook_state="unknown", error=f"健康检查失败：{exc}"))

    def run_async_operation(self, busy_text: str, worker, on_success, on_error) -> None:
        self.operation_active = True
        self.set_operation_buttons_busy(True, busy_text)
        self.run_async_job("operation", worker, on_success, on_error)

    def run_async_job(self, job_type: str, worker, on_success, on_error) -> None:
        """在线程里执行阻塞任务，并把 UI 更新投递回 Qt 主线程。

        PySide2 控件只能在主线程更新；这里用 Queue + QTimer 轮询结果，避免后台线程直接触碰控件。
        """

        self.pending_async_jobs += 1

        def target() -> None:
            try:
                self.async_result_queue.put((job_type, True, worker(), on_success, on_error))
            except Exception as exc:  # pragma: no cover - 具体异常由 UI 回调转成中文状态。
                self.async_result_queue.put((job_type, False, exc, on_success, on_error))

        Thread(target=target, daemon=True).start()
        if not self.async_result_timer.isActive():
            self.async_result_timer.start()

    def drain_async_results(self) -> None:
        while True:
            try:
                job_type, ok, payload, on_success, on_error = self.async_result_queue.get_nowait()
            except Empty:
                break
            self.pending_async_jobs = max(0, self.pending_async_jobs - 1)
            if job_type == "operation":
                self.operation_active = False
                self.set_operation_buttons_busy(False)
            if job_type == "health":
                self.health_poll_active = False
                if self.operation_active:
                    # 用户正在启动或重启时，丢弃稍早发出的健康检查结果，避免旧状态覆盖“启动中”。
                    continue
            if ok:
                on_success(payload)
            else:
                on_error(payload)
        if self.pending_async_jobs <= 0:
            self.async_result_timer.stop()

    def set_operation_buttons_busy(self, busy: bool, text: str = "") -> None:
        self.start_button.setEnabled(not busy)
        self.restart_button.setEnabled(not busy)
        self.cloud_save_button.setEnabled(not busy)
        self.autostart_checkbox.setEnabled(not busy)
        self.start_button.setText(text or "启动")
        self.restart_button.setText("请稍候" if busy else "重启")

    def apply_button_cursors(self) -> None:
        if Qt is None:
            return
        for button in (self.start_button, self.restart_button, self.log_button, self.config_button, self.cloud_save_button):
            button.setCursor(Qt.PointingHandCursor)

    def render_status(self, status: LauncherStatus) -> None:
        self.status_label.setText(status.summary)
        self.detail_label.setText(status.detail)
        self.summary_badge.setText(format_status_text(status.provider_state if status.provider_state != "stopped" else status.workwechat_state))
        self.summary_badge.setProperty("tone", status_tone(status.provider_state if status.provider_state != "stopped" else status.workwechat_state))
        self.summary_badge.style().unpolish(self.summary_badge)
        self.summary_badge.style().polish(self.summary_badge)
        self.workwechat_value_label.setText(format_status_text(status.workwechat_state))
        if status.current_version:
            self.current_version_text = status.current_version
        if status.required_version:
            self.required_version_text = status.required_version
        self.version_value_label.setText(f"{self.current_version_text} / {self.required_version_text}")
        self.provider_value_label.setText(format_status_text(status.provider_state))
        self.hook_value_label.setText(format_status_text(status.hook_state))
        self.callback_value_label.setText(format_status_text(status.callback_state))
        if self.tray:
            self.tray.setToolTip(f"企业微信 Provider：{status.summary}")

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        self.health_timer.stop()
        self.controller.stop()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 使用 camelCase 事件名。
        # 点窗口关闭按钮时默认隐藏到托盘，避免客服误关 provider。
        if self.tray and self.tray.isVisible():
            self.hide()
            event.ignore()
            return
        self.controller.stop()
        event.accept()


class SingleInstanceGuard:
    """Windows 单实例锁，避免多个启动器同时抢 provider 端口和 Hook runtime。"""

    def __init__(self) -> None:
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateMutexW(None, False, "Local\\WorkWechatProviderLauncher")
        if not self.handle:
            return True
        already_exists = kernel32.GetLastError() == 183
        return not already_exists

    def release(self) -> None:
        if os.name != "nt" or not self.handle:
            return
        import ctypes

        ctypes.windll.kernel32.CloseHandle(self.handle)
        self.handle = None


def main() -> int:
    if QApplication is None:
        raise RuntimeError("PySide2 is required to run WorkWechatProvider launcher")
    guard = SingleInstanceGuard()
    app = QApplication(sys.argv)
    # Windows 默认字体在部分环境会回退成较生硬的 SimSun；这里全局指定微软雅黑，
    # 再由 QSS 细调字号和粗细，确保客服机器上的中文显示更稳定。
    if QFont is not None:
        app.setFont(QFont("Microsoft YaHei UI", 9))
    if not guard.acquire():
        # 已有实例时直接提示并退出，避免多个 UI 同时启动多个 provider/hook 造成端口和企微绑定混乱。
        if QMessageBox is not None:
            QMessageBox.information(None, "企业微信 Provider", "企业微信 Provider 已在运行，请从托盘打开现有窗口。")
        guard.release()
        return 0
    window = LauncherWindow()
    window.show()
    try:
        return app.exec_()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
