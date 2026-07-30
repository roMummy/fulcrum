# WorkWechat Provider Launcher Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Windows GUI launcher that lets customer service staff start WorkWechat Provider with one click, while checking the installed WorkWechat client version before Hook startup.

**Architecture:** Add a PySide2 launcher entrypoint that runs as the only visible app. The launcher checks `WXWork.exe`, validates the file version, starts a packaged provider server executable, and the provider server starts a packaged Hook runtime executable instead of relying on a locally installed 32-bit Python. Keep `config.json` editable beside the package and keep detailed diagnostics in `logs/`.

**Tech Stack:** Python 3.10 32-bit, PySide2, FastAPI, Uvicorn, Flask, Pydantic, PyInstaller, psutil, unittest.

---

### Task 1: Add Launcher Config Defaults

**Files:**
- Modify: `vendors/workwechat_provider/config.example.json`
- Modify: `vendors/workwechat_provider/app.py`
- Test: `tests/test_workwechat_provider.py`

**Step 1: Write the failing test**

Add a provider config test near the existing `WorkWechatProviderTests`:

```python
def test_provider_config_includes_launcher_defaults(self):
    cfg = PROVIDER_APP.ProviderConfig()

    self.assertEqual(cfg.required_workwechat_version, "5.0.0.6008")
    self.assertEqual(cfg.workwechat_process_name, "WXWork.exe")
    self.assertEqual(cfg.hook_runtime_executable, "")
```

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider.WorkWechatProviderTests.test_provider_config_includes_launcher_defaults -v`

Expected: FAIL because `ProviderConfig` does not yet expose these fields.

**Step 3: Write minimal implementation**

Add fields to `ProviderConfig` in `vendors/workwechat_provider/app.py`:

```python
    # 启动器用这两个字段在 Hook 注入前确认本机企微版本，避免 DLL 和客户端版本不匹配。
    required_workwechat_version: str = "5.0.0.6008"
    workwechat_process_name: str = "WXWork.exe"
    # 打包版优先启动独立 Hook runtime exe；为空时保留现有 python http_py.py 兼容路径。
    hook_runtime_executable: str = ""
```

Add the same keys to `vendors/workwechat_provider/config.example.json`:

```json
  "required_workwechat_version": "5.0.0.6008",
  "workwechat_process_name": "WXWork.exe",
  "hook_runtime_executable": "",
```

**Step 4: Run test to verify it passes**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider.WorkWechatProviderTests.test_provider_config_includes_launcher_defaults -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/config.example.json vendors/workwechat_provider/app.py tests/test_workwechat_provider.py
git commit -m "feat: 增加企微启动器配置字段"
```

### Task 2: Support Packaged Hook Runtime Executable

**Files:**
- Modify: `vendors/workwechat_provider/app.py`
- Test: `tests/test_workwechat_provider.py`

**Step 1: Write the failing test**

Add tests for executable resolution and command building:

```python
def test_resolve_hook_runtime_executable_prefers_configured_path(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime_exe = Path(temp_dir) / "WorkWechatHookRuntime.exe"
        runtime_exe.write_bytes(b"exe")

        with patch.object(PROVIDER_APP, "config", PROVIDER_APP.ProviderConfig(hook_runtime_executable=str(runtime_exe))):
            self.assertEqual(PROVIDER_APP.resolve_hook_runtime_executable(), runtime_exe)


def test_build_hook_runtime_command_uses_packaged_exe(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime_dir = Path(temp_dir)
        runtime_exe = runtime_dir / "WorkWechatHookRuntime.exe"
        runtime_exe.write_bytes(b"exe")

        with patch.object(PROVIDER_APP, "config", PROVIDER_APP.ProviderConfig(hook_runtime_dir=str(runtime_dir))):
            command, mode = PROVIDER_APP.build_hook_runtime_command(runtime_dir)

        self.assertEqual(command, [str(runtime_exe)])
        self.assertEqual(mode, "executable")
```

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider.WorkWechatProviderTests.test_resolve_hook_runtime_executable_prefers_configured_path tests.test_workwechat_provider.WorkWechatProviderTests.test_build_hook_runtime_command_uses_packaged_exe -v`

Expected: FAIL because helper functions do not exist.

**Step 3: Write minimal implementation**

Add helpers in `vendors/workwechat_provider/app.py`:

```python
def resolve_hook_runtime_executable() -> Path | None:
    # 打包版会把 Hook runtime 编译成 exe，provider 优先启动它，避免客服机器安装 32 位 Python。
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
```

Update `start_embedded_hook_if_needed()` to call `build_hook_runtime_command(runtime_dir)`. Keep the existing `http_py.py` existence check only for `python` mode. In health result, report `"runtime_mode": mode` and keep `"python"` fields only for python mode.

**Step 4: Run tests**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/app.py tests/test_workwechat_provider.py
git commit -m "feat: 支持打包版Hook运行时启动"
```

