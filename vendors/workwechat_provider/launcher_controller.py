from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
import subprocess
import sys
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from launcher_paths import resolve_distribution_root


@dataclass(frozen=True)
class PortOwner:
    port: int
    pid: int
    name: str
    exe: str
    command_line: str

    @property
    def description(self) -> str:
        # UI 直接展示这个描述，排障时能快速看出是哪个进程占用了 provider/hook 端口。
        path = self.exe or self.command_line or "未知路径"
        return f"{self.port} 被 {self.name or '未知进程'}(PID {self.pid}) 占用：{path}"


class ProviderPortConflictError(RuntimeError):
    def __init__(self, conflicts: list[PortOwner]):
        self.conflicts = conflicts
        super().__init__("; ".join(owner.description for owner in conflicts))


class ProviderPortCleanupError(RuntimeError):
    def __init__(self, owner: PortOwner, reason: str):
        self.owner = owner
        self.reason = reason
        super().__init__(f"清理旧企微进程失败：{owner.description}，原因：{reason}")


def resolve_pyinstaller_bundled_root() -> Path | None:
    # onefile 启动器运行时，PyInstaller 会把内置 server/runtime 解压到 sys._MEIPASS；
    # 控制器只把它当作资源来源，不直接在 `_MEI...` 里运行子进程，避免退出时临时目录删不掉。
    bundled_root = getattr(sys, "_MEIPASS", "")
    if not bundled_root:
        return None
    bundled_path = Path(bundled_root).resolve()
    return bundled_path if bundled_path.exists() else None


def compute_runtime_build_id() -> str:
    """根据启动器 exe 元信息生成稳定 runtime 目录名。"""
    if not getattr(sys, "frozen", False):
        return "dev"
    try:
        executable = Path(sys.executable).resolve()
        stat = executable.stat()
    except OSError:
        return "unknown"
    # 使用 mtime_ns + size 足以区分不同打包产物，并避免每次启动都 hash 大 exe 影响 UI 响应。
    return f"{stat.st_mtime_ns:x}_{stat.st_size:x}"


def list_resource_files(source_root: Path) -> list[tuple[Path, Path]]:
    """列出需要从 PyInstaller 临时目录复制到稳定 runtime 的内置资源。"""
    resources: list[tuple[Path, Path]] = []
    for file_name in ("WorkWechatProviderServer.exe", "config.example.json"):
        source = source_root / file_name
        if source.exists():
            resources.append((source, Path(file_name)))

    hook_root = source_root / "hook_runtime"
    if hook_root.exists():
        for source in hook_root.rglob("*"):
            if source.is_file():
                resources.append((source, source.relative_to(source_root)))
    return resources


def runtime_copy_complete(source_root: Path, runtime_root: Path) -> bool:
    """检查稳定 runtime 是否已经完整复制，避免重复覆盖正在运行的 DLL/exe。"""
    marker = runtime_root / ".runtime-ready.json"
    if not marker.exists():
        return False
    for _, relative_path in list_resource_files(source_root):
        if not (runtime_root / relative_path).exists():
            return False
    return True


