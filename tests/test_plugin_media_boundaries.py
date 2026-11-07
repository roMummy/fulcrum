from __future__ import annotations

from pathlib import Path

import pytest

from plugins._shared.media import (
    IMAGE_TYPES,
    MediaBoundaryError,
    extract_video_cover,
    read_media_base64,
    validate_media_bytes,
)
from plugins._shared.resources import (
    ResourceBoundaryError,
    iter_owned_files,
    resolve_owned_path,
)


def test_owned_path_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "tenant"
    root.mkdir()
    (root / "inside.jpg").write_bytes(b"\xff\xd8\xffdata")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (root / "escape.jpg").symlink_to(outside)

    assert resolve_owned_path(root, "inside.jpg") == (root / "inside.jpg").resolve()
    with pytest.raises(ResourceBoundaryError):
        resolve_owned_path(root, "../outside.jpg")
    with pytest.raises(ResourceBoundaryError):
        resolve_owned_path(root, "escape.jpg")


def test_media_bytes_enforce_type_and_size() -> None:
    data, content_type = validate_media_bytes(
        b"\xff\xd8\xffimage",
        declared_type=None,
        allowed_types=IMAGE_TYPES,
        max_bytes=32,
    )
    assert data.startswith(b"\xff\xd8\xff")
    assert content_type == "image/jpeg"
    with pytest.raises(MediaBoundaryError):
        validate_media_bytes(b"text", declared_type="text/plain", allowed_types=IMAGE_TYPES, max_bytes=32)
    with pytest.raises(MediaBoundaryError):
        validate_media_bytes(b"123456", declared_type="image/jpeg", allowed_types=IMAGE_TYPES, max_bytes=5)


def test_read_media_base64_only_reads_authorized_image(tmp_path: Path) -> None:
    root = tmp_path / "tenant"
    root.mkdir()
    image = root / "a.jpg"
    image.write_bytes(b"\xff\xd8\xffimage")
    encoded, content_type = read_media_base64(
        image,
        allowed_root=root,
        max_bytes=32,
        allowed_types=IMAGE_TYPES,
    )
    assert encoded
    assert content_type == "image/jpeg"
    with pytest.raises(MediaBoundaryError):
        read_media_base64(tmp_path / "missing.jpg", allowed_root=root, max_bytes=32, allowed_types=IMAGE_TYPES)


def test_iter_owned_files_ignores_escaped_symlink(tmp_path: Path) -> None:
    root = tmp_path / "tenant"
    root.mkdir()
    (root / "inside.jpg").write_bytes(b"ok")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (root / "outside.jpg").symlink_to(outside)
    files = iter_owned_files(root, suffixes={".jpg"})
    assert files == ((root / "inside.jpg").resolve(),)


def test_extract_video_cover_reports_missing_or_failed_runtime(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    cover = tmp_path / "cover.jpg"
    video.write_bytes(b"video")

    class Failed:
        returncode = 1

    def runner(*args, **kwargs):  # noqa: ANN001
        return Failed()

    with pytest.raises(MediaBoundaryError, match="首帧生成失败"):
        extract_video_cover(video, cover, runner=runner)
