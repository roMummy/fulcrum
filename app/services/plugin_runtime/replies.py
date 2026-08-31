from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


MAX_REPLY_COUNT = 8
MAX_TEXT_LENGTH = 20_000
MAX_IMAGE_BASE64_LENGTH = 16 * 1024 * 1024
MAX_VIDEO_BASE64_LENGTH = 40 * 1024 * 1024
MAX_FILE_NAME_LENGTH = 255
MAX_LINK_TITLE_LENGTH = 200
MAX_LINK_DESCRIPTION_LENGTH = 1_000
ALLOWED_REPLY_TYPES = {"text", "image", "file", "video", "link"}


class PluginReplyError(ValueError):
    """插件回复类型、内容或资源归属不符合平台标准。"""


def text_reply(content: str) -> dict[str, str]:
    """构造非空文本回复，并限制单条消息长度。"""

    normalized = str(content or "").strip()
    if not normalized:
        raise PluginReplyError("文本回复不能为空")
    if len(normalized) > MAX_TEXT_LENGTH:
        raise PluginReplyError("文本回复长度超过平台限制")
    return {"type": "text", "content": normalized}


def image_reply(
    *,
    base64_data: str | None = None,
    image_path: str | Path | None = None,
    allowed_root: str | Path | None = None,
) -> dict[str, str]:
    """构造图片回复；本地文件必须明确位于租户授权目录内。"""

    supplied = int(bool(str(base64_data or "").strip())) + int(image_path is not None)
    if supplied != 1:
        raise PluginReplyError("图片回复必须且只能提供 base64 或本地文件")
    if base64_data is not None and str(base64_data).strip():
        normalized = _validate_base64(
            str(base64_data).strip(),
            max_length=MAX_IMAGE_BASE64_LENGTH,
            label="图片",
        )
        return {"type": "image", "base64": normalized}
    resolved_path = _validate_owned_file(image_path, allowed_root=allowed_root)
    return {"type": "image", "image_path": str(resolved_path)}


def file_reply(*, file_name: str, file_url: str) -> dict[str, str]:
    """构造远程文件回复，只允许 HTTP/HTTPS 下载地址。"""

    normalized_name = str(file_name or "").strip()
    if (
        not normalized_name
        or len(normalized_name) > MAX_FILE_NAME_LENGTH
        or "/" in normalized_name
        or "\\" in normalized_name
    ):
        raise PluginReplyError("文件名不合法")
    normalized_url = _validate_public_resource_url(file_url)
    return {"type": "file", "file_name": normalized_name, "file_url": normalized_url}


def link_reply(
    *,
    url: str,
    title: str = "",
    description: str = "",
    thumb_url: str = "",
) -> dict[str, str]:
    """构造渠道无关的链接卡片，协议格式由各渠道适配器负责转换。"""

    normalized_url = _validate_public_resource_url(url)
    normalized_title = str(title or "").strip()
    normalized_description = str(description or "").strip()
    normalized_thumb_url = str(thumb_url or "").strip()
    if len(normalized_title) > MAX_LINK_TITLE_LENGTH:
        raise PluginReplyError("链接标题超过平台限制")
    if len(normalized_description) > MAX_LINK_DESCRIPTION_LENGTH:
        raise PluginReplyError("链接描述超过平台限制")
    if normalized_thumb_url:
        normalized_thumb_url = _validate_public_resource_url(normalized_thumb_url)

    # 标题可以为空，渠道层会使用 URL 作为最小可读内容；这样既保持 A-BOT 规则能力，
    # 又不会为了展示文案在插件核心中加入任何微信或 Telegram 专用分支。
    reply = {
        "type": "link",
        "url": normalized_url,
        "title": normalized_title,
        "description": normalized_description,
    }
    if normalized_thumb_url:
        reply["thumb_url"] = normalized_thumb_url
    return reply


