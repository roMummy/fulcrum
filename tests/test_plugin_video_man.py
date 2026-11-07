from __future__ import annotations

from app.services.plugin_runtime.contracts import PluginExecutionRequest
from plugins.video_man import plugin as video_man
from plugins.video_man import service as video_service
from plugins.video_man.service import load_config


def test_video_man_config_preserves_original_command_set() -> None:
    config = load_config({"enabled": True})
    assert config.commands == ("猛男", "肌肉", "帅哥")
    assert config.points_cost == 2


def test_video_man_resolves_api_payload() -> None:
    assert video_service._resolve_video_url({"data": {"video": "https://example.test/video.mp4"}}).endswith("video.mp4")


def test_video_man_rejects_invalid_api_payload() -> None:
    try:
        video_service._resolve_video_url({"data": {"video": "file:///tmp/video"}})
    except ValueError as exc:
        assert "有效地址" in str(exc)
    else:  # pragma: no cover - 仅用于保证失败路径必须抛出稳定错误。
        raise AssertionError("无效视频地址未被拒绝")


def test_video_man_reload_does_not_call_provider() -> None:
    result = video_man.execute_action(PluginExecutionRequest(action="reload", config={"enabled": True}))
    assert result.ok is True
    assert result.output["points_cost"] == 2


def test_video_man_matcher_respects_enabled_flag() -> None:
    class Context:
        content = "肌肉"
        binding_config = {"enabled": True}

    assert video_man.matches_event(Context())["matched"] is True
