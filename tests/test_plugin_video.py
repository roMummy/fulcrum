from __future__ import annotations

from app.services.plugin_runtime.contracts import PluginExecutionRequest
from plugins.video import plugin as video
from plugins.video import service as video_service
from plugins.video.service import VideoResult, load_config


def test_video_config_uses_bounded_defaults() -> None:
    config = load_config({"enabled": True, "max_video_bytes": 999999999})
    assert config.max_video_bytes == 40 * 1024 * 1024
    assert config.points_cost == 8
    assert config.video_url.startswith("http")


def test_video_reload_does_not_download() -> None:
    result = video.execute_action(PluginExecutionRequest(action="reload", config={"enabled": True}))
    assert result.ok is True
    assert result.output["max_video_seconds"] == 180


def test_video_disabled_returns_business_status() -> None:
    result = video.execute_action(PluginExecutionRequest(action="execute", config={"enabled": False}))
    assert result.ok is False
    assert result.status == "trusted_context_required"


def test_video_matcher_respects_enabled_flag() -> None:
    class Context:
        content = "黑丝"
        binding_config = {"enabled": True}

    assert video.matches_event(Context())["matched"] is True


def test_video_result_shape_is_explicit() -> None:
    result = VideoResult(video_base64="dm", cover_base64="an", duration_seconds=12)
    assert result.duration_seconds == 12
    assert result.video_base64 == "dm"


def test_fetch_video_cleans_tenant_temp_directory(monkeypatch, tmp_path) -> None:
    temp_dir = tmp_path / "job"
    temp_dir.mkdir()

    monkeypatch.setattr(video_service, "create_tenant_temp_dir", lambda tenant_id, plugin_code: temp_dir)
    monkeypatch.setattr(
        video_service,
        "download_media",
        lambda *args, **kwargs: (b"\x00\x00\x00\x18ftypmp42video", "video/mp4"),
    )
    monkeypatch.setattr(video_service, "probe_video_duration", lambda path: 2.0)

    def fake_cover(video_path, cover_path):  # noqa: ANN001
        cover_path.write_bytes(b"\xff\xd8\xffcover")
        return cover_path

    monkeypatch.setattr(video_service, "extract_video_cover", fake_cover)

    result = video_service.fetch_video(object(), 7, load_config({"enabled": True}))

    assert result.duration_seconds == 2
    assert result.video_base64
    assert not temp_dir.exists()

