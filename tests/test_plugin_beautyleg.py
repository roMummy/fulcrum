from __future__ import annotations

from pathlib import Path

from app.services.plugin_runtime.contracts import PluginExecutionRequest
from plugins.beautyleg import plugin as beautyleg
from plugins.beautyleg.service import build_image_reply, load_config, select_random_image


def test_beautyleg_config_keeps_resource_path_relative() -> None:
    config = load_config({"enabled": True, "resource_dir": "/mnt/nfs_share/beauty_leg"})
    assert config.resource_dir == "images"
    assert config.points_cost == 2


def test_beautyleg_selects_only_tenant_owned_images(monkeypatch, tmp_path: Path) -> None:
    resource_root = tmp_path / "tenant" / "images"
    resource_root.mkdir(parents=True)
    image = resource_root / "one.jpg"
    image.write_bytes(b"\xff\xd8\xffimage")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (resource_root / "escape.jpg").symlink_to(outside)
    monkeypatch.setattr(beautyleg, "select_random_image", select_random_image)
    monkeypatch.setattr("plugins.beautyleg.service.tenant_media_root", lambda tenant_id, code: tmp_path / "tenant")

    selected = select_random_image(7, load_config({"enabled": True}), chooser=lambda values: values[0])

    assert selected == image.resolve()


def test_beautyleg_builds_standard_image_reply(tmp_path: Path) -> None:
    resource_root = tmp_path / "tenant" / "images"
    resource_root.mkdir(parents=True)
    image = resource_root / "one.jpg"
    image.write_bytes(b"\xff\xd8\xffimage")

    reply = build_image_reply(image, load_config({"enabled": True}))

    assert reply["type"] == "image"
    assert reply["base64"]
    assert "image_path" not in reply


def test_beautyleg_matches_enabled_command() -> None:
    class Context:
        content = "腿来"
        binding_config = {"enabled": True}

    result = beautyleg.matches_event(Context())
    assert result["matched"] is True


def test_beautyleg_reload_does_not_require_message_context() -> None:
    result = beautyleg.execute_action(PluginExecutionRequest(action="reload", config={"enabled": True}))
    assert result.ok is True
    assert result.output["points_cost"] == 2