def video_reply(
    *,
    video_base64: str,
    cover_base64: str,
    play_length: int | None = None,
) -> dict[str, Any]:
    """构造带封面的视频回复，时长使用非负秒数。"""

    normalized_video = _validate_base64(
        video_base64,
        max_length=MAX_VIDEO_BASE64_LENGTH,
        label="视频",
    )
    normalized_cover = _validate_base64(
        cover_base64,
        max_length=MAX_IMAGE_BASE64_LENGTH,
        label="视频封面",
    )
    reply: dict[str, Any] = {
        "type": "video",
        "base64": normalized_video,
        "image_base64": normalized_cover,
    }
    if play_length is not None:
        normalized_length = int(play_length)
        if normalized_length < 0 or normalized_length > 86_400:
            raise PluginReplyError("视频时长不合法")
        reply["play_length"] = normalized_length
    return reply


def standard_replies(
    replies: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    max_count: int = MAX_REPLY_COUNT,
    allowed_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """校验一组标准回复，阻止插件一次生成过多出站任务。"""

    bounded_count = max(1, min(int(max_count or 1), MAX_REPLY_COUNT))
    if len(replies) > bounded_count:
        raise PluginReplyError("单次插件回复数量超过平台限制")
    normalized: list[dict[str, Any]] = []
    for item in replies:
        if not isinstance(item, Mapping):
            raise PluginReplyError("插件回复必须是对象")
        reply_type = str(item.get("type") or "").strip().lower()
        if reply_type not in ALLOWED_REPLY_TYPES:
            raise PluginReplyError("插件回复类型不受支持")
        # 即使插件手工传入 dict，也重新经过对应构造器校验，不能用绕过帮助函数的
        # 方式注入 file:// 地址、越权本地路径或无界 base64 内容。
        if reply_type == "text":
            normalized.append(text_reply(str(item.get("content") or item.get("text") or "")))
        elif reply_type == "image":
            normalized.append(
                image_reply(
                    base64_data=str(item.get("base64") or item.get("image_base64") or "") or None,
                    image_path=item.get("image_path") or item.get("path"),
                    allowed_root=allowed_root,
                )
            )
        elif reply_type == "file":
            normalized.append(
                file_reply(
                    file_name=str(item.get("file_name") or ""),
                    file_url=str(item.get("file_url") or item.get("url") or ""),
                )
            )
        elif reply_type == "video":
            normalized.append(
                video_reply(
                    video_base64=str(item.get("base64") or item.get("video_base64") or ""),
                    cover_base64=str(item.get("image_base64") or item.get("cover_base64") or ""),
                    play_length=item.get("play_length"),
                )
            )
        else:
            normalized.append(
                link_reply(
                    url=str(item.get("url") or item.get("link_url") or ""),
                    title=str(item.get("title") or item.get("link_title") or ""),
                    description=str(
                        item.get("description")
                        or item.get("desc")
                        or item.get("link_description")
                        or ""
                    ),
                    thumb_url=str(item.get("thumb_url") or item.get("image_url") or ""),
                )
            )
    return normalized


def _validate_owned_file(
    file_path: str | Path | None,
    *,
    allowed_root: str | Path | None,
) -> Path:
    if file_path is None or allowed_root is None:
        raise PluginReplyError("本地文件回复必须提供授权目录")
    root = Path(allowed_root).expanduser().resolve(strict=True)
    candidate = Path(file_path).expanduser().resolve(strict=True)
    if not root.is_dir() or not candidate.is_file():
        raise PluginReplyError("本地回复资源不存在")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PluginReplyError("本地回复资源不属于当前授权目录") from exc
    return candidate


def _validate_public_resource_url(url: str) -> str:
    normalized = str(url or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise PluginReplyError("文件地址必须使用 http 或 https")
    if parsed.username is not None or parsed.password is not None:
        raise PluginReplyError("文件地址不能内嵌账号密码")
    return normalized


def _validate_base64(value: str, *, max_length: int, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise PluginReplyError(f"{label} base64 为空或超过平台限制")
    try:
        base64.b64decode(normalized, validate=True)
    except (ValueError, TypeError) as exc:
        raise PluginReplyError(f"{label} base64 格式不合法") from exc
    return normalized