### Task 3: Add Launcher Check Module

**Files:**
- Create: `vendors/workwechat_provider/launcher_checks.py`
- Test: `tests/test_workwechat_provider_launcher.py`

**Step 1: Write the failing test**

Create `tests/test_workwechat_provider_launcher.py`:

```python
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "vendors" / "workwechat_provider" / "launcher_checks.py"
SPEC = importlib.util.spec_from_file_location("workwechat_provider_launcher_checks", MODULE_PATH)
assert SPEC and SPEC.loader
LAUNCHER_CHECKS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LAUNCHER_CHECKS
SPEC.loader.exec_module(LAUNCHER_CHECKS)


class WorkWechatProviderLauncherTests(unittest.TestCase):
    def test_normalize_version_keeps_four_part_version(self):
        self.assertEqual(LAUNCHER_CHECKS.normalize_version("5.0.0.6008"), "5.0.0.6008")

    def test_compare_required_version_requires_exact_match(self):
        self.assertTrue(LAUNCHER_CHECKS.is_required_version("5.0.0.6008", "5.0.0.6008"))
        self.assertFalse(LAUNCHER_CHECKS.is_required_version("5.0.0.6009", "5.0.0.6008"))

    def test_find_workwechat_process_returns_empty_when_missing(self):
        with patch.object(LAUNCHER_CHECKS.psutil, "process_iter", return_value=[]):
            result = LAUNCHER_CHECKS.find_workwechat_process("WXWork.exe")

        self.assertIsNone(result)
```

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: FAIL because `launcher_checks.py` does not exist.

**Step 3: Write minimal implementation**

Create `vendors/workwechat_provider/launcher_checks.py` with detailed Chinese comments:

```python
from __future__ import annotations

import ctypes
import socket
from pathlib import Path
from typing import Any

import psutil


def normalize_version(value: str) -> str:
    # 文件版本可能带空格或额外描述，启动器只保留四段数字，便于和配置做严格比对。
    text = str(value or "").strip()
    parts = [part for part in text.split(".") if part.isdigit()]
    return ".".join(parts[:4])


def is_required_version(current: str, required: str) -> bool:
    # Hook DLL 对企业微信版本敏感，这里故意使用精确匹配，不做模糊兼容。
    return bool(required) and normalize_version(current) == normalize_version(required)


def find_workwechat_process(process_name: str) -> dict[str, Any] | None:
    # psutil 可以拿到进程 exe 路径，后续读取文件版本需要依赖这个真实路径。
    expected = process_name.lower()
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = process.info
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if str(info.get("name") or "").lower() == expected and info.get("exe"):
            return {"pid": info["pid"], "name": info["name"], "exe": info["exe"]}
    return None


def is_tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
```

Add `get_windows_file_version(path: str) -> str` using `ctypes.windll.version` and return `""` on non-Windows or read failure. Keep tests mocked for this function in later tasks.

**Step 4: Run test to verify it passes**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/launcher_checks.py tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加企微启动器检测工具"
```

### Task 4: Add Launcher Status Model

**Files:**
- Create: `vendors/workwechat_provider/launcher_state.py`
- Test: `tests/test_workwechat_provider_launcher.py`

**Step 1: Write the failing test**

Add:

```python
def test_launcher_status_builds_customer_facing_message(self):
    status = LAUNCHER_STATE.LauncherStatus(
        workwechat_state="version_mismatch",
        current_version="5.0.0.6009",
        required_version="5.0.0.6008",
    )

    self.assertIn("版本", status.summary)
    self.assertIn("5.0.0.6009", status.detail)
