"""插件包的安装、升级、回滚(热插拔):包校验、原子替换、历史版本存档、平台上传路由。"""

from __future__ import annotations

import io
import os
import stat
import tempfile
import zipfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="plugin-packages-logs-"))

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.web.platform_operations as platform_operations_web
import app.web.platform_plugins as platform_plugins_web
from app.core.db import Base
from app.core.settings import settings
from app.models.entities import Plugin
from app.models.saas import PlatformAuditLog, PlatformUser
from app.services import plugin_directory, plugin_packages
from app.services.plugin_packages import PluginPackageError
from app.services.saas_auth import create_platform_session, hash_password, load_platform_principal
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE


def _manifest(code: str = "demo", version: str = "1.0.0") -> str:
    return f"""
[plugin]
code = "{code}"
display_name = "示例"
version = "{version}"
[capability]
channel_types = ["simulator"]
chat_types = ["group"]
message_types = ["text"]
[matcher]
commands = ["示例"]
"""


def _zip(files: dict[str, str | bytes], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def _package(version: str = "1.0.0", *, code: str = "demo", marker: str = "v1", wrapped: bool = True) -> bytes:
    prefix = f"{code}/" if wrapped else ""
    return _zip({f"{prefix}config.toml": _manifest(code, version), f"{prefix}plugin.py": f"MARKER = {marker!r}\ndef execute_action(request):\n    return None\n"})


@pytest.fixture()
def plugins_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins_dir", str(tmp_path))
    plugin_directory._SCAN_CACHE.clear()
    yield tmp_path
    plugin_directory._SCAN_CACHE.clear()


def _staging_leftovers(root) -> list[str]:
    staging = root / "_staging"
    return sorted(path.name for path in staging.iterdir()) if staging.exists() else []


def _marker(root, code: str = "demo") -> str:
    return (root / code / "plugin.py").read_text(encoding="utf-8").splitlines()[0]


# ---------- 安装 / 升级 / 回滚 ----------

def test_install_then_upgrade_then_rollback_keeps_every_version_recoverable(plugins_dir) -> None:
    installed = plugin_packages.install_or_upgrade(_package("1.0.0", marker="v1"))
    assert (installed.action, installed.code, installed.from_version, installed.to_version) == ("install", "demo", None, "1.0.0")
    assert "v1" in _marker(plugins_dir) and plugin_packages.list_versions("demo") == []  # 首次安装没有旧版本

    upgraded = plugin_packages.install_or_upgrade(_package("2.0.0", marker="v2", wrapped=False))  # 目录包与平铺包都支持
    assert (upgraded.action, upgraded.from_version, upgraded.to_version) == ("upgrade", "1.0.0", "2.0.0")
    assert "v2" in _marker(plugins_dir)
    (stored,) = plugin_packages.list_versions("demo")
    assert stored.version == "1.0.0"

    back = plugin_packages.rollback("demo", stored.filename)
    assert (back.action, back.from_version, back.to_version) == ("rollback", "2.0.0", "1.0.0")
    assert "v1" in _marker(plugins_dir)
    assert sorted(item.version for item in plugin_packages.list_versions("demo")) == ["1.0.0", "2.0.0"]  # 回滚前的 2.0.0 也存档了,可以再撤销
    assert plugin_directory.get_discovered_plugin("demo", force=True).manifest.version == "1.0.0"
    assert _staging_leftovers(plugins_dir) == []  # 暂存区清理干净


def test_rollback_only_accepts_listed_archives(plugins_dir) -> None:
    plugin_packages.install_or_upgrade(_package("1.0.0"))
    for bad in ("../../etc/passwd", "20260101000000-v9.9.9.zip", ""):
        with pytest.raises(PluginPackageError):
            plugin_packages.rollback("demo", bad)


def test_upgrade_takes_effect_in_the_running_process_without_restart(plugins_dir) -> None:
    plugin_packages.install_or_upgrade(_package("1.0.0", marker="v1"))
    assert plugin_directory.load_directory_plugin("demo").instance is not None
    first = plugin_directory._MODULE_CACHE[str(plugins_dir / "demo" / "plugin.py")][1].module_name
    plugin_packages.install_or_upgrade(_package("2.0.0", marker="v2"))
    plugin_directory.load_directory_plugin("demo")
    second = plugin_directory._MODULE_CACHE[str(plugins_dir / "demo" / "plugin.py")][1].module_name
    assert first != second  # 重新加载了新代码


# ---------- 包校验:被拒绝时正式目录不受影响 ----------

BAD_PACKAGES = {
    "不是 zip": (b"not a zip", "不是有效的 zip"),
    "空文件": (b"", "空的"),
    "没有 config.toml": (_zip({"demo/plugin.py": "x=1"}), "config.toml"),
    "路径穿越": (_zip({"../evil/config.toml": _manifest(), "../evil/plugin.py": "x=1"}), "不安全的路径"),
    "绝对路径": (_zip({"/etc/config.toml": _manifest()}), "不安全的路径"),
    "符号链接": (_zip({"demo/config.toml": _manifest(), "demo/plugin.py": "x=1"}, symlink="demo/link"), "符号链接"),
    "目录之外还有文件": (_zip({"demo/config.toml": _manifest(), "demo/plugin.py": "x=1", "other.txt": "x"}), "其他内容"),
    "编码不合法": (_zip({"config.toml": _manifest("Bad-Code"), "plugin.py": "x=1"}), "plugin.code"),
    "下划线开头的编码": (_zip({"config.toml": _manifest("_shared"), "plugin.py": "x=1"}), "plugin.code"),
    "缺少入口": (_zip({"config.toml": _manifest()}), "入口"),
    "清单无效": (_zip({"config.toml": _manifest() + '\n[[config.fields]]\nkey = "a"\nlabel = "x"\nkind = "color"\n', "plugin.py": "x=1"}), "清单无效"),
    "语法错误": (_zip({"config.toml": _manifest(), "plugin.py": "def broken(:\n"}), "语法错误"),
    "TOML 损坏": (_zip({"config.toml": "[plugin\ncode =", "plugin.py": "x=1"}), "解析失败"),
}


@pytest.mark.parametrize("name", list(BAD_PACKAGES))
def test_bad_packages_are_rejected_and_the_live_plugin_is_untouched(plugins_dir, name) -> None:
    plugin_packages.install_or_upgrade(_package("1.0.0", marker="v1"))
    data, expected = BAD_PACKAGES[name]
    with pytest.raises(PluginPackageError, match=expected):
        plugin_packages.install_or_upgrade(data)
    assert "v1" in _marker(plugins_dir)
    assert not (plugins_dir / "evil").exists() and not (plugins_dir.parent / "evil").exists()
    assert _staging_leftovers(plugins_dir) == []  # 暂存区不留残渣


def test_size_limits_are_enforced(plugins_dir, monkeypatch) -> None:
    monkeypatch.setattr(plugin_packages, "MAX_PACKAGE_BYTES", 100)
    with pytest.raises(PluginPackageError, match="上限"):
        plugin_packages.install_or_upgrade(_package())
    monkeypatch.undo()
    monkeypatch.setattr(settings, "plugins_dir", str(plugins_dir))
    monkeypatch.setattr(plugin_packages, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(PluginPackageError, match="体积"):
        plugin_packages.install_or_upgrade(_package())
    monkeypatch.undo()
    monkeypatch.setattr(settings, "plugins_dir", str(plugins_dir))
    monkeypatch.setattr(plugin_packages, "MAX_FILES", 1)
    with pytest.raises(PluginPackageError, match="文件数"):
        plugin_packages.install_or_upgrade(_package())


# ---------- 平台上传路由 ----------

def test_platform_upload_and_rollback_routes(plugins_dir, tmp_path_factory, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('db') / 'packages.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        admin = PlatformUser(username="pkg-admin", display_name="管理员", password_hash=hash_password("Package-admin-123"), role="platform_admin", is_enabled=True, auth_version=1, must_change_password=False)
        db.add(admin)
        db.commit()
        db.refresh(admin)
        session = create_platform_session(db, admin)
    monkeypatch.setattr(platform_plugins_web, "SessionLocal", factory)
    monkeypatch.setattr(platform_operations_web, "SessionLocal", factory)
    app = FastAPI()

    @app.middleware("http")
    async def restore(request: Request, call_next):
        with factory() as db:
            request.state.platform_principal = load_platform_principal(db, request.cookies.get(PLATFORM_SESSION_COOKIE))
        return await call_next(request)

    app.include_router(platform_plugins_web.router)
    csrf = session.raw_csrf_token

    def upload(client, data: bytes, token: str = csrf):
        return client.post("/platform/plugins/upload", data={"csrf_token": token}, files={"package": ("p.zip", data, "application/zip")})

    with TestClient(app, follow_redirects=False) as client:
        assert upload(client, _package()).status_code == 401  # 未登录
        client.cookies.set(PLATFORM_SESSION_COOKIE, session.raw_token)
        client.cookies.set(PLATFORM_CSRF_COOKIE, csrf)

        assert upload(client, _package(), token="forged").status_code == 403
        assert not (plugins_dir / "demo").exists()  # 伪造令牌什么也没装

        bad = upload(client, b"junk")
        assert bad.status_code == 303 and "tone=danger" in bad.headers["location"]

        assert upload(client, _package("1.0.0", marker="v1")).status_code == 303
        with factory() as db:
            demo = db.scalar(select(Plugin).where(Plugin.code == "demo"))
            assert demo.visibility == "platform_only" and demo.version == "1.0.0"  # 新装默认仅平台可见

        assert upload(client, _package("2.0.0", marker="v2")).status_code == 303
        with factory() as db:
            demo = db.scalar(select(Plugin).where(Plugin.code == "demo"))
            assert demo.version == "2.0.0" and demo.visibility == "platform_only"
        (stored,) = plugin_packages.list_versions("demo")

        page = client.get("/platform/plugins")
        assert "历史版本 1" in page.text and stored.filename in page.text  # 卡片上有历史版本入口,版本数据随卡片带给共用弹窗

        assert client.post("/platform/plugins/demo/rollback", data={"csrf_token": "forged", "version_file": stored.filename}).status_code == 403
        assert "v2" in _marker(plugins_dir)
        assert client.post("/platform/plugins/demo/rollback", data={"csrf_token": csrf, "version_file": stored.filename}).status_code == 303
        assert "v1" in _marker(plugins_dir)
        with factory() as db:
            assert db.scalar(select(Plugin.version).where(Plugin.code == "demo")) == "1.0.0"
            actions = db.scalars(select(PlatformAuditLog.action).order_by(PlatformAuditLog.id)).all()
            assert [item for item in actions if item.startswith("plugin.") and item != "plugin.visibility"] == ["plugin.install", "plugin.upgrade", "plugin.rollback"]
