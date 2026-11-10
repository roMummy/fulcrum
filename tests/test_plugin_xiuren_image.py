"""秀人图片插件:从图片目录随机预取一批再发送(不依赖外部服务)。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.core.settings import settings
from plugins.xiuren_image import service as xs
from plugins.xiuren_image.service import load_config

# 1x1 的有效 PNG,能通过插件的图片类型校验。
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


@pytest.fixture()
def library(monkeypatch, tmp_path: Path) -> Path:
    """图片库根目录(模拟挂载进容器的共享盘),里面有 xiuren 子目录。"""

    root = tmp_path / "library"
    (root / "xiuren").mkdir(parents=True)
    monkeypatch.setattr(settings, "media_library_root", str(root))
    return root


def _images(directory: Path, count: int, *, start: int = 0) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(start, start + count):
        (directory / f"{i:03d}.png").write_bytes(PNG)
        names.append(f"{i:03d}.png")
    return names


# ---------- 配置与目录 ----------

def test_config_defaults_and_directory_must_stay_relative() -> None:
    assert load_config({}).library_dir == "xiuren" and load_config({}).prefetch_count == 20
    for bad in ("../other", "a/../../b"):
        assert load_config({"library_dir": bad}).library_dir == "xiuren"
    # 开头的斜杠会被去掉,当作相对路径处理,不会变成绝对路径
    assert load_config({"library_dir": "/美图/一期/"}).library_dir == "美图/一期"
    assert load_config({"library_dir": "美图/一期"}).library_dir == "美图/一期"
    assert load_config({"prefetch_count": 9999}).prefetch_count == 100


def test_library_root_uses_platform_root_or_falls_back_to_tenant_dir(monkeypatch, tmp_path: Path) -> None:
    config = load_config({"library_dir": "pics"})
    monkeypatch.setattr(settings, "media_library_root", "/media-library")
    assert xs.library_root(7, config) == Path("/media-library/pics")
    monkeypatch.setattr(settings, "media_library_root", "")
    monkeypatch.setattr(xs, "tenant_media_root", lambda tenant_id, code: tmp_path / f"t{tenant_id}")
    assert xs.library_root(7, config) == tmp_path / "t7" / "pics"


# ---------- 扫描与预取 ----------

def test_scan_filters_non_images_empty_oversized_and_escaping_links(library: Path, tmp_path: Path) -> None:
    root = library / "xiuren"
    _images(root, 2)
    _images(root / "子目录", 1, start=10)
    (root / "readme.txt").write_text("x")
    (root / "empty.png").write_bytes(b"")
    (root / "big.png").write_bytes(PNG * 1000)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (root / "escape.png").symlink_to(outside)  # 指到目录外的链接
    found = xs.scan_images(root, load_config({"max_image_bytes": 100}))
    assert sorted(found) == ["000.png", "001.png", "子目录/010.png"]
    assert xs.scan_images(library / "不存在", load_config({})) == []


def test_refill_samples_prefetch_count_paths_without_copying(library: Path) -> None:
    _images(library / "xiuren", 30)
    pool = xs.refill_pool(7, load_config({"prefetch_count": 20}))
    assert len(pool) == 20 and len(set(pool)) == 20
    # 图片少于预取张数时全部入池;目录为空返回空池
    assert len(xs.refill_pool(7, load_config({"prefetch_count": 50}))) == 30
    assert xs.refill_pool(7, load_config({"library_dir": "空目录"})) == []


# ---------- 从池里取图 ----------

def test_pick_returns_image_and_pool_without_it(library: Path) -> None:
    names = _images(library / "xiuren", 3)
    data, chosen, remaining = xs.pick_from_pool(7, load_config({}), names, chooser=lambda items: items[1])
    assert data == PNG and chosen == names[1] and remaining == [names[0], names[2]]
    # 取用不改动磁盘上的图片
    assert len(list((library / "xiuren").glob("*.png"))) == 3


def test_pick_drops_broken_missing_and_escaping_entries(library: Path, tmp_path: Path) -> None:
    root = library / "xiuren"
    (root / "bad.png").write_bytes(b"not an image")
    good = _images(root, 1)[0]
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (root / "link.png").symlink_to(outside)
    pool = ["missing.png", "bad.png", "link.png", "../outside.png", good]
    picked = xs.pick_from_pool(7, load_config({}), pool, chooser=lambda items: items[0])
    assert picked is not None and picked[1] == good and picked[2] == []
    assert xs.pick_from_pool(7, load_config({}), ["missing.png", "../outside.png"]) is None
    assert xs.pick_from_pool(7, load_config({}), []) is None


# ---------- 插件入口完整流程 ----------

class _Env:
    """把限流、状态存储、积分替换成内存替身。"""

    def __init__(self, monkeypatch, *, points_ok: bool = True):
        from plugins.xiuren_image import plugin as xp

        self.xp, self.spent, self.state = xp, [], {}
        outer = self
        monkeypatch.setattr(xp, "require_tenant_id", lambda: 7)

        class _Decision:
            allowed = True
            retry_after_seconds = 0

        class _Limiter:
            def __init__(self, *a): pass
            def consume(self, *a, **k): return _Decision()

        class _Store:
            def __init__(self, *a): pass
            def get(self, ns, key): return outer.state or None
            def put(self, ns, key, value): outer.state = value

        class _Points:
            def __init__(self, db): pass
            def spend(self, ctx, cost, **kw):
                outer.spent.append(cost)
                return type("R", (), {"ok": points_ok, "message": "积分不足"})()

        monkeypatch.setattr(xp, "PluginRateLimiter", _Limiter)
        monkeypatch.setattr(xp, "PluginStateStore", _Store)
        monkeypatch.setattr(xp, "PointsService", _Points)

    def run(self, config: dict | None = None):
        from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest

        context = PluginExecutionContext(
            tenant_id=7, binding_id=1, bot_id=1, channel_id=1, conversation_id=3, sender_id="u1", message_id=9,
            channel_type="wechat_webridge", chat_type="group", message_type="text",
        )
        return self.xp.execute_message_action(None, PluginExecutionRequest(action="execute", config=config or {}, context=context))


def test_flow_empty_library_replies_clearly_without_charging(library: Path, monkeypatch) -> None:
    env = _Env(monkeypatch)
    result = env.run()
    assert result.ok and result.output["provider_status"] == "library_empty" and env.spent == []


def test_flow_first_command_fills_pool_then_each_command_takes_one(library: Path, monkeypatch) -> None:
    _images(library / "xiuren", 30)
    env = _Env(monkeypatch)
    first = env.run({"prefetch_count": 20})
    assert first.ok and first.output["replies"][0]["type"] == "image"
    assert env.spent == [5] and len(env.state["pool"]) == 19  # 预取 20 张,发走 1 张
    for _ in range(19):
        assert env.run({"prefetch_count": 20}).ok
    assert env.state["pool"] == []  # 这一批 20 张发完,没有重复发送
    # 池空后下一条命令自动再抽一批
    assert env.run({"prefetch_count": 20}).ok and len(env.state["pool"]) == 19


def test_flow_keeps_the_image_when_points_are_insufficient(library: Path, monkeypatch) -> None:
    _images(library / "xiuren", 3)
    env = _Env(monkeypatch, points_ok=False)
    result = env.run()
    assert not result.ok and result.output["provider_status"] == "insufficient_points"
    assert len(env.state["pool"]) == 3  # 一张没少


def test_flow_zero_cost_skips_points(library: Path, monkeypatch) -> None:
    _images(library / "xiuren", 2)
    env = _Env(monkeypatch)
    assert env.run({"points_cost": 0}).ok and env.spent == []