```

Load `launcher_state.py` in the test module the same way `launcher_checks.py` is loaded.

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: FAIL because `launcher_state.py` does not exist.

**Step 3: Write minimal implementation**

Create dataclasses:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LauncherStatus:
    workwechat_state: str = "unknown"
    provider_state: str = "stopped"
    hook_state: str = "stopped"
    callback_state: str = "unknown"
    current_version: str = ""
    required_version: str = ""
    error: str = ""

    @property
    def summary(self) -> str:
        # 摘要用于窗口主状态，必须让客服一眼知道是否能继续工作。
        if self.workwechat_state == "version_mismatch":
            return "企业微信版本不匹配"
        if self.error:
            return "启动异常"
        if self.provider_state == "running" and self.hook_state == "running":
            return "运行正常"
        return "等待启动"

    @property
    def detail(self) -> str:
        if self.workwechat_state == "version_mismatch":
            return f"当前企业微信版本是 {self.current_version}，需要 {self.required_version}。"
        if self.error:
            return self.error
        return "请打开并登录企业微信，然后点击启动。"
```

**Step 4: Run test to verify it passes**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/launcher_state.py tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加企微启动器状态模型"
```

### Task 5: Add Provider Process Controller

**Files:**
- Create: `vendors/workwechat_provider/launcher_controller.py`
- Test: `tests/test_workwechat_provider_launcher.py`

**Step 1: Write the failing test**

Add tests for command resolution:

```python
def test_provider_command_prefers_packaged_server_exe(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        server = root / "WorkWechatProviderServer.exe"
        server.write_bytes(b"exe")

        command = LAUNCHER_CONTROLLER.build_provider_command(root)

    self.assertEqual(command, [str(server)])
```

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: FAIL because `launcher_controller.py` does not exist.

**Step 3: Write minimal implementation**

Create `launcher_controller.py`:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def build_provider_command(app_root: Path) -> list[str]:
    # 打包目录中优先使用独立 provider server exe；开发环境回退到当前 Python 运行 app.py。
    server_exe = app_root / "WorkWechatProviderServer.exe"
    if server_exe.exists():
        return [str(server_exe)]
    return [sys.executable, str(app_root / "app.py")]


class ProviderProcessController:
    def __init__(self, app_root: Path):
        self.app_root = app_root
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            build_provider_command(self.app_root),
            cwd=str(self.app_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
```

**Step 4: Run test to verify it passes**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/launcher_controller.py tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加企微Provider进程控制器"
```

### Task 6: Add PySide2 Launcher Window

**Files:**
- Create: `vendors/workwechat_provider/launcher_app.py`
- Modify: `vendors/workwechat_provider/requirements.txt`

**Step 1: Write the smoke import test**

Add a test that imports `launcher_app.py` only when PySide2 is installed:

```python
def test_launcher_app_imports_when_pyside2_available(self):
    try:
        import PySide2  # noqa: F401
    except ImportError:
        self.skipTest("PySide2 is only required for Windows launcher packaging")

    module_path = Path(__file__).resolve().parents[1] / "vendors" / "workwechat_provider" / "launcher_app.py"
    spec = importlib.util.spec_from_file_location("workwechat_provider_launcher_app", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    self.assertTrue(hasattr(module, "LauncherWindow"))
```

**Step 2: Run test to verify it fails or skips**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: FAIL if PySide2 exists and file is missing, otherwise SKIP.

**Step 3: Write minimal implementation**

Create `launcher_app.py`:

```python
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide2.QtCore import QTimer
from PySide2.QtWidgets import QApplication, QLabel, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget

from launcher_checks import find_workwechat_process, get_windows_file_version, is_required_version
from launcher_controller import ProviderProcessController
from launcher_state import LauncherStatus


APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = APP_ROOT / "config.json"


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.controller = ProviderProcessController(APP_ROOT)
        self.status_label = QLabel("等待启动")
        self.detail_label = QLabel("请打开并登录企业微信，然后点击启动。")
        self.start_button = QPushButton("启动")
        self.restart_button = QPushButton("重启")
        self.log_button = QPushButton("打开日志")
        self.config_button = QPushButton("打开配置")

        # 窗口只展示客服需要理解的状态，详细技术信息统一写日志。
        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.start_button)
        layout.addWidget(self.restart_button)
        layout.addWidget(self.log_button)
        layout.addWidget(self.config_button)
        self.setLayout(layout)

        self.start_button.clicked.connect(self.start_provider)
        self.restart_button.clicked.connect(self.restart_provider)
        self.setWindowTitle("企业微信 Provider")
        self.resize(420, 220)

    def load_config(self) -> dict:
        if not CONFIG_PATH.exists():
            return {}
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    def preflight(self) -> LauncherStatus:
        cfg = self.load_config()
        required_version = str(cfg.get("required_workwechat_version") or "5.0.0.6008")
        process_name = str(cfg.get("workwechat_process_name") or "WXWork.exe")
        process = find_workwechat_process(process_name)
        if not process:
            return LauncherStatus(workwechat_state="missing", required_version=required_version, error="请先打开企业微信并登录，然后点击启动。")
        current_version = get_windows_file_version(process["exe"])
        if not is_required_version(current_version, required_version):
            return LauncherStatus(workwechat_state="version_mismatch", current_version=current_version or "未知", required_version=required_version)
        return LauncherStatus(workwechat_state="ready", current_version=current_version, required_version=required_version)

    def start_provider(self) -> None:
        status = self.preflight()
        if status.workwechat_state not in {"ready"}:
            self.render_status(status)
            return
        self.controller.start()
        self.render_status(LauncherStatus(workwechat_state="ready", provider_state="running", hook_state="running"))

    def restart_provider(self) -> None:
        self.controller.stop()
        self.start_provider()

    def render_status(self, status: LauncherStatus) -> None:
        self.status_label.setText(status.summary)
        self.detail_label.setText(status.detail)


def main() -> int:
    app = QApplication(sys.argv)
    window = LauncherWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
```

Add dependencies:

```text
PySide2>=5.15.0
psutil>=5.9.0
pyinstaller>=6.0.0
```

**Step 4: Run smoke test**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: PASS or SKIP for PySide2 smoke test on non-Windows dev environment.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/launcher_app.py vendors/workwechat_provider/requirements.txt tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加企业微信Provider小窗口入口"
```

### Task 7: Add Health Polling and Customer Status Updates

**Files:**
- Modify: `vendors/workwechat_provider/launcher_checks.py`
- Modify: `vendors/workwechat_provider/launcher_app.py`
- Test: `tests/test_workwechat_provider_launcher.py`

**Step 1: Write the failing test**

Add tests for provider health normalization:

```python
def test_normalize_provider_health_maps_ok_to_running(self):
    result = LAUNCHER_CHECKS.normalize_provider_health({"ok": True, "hook": {"ok": True}})

    self.assertEqual(result["provider_state"], "running")
    self.assertEqual(result["hook_state"], "running")
```

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: FAIL because `normalize_provider_health` does not exist.

**Step 3: Write minimal implementation**

Add `fetch_provider_health(base_url, token, timeout)` and `normalize_provider_health(payload)` to `launcher_checks.py`. The fetch function should call `/health`, pass `X-Provider-Token` when configured, and return a dict with an `"error"` key on failure.

Update `launcher_app.py`:

- Add a `QTimer` that polls health every 3 seconds after start.
- Render `Provider: 运行中` only when `/health` responds.
- Render Hook as `运行中 / 异常 / 启动中` based on normalized health.
- Keep window text short and write raw payload into logs.

**Step 4: Run tests**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: PASS or PySide2 smoke SKIP.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/launcher_checks.py vendors/workwechat_provider/launcher_app.py tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加启动器健康状态轮询"
```

### Task 8: Add Tray, Open Config, Open Logs, and Optional Autostart

**Files:**
- Modify: `vendors/workwechat_provider/launcher_app.py`
- Test: `tests/test_workwechat_provider_launcher.py`

**Step 1: Write the behavior tests**

Add pure helper tests for autostart command generation:

```python
def test_build_autostart_command_quotes_launcher_path(self):
    command = LAUNCHER_APP.build_autostart_command(Path("C:/Program Files/WorkWechatProvider/WorkWechatProvider.exe"))

    self.assertIn('"C:/Program Files/WorkWechatProvider/WorkWechatProvider.exe"', command)
```

**Step 2: Run test to verify it fails**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: FAIL because helper does not exist.

**Step 3: Write minimal implementation**

In `launcher_app.py`:

- Add `QSystemTrayIcon` with menu actions: 显示窗口、重启、打开日志、退出。
- Override close event to hide to tray by default.
- Add `open_path(path)` helper using `os.startfile` on Windows.
- Add a checkbox or menu action for autostart.
- Implement autostart using the current user's registry key:

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

Use Chinese comments around registry writes because this affects Windows startup behavior.

**Step 4: Run tests**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_provider_launcher -v`

Expected: PASS or PySide2 smoke SKIP.

**Step 5: Commit**

```bash
git add vendors/workwechat_provider/launcher_app.py tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加启动器托盘和开机自启"
```

### Task 9: Add Windows Packaging Script

**Files:**
- Create: `vendors/workwechat_provider/build-provider-launcher.bat`
- Modify: `vendors/workwechat_provider/README.md`

**Step 1: Write the script**

Create `build-provider-launcher.bat`:

```bat
@echo off
setlocal
cd /d "%~dp0"

rem 打包必须使用 32 位 Python，因为企业微信 Hook DLL 是 32 位。
set "PY32=py -3.10-32"
%PY32% -c "import struct; raise SystemExit(0 if struct.calcsize('P') * 8 == 32 else 1)"
if errorlevel 1 (
  echo [ERROR] Python 3.10 32-bit is required.
  exit /b 1
)

if not exist ".venv32\Scripts\python.exe" (
  %PY32% -m venv .venv32
  if errorlevel 1 exit /b %ERRORLEVEL%
)

".venv32\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\python.exe" -m pip install -r hook_runtime\requirements.txt
if errorlevel 1 exit /b %ERRORLEVEL%

rem 先构建内部 provider server 和 Hook runtime，再嵌入最终单文件 UI 启动器。
".venv32\Scripts\pyinstaller.exe" --noconsole --onefile --name WorkWechatProviderServer app.py
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\pyinstaller.exe" --noconsole --onefile --name WorkWechatHookRuntime hook_runtime\http_py.py
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\pyinstaller.exe" --noconsole --onefile --name WorkWechatProvider --add-binary "dist\WorkWechatProviderServer.exe;." --add-binary "dist\WorkWechatHookRuntime.exe;hook_runtime" --add-binary "hook_runtime\Loader.dll;hook_runtime" --add-binary "hook_runtime\WeChat_5.0.0.6008.dll;hook_runtime" --add-data "hook_runtime\API.docx;hook_runtime" --add-data "config.example.json;." launcher_app.py
if errorlevel 1 exit /b %ERRORLEVEL%

if exist "dist\WorkWechatProviderServer.exe" del /q "dist\WorkWechatProviderServer.exe"
if exist "dist\WorkWechatHookRuntime.exe" del /q "dist\WorkWechatHookRuntime.exe"

echo [INFO] Done: dist\WorkWechatProvider.exe
```

**Step 2: Update README**

Add a “小窗口启动器打包” section with:

- Build requires Python 3.10 32-bit on the packaging machine.
- Customer machine does not need Python.
- Customer starts `WorkWechatProvider.exe`.
- DLL cannot be fully hidden.
- If WorkWechat version mismatch appears, install the required client version.

**Step 3: Run static checks**

Run on Linux/dev box: `git diff --check`

Expected: PASS.

On Windows build box, run: `build-provider-launcher.bat`

Expected: `dist\WorkWechatProvider.exe` exists.

**Step 4: Commit**

```bash
git add vendors/workwechat_provider/build-provider-launcher.bat vendors/workwechat_provider/README.md
git commit -m "build: 增加企业微信Provider启动器打包脚本"
```

### Task 10: Final Verification

**Files:**
- No new files unless verification finds defects.

**Step 1: Run unit tests**

Run:

```bash
.venv311/bin/python -m unittest tests.test_workwechat_provider tests.test_workwechat_provider_launcher -v
```

Expected: PASS, with PySide2 smoke test skipped if PySide2 is not installed in the dev venv.

**Step 2: Run diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors. `git status --short` may be clean or only contain unrelated user files.

**Step 3: Manual Windows checks**

On a Windows machine with Python 3.10 32-bit:

1. Run `vendors\workwechat_provider\build-provider-launcher.bat`.
2. Open matching WorkWechat client version `5.0.0.6008`.
3. Double-click `dist\WorkWechatProvider.exe`.
4. Confirm window shows running status.
5. Confirm `http://127.0.0.1:8181/health` returns provider health.
6. Close the window and confirm it minimizes to tray.
7. Exit from tray and confirm provider child processes stop.

**Step 4: Commit any verification fixes**

If fixes were required:

```bash
git add <fixed-files>
git commit -m "fix: 修复企业微信Provider启动器验证问题"
```