def prepare_stable_bundled_runtime(app_root: Path) -> Path | None:
    """把 onefile 内置资源复制到稳定目录，并返回 provider 可读取的资源根目录。"""
    source_root = resolve_pyinstaller_bundled_root()
    if not source_root:
        return None

    runtime_root = app_root / "runtime" / compute_runtime_build_id()
    if runtime_copy_complete(source_root, runtime_root):
        return runtime_root

    runtime_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source, relative_path in list_resource_files(source_root):
        target = runtime_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(relative_path))

    # marker 最后写入；如果中途复制失败，下次启动会重新补齐缺失文件。
    (runtime_root / ".runtime-ready.json").write_text(
        json.dumps({"build_id": compute_runtime_build_id(), "files": copied}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return runtime_root


def resolve_resource_root(app_root: Path, source_root: Path) -> Path | None:
    """解析 provider 用来寻找内置 Hook runtime 的资源根目录。"""
    stable_runtime = prepare_stable_bundled_runtime(app_root)
    if stable_runtime:
        return stable_runtime
    # 开发环境或目录交付时没有 `_MEI...`，直接让 provider 从源码/交付目录读取 hook_runtime。
    if (source_root / "hook_runtime").exists() or (source_root / "WorkWechatProviderServer.exe").exists():
        return source_root
    return None


def build_provider_command(source_root: Path, resource_root: Path | None) -> list[str]:
    # 单文件 UI 交付时，内部 server 会先复制到稳定 runtime，再从那里启动。
    stable_server = resource_root / "WorkWechatProviderServer.exe" if resource_root else None
    if stable_server and stable_server.exists():
        return [str(stable_server)]

    # 调试或目录交付时优先使用源码/交付目录里的 server，便于现场替换 server 做问题定位。
    server_exe = source_root / "WorkWechatProviderServer.exe"
    if server_exe.exists():
        return [str(server_exe)]

    # 开发环境回退到当前 Python 直接运行 app.py。
    return [sys.executable, str(source_root / "app.py")]


def load_cleanup_ports(app_root: Path) -> list[int]:
    # 端口仍以 config.json 为准，避免以后现场改 provider_port 或 hook_api_base 后清理逻辑还写死旧端口。
    config_path = app_root / "config.json"
    provider_port = 8181
    hook_port = 8989
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        config = {}
    if isinstance(config, dict):
        try:
            provider_port = int(config.get("provider_port") or provider_port)
        except (TypeError, ValueError):
            provider_port = 8181
        parsed = urlparse(str(config.get("hook_api_base") or "http://127.0.0.1:8989"))
        if parsed.port:
            hook_port = int(parsed.port)
    return sorted({provider_port, hook_port})


def normalize_process_text(value: str) -> str:
    return str(value or "").replace("\\", "/").lower()


def is_project_workwechat_process(owner: PortOwner) -> bool:
    name = str(owner.name or "").lower()
    command_line = normalize_process_text(owner.command_line)
    exe = normalize_process_text(owner.exe)
    combined = f"{command_line} {exe}"

    # 打包版只允许清理明确命名的 provider server / hook runtime，避免按端口误杀其他程序。
    if name in {"workwechatproviderserver.exe", "workwechathookruntime.exe"}:
        return True

    # 源码版通常是 python app.py / hook_runtime/http_py.py；必须同时出现项目目录特征和入口文件。
    if "workwechat_provider" not in combined:
        return False
    if combined.endswith("/app.py") or "workwechat_provider/app.py" in combined:
        return True
    if "workwechat_provider/hook_runtime/http_py.py" in combined:
        return True
    return False


def iter_listening_port_owners(ports: list[int]) -> list[PortOwner]:
    try:
        import psutil
    except ImportError:
        return []

    target_ports = {int(port) for port in ports}
    owners: list[PortOwner] = []
    seen: set[tuple[int, int]] = set()
    for connection in psutil.net_connections(kind="inet"):
        if str(getattr(connection, "status", "")).upper() != "LISTEN":
            continue
        local_address = getattr(connection, "laddr", None)
        port = int(getattr(local_address, "port", 0) or (local_address[1] if local_address else 0))
        pid = int(getattr(connection, "pid", 0) or 0)
        if port not in target_ports or not pid or (port, pid) in seen or pid == os.getpid():
            continue
        seen.add((port, pid))
        try:
            process = psutil.Process(pid)
            name = process.name()
            exe = process.exe()
            command_line = " ".join(process.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            # 读不到进程详情时不能自动清理，只保留 PID 和端口给 UI 提示人工处理。
            name = ""
            exe = ""
            command_line = ""
        owners.append(PortOwner(port=port, pid=pid, name=name, exe=exe, command_line=command_line))
    return owners


def terminate_process_tree_by_pid(pid: int, *, timeout: float = 3.0) -> None:
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("psutil_required_for_port_cleanup") from exc

    try:
        parent = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    processes = children + [parent]
    # PyInstaller onefile 会出现“引导父进程 + 真正运行子进程”两个同名进程；
    # 这里必须按整棵树回收，不能只停 Popen 返回的父进程，否则 8181/8989 监听子进程会残留。
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            continue
    _gone, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
    if alive:
        _gone, alive = psutil.wait_procs(alive, timeout=timeout)
    if alive:
        remaining = ", ".join(f"{process.name()}(PID {process.pid})" for process in alive)
        raise RuntimeError(f"processes_still_alive_after_kill: {remaining}")


def wait_for_ports_released(ports: list[int], *, timeout: float = 5.0, interval: float = 0.2) -> list[PortOwner]:
    """等待目标端口释放，并返回超时后仍在监听的进程。

    Windows 上进程退出到 TCP 监听释放之间可能有短暂延迟；重启流程如果不等待，
    新 provider 会立刻尝试绑定 8181，从而出现“旧进程已杀但端口仍冲突”的现场提示。
    """
    deadline = time.monotonic() + max(0.1, float(timeout))
    remaining: list[PortOwner] = []
    while time.monotonic() < deadline:
        remaining = iter_listening_port_owners(ports)
        if not remaining:
            return []
        time.sleep(max(0.05, float(interval)))
    return iter_listening_port_owners(ports) or remaining


def cleanup_project_port_owners(app_root: Path) -> list[PortOwner]:
    ports = load_cleanup_ports(app_root)
    owners = iter_listening_port_owners(ports)
    project_owners = [owner for owner in owners if is_project_workwechat_process(owner)]
    conflicts = [owner for owner in owners if not is_project_workwechat_process(owner)]
    if conflicts:
        raise ProviderPortConflictError(conflicts)

    for owner in project_owners:
        try:
            terminate_process_tree_by_pid(owner.pid)
        except Exception as exc:
            # 只要旧进程没能确定释放端口，就不要继续启动新 provider；否则会回到多个实例抢端口的状态。
            raise ProviderPortCleanupError(owner, str(exc)) from exc
    if project_owners:
        remaining = wait_for_ports_released(ports)
        remaining_conflicts = [owner for owner in remaining if not is_project_workwechat_process(owner)]
        if remaining_conflicts:
            raise ProviderPortConflictError(remaining_conflicts)
        if remaining:
            # 旧 provider/hook 已经发出终止信号但端口仍未释放，继续启动只会再次撞 8181/8989。
            raise ProviderPortCleanupError(remaining[0], "端口在回收后仍未释放")
    return project_owners


class ProviderProcessController:
    def __init__(self, app_root: Path, source_root: Path | None = None):
        self.app_root = app_root
        self.source_root = source_root or resolve_distribution_root()
        self.resource_root: Path | None = None
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        # 已经运行时不重复拉起，避免客服连续点击造成多个 provider 抢同一个端口。
        if self.process and self.process.poll() is None:
            return
        self.app_root.mkdir(parents=True, exist_ok=True)
        cleanup_project_port_owners(self.app_root)
        self.resource_root = resolve_resource_root(self.app_root, self.source_root)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["WORKWECHAT_PROVIDER_APP_ROOT"] = str(self.app_root)
        if self.resource_root:
            env["WORKWECHAT_PROVIDER_BUNDLED_ROOT"] = str(self.resource_root)
        self.process = subprocess.Popen(
            build_provider_command(self.source_root, self.resource_root),
            cwd=str(self.app_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def stop(self) -> None:
        # 打包版 Popen 拿到的可能只是 PyInstaller 引导父进程；
        # 重启时必须回收整棵子树，并稍等端口释放，避免真实监听子进程继续占用 8181。
        if self.process and self.process.poll() is None:
            terminate_process_tree_by_pid(self.process.pid, timeout=8)
        self.process = None
        try:
            # 如果 UI 进程曾经异常恢复或 Popen 引用已经失效，仍要按端口兜底清理残留的本项目实例。
            cleanup_project_port_owners(self.app_root)
        except (ProviderPortCleanupError, ProviderPortConflictError):
            # stop() 也用于关闭窗口/退出托盘；这里做最佳努力回收，真正的启动前校验仍由 start() 严格报错。
            return

    def terminate_process_tree(self, pid: int) -> None:
        """兜底回收 provider 子进程树，避免 Hook runtime 继续占用稳定 runtime 文件。"""
        try:
            import psutil
        except ImportError:
            self.process.kill()
            return

        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        children = parent.children(recursive=True)
        for process in children:
            process.terminate()
        parent.kill()
        psutil.wait_procs(children + [parent], timeout=3)
