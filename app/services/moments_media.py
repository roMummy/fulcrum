"""朋友圈媒体:点击占位卡片时,让手机下载并解密这张图片/这个视频,再传到我们服务器保存,页面轮询到就显示。

为什么不直接用动态里的预览地址:朋友圈媒体在微信 CDN 上是加密存放的,而且地址只认微信客户端的 User-Agent——
浏览器直接请求会被拒绝(HTTP 400),换成微信的 UA 拿到的也是密文。密钥只在手机里,所以必须让手机来做。

协议(微桥 8080 文档 8.6/第 14 节):命令 moments_media_download,参数 sns_id、media_id(动态里媒体的 mid)和 upload 对象
(url、method、resource_id、headers);手机准备好后把文件的原始字节(不是 multipart)POST 到 upload.url。
限制:只支持普通图片和视频动态,单个文件 120 MiB,手机同时最多准备 2 个。

安全:上传地址不带登录态,所以每次请求生成一个一次性令牌放进 upload.headers,服务端只存摘要,
上传时核对令牌、媒体行处于等待状态、且在有效期内;核对通过后令牌立即作废。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, MomentsMedia, MomentsPost, TaskJob
from app.services.wechat_webridge import moments, service

COMMAND = "moments_media_download"
# 手机准备(最长约 120 秒)加上传(最长约 180 秒),留一点余量。
REQUEST_TIMEOUT_SECONDS = 330
MAX_BYTES = 120 * 1024 * 1024
# 手机同一时间最多准备这么多个朋友圈媒体。
MAX_CONCURRENT = 2
UPLOAD_HEADER = "X-Upload-Token"
# 能显示的媒体:线上实测动态里图片的媒体类型是 1、视频是 5(类型 2 是纯文字动态,没有媒体)
SUPPORTED_MEDIA_TYPES = {1, 5}


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def storage_dir(channel_id: int) -> Path:
    return Path(settings.data_dir) / "moments_media" / f"channel_{int(channel_id)}"


def sniff(head: bytes, declared: str = "") -> tuple[str, str]:
    """按文件头判断 (mime, 扩展名);手机常常只声明 application/octet-stream,所以以文件头为准。"""

    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head.startswith(b"\x89PNG"):
        return "image/png", ".png"
    if head[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif", ".gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    normalized = str(declared or "").split(";")[0].strip().lower()
    if normalized.startswith(("image/", "video/")):
        return normalized, ".bin"
    return "application/octet-stream", ".bin"


def _result(state: str, row: MomentsMedia | None = None, error: str = "") -> dict[str, Any]:
    ready = state == "ready" and row is not None
    return {"state": state, "media_id": int(row.id) if row is not None else None, "url": f"/moments/media/file/{row.id}" if ready else "", "error": error}


def find(db: Session, post_id: int, mid: str) -> MomentsMedia | None:
    return db.scalar(select(MomentsMedia).where(MomentsMedia.post_id == int(post_id), MomentsMedia.mid == str(mid)))


def status(db: Session, row: MomentsMedia | None) -> dict[str, Any]:
    """ready 可显示 / loading 手机正在处理 / failed 失败 / none 还没请求过。顺便把任务失败或超时落实到记录上。"""

    if row is None:
        return _result("none")
    if row.status == "downloaded" and row.storage_path:
        return _result("ready", row)
    if row.status == "failed":
        return _result("failed", row, row.last_error or "获取失败")
    if not row.requested_at:
        return _result("none", row)
    job = db.get(TaskJob, int(row.job_id)) if row.job_id else None
    if job is not None and job.status == "failed":
        row.status, row.last_error = "failed", (job.error_message or "手机没有响应")[:200]
        return _result("failed", row, row.last_error)
    if now_shanghai() - ensure_shanghai_datetime(row.requested_at) > timedelta(seconds=REQUEST_TIMEOUT_SECONDS):
        row.status, row.last_error = "failed", "获取超时，请确认手机在线后重试"
        return _result("failed", row, row.last_error)
    return _result("loading", row)


def request(db: Session, post: MomentsPost, mid: str) -> dict[str, Any]:
    """请求把这条动态里的某个媒体传上来。已有文件/已在获取中直接返回当前状态;否则下发下载命令。调用方负责提交。"""

    entry = next((item for item in moments.decode(post)["media"] if item.get("mid") == mid), None)
    if entry is None:
        raise ValueError("这条动态里没有这个媒体")
    if entry.get("type") not in SUPPORTED_MEDIA_TYPES:
        return _result("failed", None, "这种媒体暂不支持获取")
    row = find(db, post.id, mid)
    current = status(db, row)
    if current["state"] in {"ready", "loading"}:
        return current
    channel = db.get(Channel, post.channel_id)
    if channel is None or channel.channel_type != "wechat_webridge":
        return _result("failed", None, "这个渠道暂不支持获取朋友圈媒体")
    in_flight = db.scalar(
        select(func.count(MomentsMedia.id)).where(
            MomentsMedia.channel_id == channel.id, MomentsMedia.status == "pending", MomentsMedia.requested_at > now_shanghai() - timedelta(seconds=REQUEST_TIMEOUT_SECONDS)
        )
    ) or 0
    if in_flight >= MAX_CONCURRENT:
        return _result("failed", None, "手机正在处理其他媒体，请稍后再点")
    if row is None:
        row = MomentsMedia(channel_id=channel.id, post_id=post.id, sns_id=post.sns_id, mid=mid)
        db.add(row)
        db.flush()
    token = secrets.token_urlsafe(32)
    row.status, row.last_error, row.token_hash, row.requested_at = "pending", None, token_hash(token), now_shanghai()
    base_url = str(channel.api_base or "").strip().rstrip("/")
    job = service.enqueue_command(
        db, channel, command_name=COMMAND, priority=100, request_id=f"momedia_{row.id}_{secrets.token_hex(6)}",
        args={
            "sns_id": post.sns_id, "media_id": mid,
            "upload": {"url": f"{base_url}/agent/moments_upload?media_id={row.id}", "method": "POST", "resource_id": str(row.id), "headers": {UPLOAD_HEADER: token}},
        },
    )
    row.job_id = int(job.id)
    db.flush()
    return _result("loading", row)


def accepts_upload(row: MomentsMedia | None, token: str, channel_id: int) -> bool:
    """上传是否有效:记录存在、属于这个渠道、还在等待、没过期,且令牌对得上(恒定时间比较)。"""

    if row is None or int(row.channel_id) != int(channel_id) or row.status != "pending" or not row.token_hash or not row.requested_at:
        return False
    if now_shanghai() - ensure_shanghai_datetime(row.requested_at) > timedelta(seconds=REQUEST_TIMEOUT_SECONDS):
        return False
    return hmac.compare_digest(row.token_hash, token_hash(token))


def finish_upload(row: MomentsMedia, path: Path, size: int, mime_type: str) -> None:
    """文件已完整写盘:记录位置并让令牌作废(重复上传、别人拿到地址都没用)。"""

    row.status, row.storage_path, row.file_size, row.mime_type = "downloaded", str(path), int(size), mime_type
    row.last_error, row.token_hash = None, None
