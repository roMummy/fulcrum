from __future__ import annotations

from pathlib import Path

import pytest

from app.services.outbound_files import resolve_tenant_share_file


VALID_SHARE_ID = "0123456789abcdef0123456789abcdef"


def _create_shared_file(root: Path, *, tenant_id: int = 11, channel_id: int = 22) -> Path:
    """在临时租户目录创建一份合法分享文件，供路径矩阵验证。"""

    target = root / str(tenant_id) / "outbound_files" / str(channel_id) / VALID_SHARE_ID / "order.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("tenant-private", encoding="utf-8")
    return target


def test_share_file_resolution_requires_exact_tenant_channel_and_token(tmp_path: Path) -> None:
    """已知文件名和分享码仍不能通过替换租户或渠道目录读取其他归属文件。"""

    expected = _create_shared_file(tmp_path)
    assert resolve_tenant_share_file(
        root=tmp_path,
        tenant_id=11,
        namespace="outbound_files",
        channel_id=22,
        share_id=VALID_SHARE_ID,
        file_name="order.txt",
    ) == expected.resolve()
    assert resolve_tenant_share_file(
        root=tmp_path,
        tenant_id=12,
        namespace="outbound_files",
        channel_id=22,
        share_id=VALID_SHARE_ID,
        file_name="order.txt",
    ) is None
    assert resolve_tenant_share_file(
        root=tmp_path,
        tenant_id=11,
        namespace="outbound_files",
        channel_id=23,
        share_id=VALID_SHARE_ID,
        file_name="order.txt",
    ) is None


@pytest.mark.parametrize(
    ("share_id", "file_name"),
    [
        ("..", "order.txt"),
        ("not-hex", "order.txt"),
        (VALID_SHARE_ID[:-1], "order.txt"),
        (VALID_SHARE_ID, "../order.txt"),
        (VALID_SHARE_ID, ".."),
        (VALID_SHARE_ID, "folder/order.txt"),
    ],
)
def test_share_file_resolution_rejects_path_manipulation(tmp_path: Path, share_id: str, file_name: str) -> None:
    """分享码、文件名和目录命名空间中的路径操纵全部返回资源不存在。"""

    _create_shared_file(tmp_path)
    assert resolve_tenant_share_file(
        root=tmp_path,
        tenant_id=11,
        namespace="outbound_files",
        channel_id=22,
        share_id=share_id,
        file_name=file_name,
    ) is None


def test_share_file_resolution_rejects_symlink_escape(tmp_path: Path) -> None:
    """即使分享目录内出现符号链接，解析后的真实路径也不能逃出租户渠道目录。"""

    share_dir = tmp_path / "11" / "inbound_files" / "22" / VALID_SHARE_ID
    share_dir.mkdir(parents=True)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside", encoding="utf-8")
    link = share_dir / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")

    assert resolve_tenant_share_file(
        root=tmp_path,
        tenant_id=11,
        namespace="inbound_files",
        channel_id=22,
        share_id=VALID_SHARE_ID,
        file_name="linked.txt",
    ) is None
