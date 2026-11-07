from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path

from plugins.xiuren_image import service as xiuren_service
from plugins.xiuren_image.service import CachedImage, load_config
from app.services.plugin_runtime.scheduler import _build_plugin_schedule_specs


def test_xiuren_config_rejects_absolute_resource_directory() -> None:
    config = load_config({"enabled": True, "resource_dir": "/mnt/nfs_share", "cache_size": 999})
    assert config.resource_dir == "images"
    assert config.cache_size == 100
    assert config.points_cost == 5


def test_choose_cached_image_reads_only_current_tenant(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "tenant" / "images"
    root.mkdir(parents=True)
    image = root / "one.img"
    image.write_bytes(b"\xff\xd8\xffimage")
    monkeypatch.setattr("plugins.xiuren_image.service.tenant_media_root", lambda tenant_id, code: tmp_path / "tenant")
    config = load_config({"enabled": True})
    selected = xiuren_service.choose_cached_image(7, config, {"files": [{"path": "one.img", "digest": "abc", "source_url": "https://example.test"}]}, chooser=lambda values: values[0])
    assert selected is not None
    assert selected[0].startswith(b"\xff\xd8\xff")
    assert isinstance(selected[1], CachedImage)


def test_refresh_without_sources_preserves_existing_cache() -> None:
    state = {"files": [{"path": "one.img", "digest": "abc", "source_url": ""}]}
    result = xiuren_service.refresh_cached_images(object(), 7, load_config({"enabled": True}), state)
    assert result["files"] == state["files"]
    assert result["downloaded"] == 0


def test_xiuren_schedule_is_disabled_without_explicit_refresh_configuration() -> None:
    current = datetime(2026, 11, 7, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _build_plugin_schedule_specs("xiuren_image", {"enabled": True, "refresh_enabled": False}, current) == ()
    specs = _build_plugin_schedule_specs(
        "xiuren_image",
        {"enabled": True, "refresh_enabled": True, "source_urls": ["https://example.test"], "refresh_interval_seconds": 3600},
        current,
    )
    assert len(specs) == 1
    assert specs[0].action_key == "refresh_resources"

