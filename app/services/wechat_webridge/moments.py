"""朋友圈:通过微桥手机取回朋友圈动态,缓存到 moments_posts,供后台「朋友圈」页面查看。

协议(微桥 8080 文档第 14 节,并已在真实手机上核对过返回结构):
- 朋友圈动作是异步的:命令 moments_list 的回报只是"已受理"(status=pending,带 operation_id),没有内容;
  要再发 moments_operation(operation_id)取最终结果(status=succeeded,items 是动态列表,next_cursor 用于翻到更早的)。
  手机端通常 2 秒内完成,所以这里在第一次回报后安排几次间隔递增的查询(见 POLL_DELAYS)。
- 一次最多约 10 条;direction=head 取最新,direction=tail 配合上一页的 next_cursor 取更早的。
- 同一时间只能有一个在途操作(手机端会拒绝同范围的并发任务),所以刷新进行中时不会再发第二个。
- 动态条目:sns_id、wxid(作者)、create_time(秒)、content、type、is_mine、liked、like_count、comment_count、
  media[]、likes[]、comments[];没有作者昵称和头像,页面按好友表解析。

状态(进行中/完成/失败、更早内容的游标)存在渠道 runtime_info_json 的 "moments" 键里,不新增表。
朋友圈内容是好友的隐私:只缓存文字、点赞评论和预览图地址,日志与任务结果里只记数量,不记内容。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import SHANGHAI_TZ, now_shanghai
from app.models.entities import Channel, MomentsMedia, MomentsPost
from app.services.identity_directory import IdentityDirectory
from app.services.message_view import duration_label, size_label
from app.services.wechat_webridge import service, wakeup

logger = get_logger(__name__)

COMMAND_LIST = "moments_list"
COMMAND_OPERATION = "moments_operation"
MOMENTS_COMMANDS = {COMMAND_LIST, COMMAND_OPERATION}
STATE_KEY = "moments"
PRIORITY = 120
# 第一次回报(已受理)之后查询最终结果的等待时间(秒):实测通常 2 秒内完成,留到 16 秒左右。
POLL_DELAYS = (1.5, 2.0, 3.0, 4.0, 6.0)
MAX_POLL_ATTEMPTS = len(POLL_DELAYS)
# 刷新超过这么久还没有结果,就当作手机没有响应,允许重新发起。
RUNNING_TIMEOUT_SECONDS = 120
PAGE_SIZE = 20
MAX_COMMENTS = 50
MAX_LIKES = 100
MAX_MEDIA = 12


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------- 状态 ----------

def get_state(channel: Channel) -> dict[str, Any]:
    """读取刷新状态。进行中但超过 RUNNING_TIMEOUT_SECONDS 的视为失败(手机离线或没有响应)。"""

    state = service._runtime(channel).get(STATE_KEY)
    state = dict(state) if isinstance(state, dict) else {}
    if state.get("state") == "running":
        started = _parse_time(state.get("started_at"))
        if started is None or now_shanghai() - started > timedelta(seconds=RUNNING_TIMEOUT_SECONDS):
            state.update(state="failed", error="手机没有响应，请确认手机在线后重试")
    return state


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI_TZ)


def _set_state(channel: Channel, **updates: Any) -> dict[str, Any]:
    runtime = service._runtime(channel)
    state = runtime.get(STATE_KEY) if isinstance(runtime.get(STATE_KEY), dict) else {}
    state = {**state, **updates}
    runtime[STATE_KEY] = state
    service._save_runtime(channel, runtime)
    return state


# ---------- 发起刷新 ----------

def request_refresh(db: Session, channel: Channel, direction: str = "head") -> dict[str, Any]:
    """向手机发起一次朋友圈取回。direction: head 刷新最新,tail 取更早的(需要上一页留下的游标)。

    返回 {"started": bool, "reason": str}:已有刷新在进行中、或没有更早的内容时不发起。
    """

    if direction not in {"head", "tail"}:
        raise ValueError("朋友圈方向只能是 head 或 tail")
    current = get_state(channel)
    if current.get("state") == "running":
        return {"started": False, "reason": "已经在刷新了，请稍等"}
    args: dict[str, Any] = {"direction": direction}
    if direction == "tail":
        cursor = _text(current.get("next_cursor"))
        if not cursor:
            return {"started": False, "reason": "没有更早的动态了，或还没刷新过"}
        args["cursor"] = cursor
    request_id = f"moments_{uuid.uuid4().hex}"
    service.enqueue_command(db, channel, command_name=COMMAND_LIST, args=args, request_id=request_id, priority=PRIORITY)
    _set_state(channel, state="running", direction=direction, request_id=request_id, started_at=now_shanghai().isoformat(), finished_at="", error="", new_count=0, fetched_count=0)
    return {"started": True, "reason": ""}


# ---------- 手机回报 ----------

def handle_result(db: Session, channel: Channel, command: dict[str, Any], ok: bool, result: dict[str, Any]) -> None:
    """moments_list / moments_operation 的回报:已受理就安排查询最终结果,拿到结果就入库。调用方负责提交。"""

    command_type = _text(command.get("type"))
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    status = _text(inner.get("status"))
    operation_id = _text(inner.get("operation_id"))
    request_id = _text(command.get("request_id"))

    if not ok or status in {"failed", "unknown"}:
        reason = _text(inner.get("error") or result.get("error")) or ("朋友圈取回失败" if status != "unknown" else "手机没有确认结果，请稍后重试")
        _set_state(channel, state="failed", error=reason[:200], finished_at=now_shanghai().isoformat())
        logger.warning("moments fetch failed", extra={"channel_id": channel.id, "command": command_type, "status": status})
        return
    if status == "succeeded" and isinstance(inner.get("items"), list):
        _persist(db, channel, inner)
        return
    # 已受理但还没有结果:安排下一次查询
    attempt = 1
    if command_type == COMMAND_OPERATION:
        tail = request_id.rsplit("_", 1)[-1]
        attempt = _int(tail, 1) + 1
    if not operation_id or attempt > MAX_POLL_ATTEMPTS:
        _set_state(channel, state="failed", error="朋友圈取回超时，请稍后重试", finished_at=now_shanghai().isoformat())
        return
    delay = POLL_DELAYS[attempt - 1]
    service.enqueue_command(
        db, channel, command_name=COMMAND_OPERATION, args={"operation_id": operation_id},
        request_id=f"moments_op_{operation_id}_{attempt}", priority=PRIORITY, available_at=now_shanghai() + timedelta(seconds=delay),
    )
    # 命令随调用方的事务提交,延迟之后再叫醒手机轮询(延迟一定晚于提交)。
    wakeup.notify(int(channel.id), delay)


# ---------- 入库 ----------

def _compact_media(item: dict[str, Any]) -> list[dict[str, Any]]:
    """媒体只留下占位卡片需要的信息。预览地址(preview_urls)不保存:它是微信 CDN 上的加密内容,
    浏览器直接请求会被拒绝,真要看得让手机下载解密(见 moments_media)。"""

    media = []
    for entry in (item.get("media") or [])[:MAX_MEDIA]:
        if not isinstance(entry, dict):
            continue
        media.append(
            {
                "mid": _text(entry.get("mid")), "type": _int(entry.get("type")), "width": _int(entry.get("width")), "height": _int(entry.get("height")),
                "duration": float(entry.get("videoDuration") or 0), "size": _int(entry.get("totalSize")),
                "title": _text(entry.get("title"))[:100], "desc": _text(entry.get("desc"))[:200],
            }
        )
    return media


def _compact_comments(item: dict[str, Any]) -> list[dict[str, Any]]:
    comments = []
    for entry in (item.get("comments") or [])[:MAX_COMMENTS]:
        if not isinstance(entry, dict):
            continue
        comments.append(
            {
                "id": _text(entry.get("comment_id")), "wxid": _text(entry.get("wxid")), "nickname": _text(entry.get("nickname"))[:60],
                "content": _text(entry.get("content"))[:500], "reply_wxid": _text(entry.get("reply_wxid")), "time": _int(entry.get("create_time")),
                "mine": bool(entry.get("is_mine")),
            }
        )
    return comments


def _compact_likes(item: dict[str, Any]) -> list[dict[str, str]]:
    likes = []
    for entry in (item.get("likes") or [])[:MAX_LIKES]:
        if isinstance(entry, dict):
            likes.append({"wxid": _text(entry.get("wxid")), "nickname": _text(entry.get("nickname"))[:60]})
        elif entry:
            likes.append({"wxid": _text(entry), "nickname": ""})
    return likes


def _persist(db: Session, channel: Channel, inner: dict[str, Any]) -> None:
    """把一页动态写入缓存(同一条动态只更新互动数据),并记下游标与结果状态。"""

    now = now_shanghai()
    items = [item for item in inner.get("items") or [] if isinstance(item, dict) and _text(item.get("sns_id")) and _text(item.get("wxid"))]
    existing = {
        post.sns_id: post
        for post in db.scalars(select(MomentsPost).where(MomentsPost.channel_id == channel.id, MomentsPost.sns_id.in_([_text(i["sns_id"]) for i in items]))).all()
    } if items else {}
    created = 0
    for item in items:
        sns_id = _text(item["sns_id"])
        post = existing.get(sns_id)
        if post is None:
            seconds = _int(item.get("create_time"))
            post = MomentsPost(
                channel_id=channel.id, sns_id=sns_id, author_wxid=_text(item["wxid"])[:191],
                create_time=datetime.fromtimestamp(seconds, tz=SHANGHAI_TZ) if seconds > 0 else now,
            )
            db.add(post)
            existing[sns_id] = post
            created += 1
        post.content = _text(item.get("content")) or None
        post.post_type = _int(item.get("type"))
        post.is_mine = bool(item.get("is_mine"))
        post.liked = bool(item.get("liked"))
        post.like_count = _int(item.get("like_count"))
        post.comment_count = _int(item.get("comment_count"))
        post.private = bool(item.get("private"))
        post.media_json = json.dumps(_compact_media(item), ensure_ascii=False)
        post.likes_json = json.dumps(_compact_likes(item), ensure_ascii=False)
        post.comments_json = json.dumps(_compact_comments(item), ensure_ascii=False)
        post.fetched_at = now
    # 游标:翻到更早的需要它。没有 next_cursor(或明确没有更多)就清空,页面据此隐藏"加载更早"。
    cursor = "" if inner.get("has_more") is False else _text(inner.get("next_cursor"))
    _set_state(
        channel, state="done", next_cursor=cursor, finished_at=now.isoformat(), error="", new_count=created, fetched_count=len(items),
        refreshed_at=now.isoformat(),
    )
    logger.info("moments fetched", extra={"channel_id": channel.id, "fetched_count": len(items), "created_count": created, "has_older": bool(cursor)})


# ---------- 页面数据 ----------

def decode(post: MomentsPost) -> dict[str, Any]:
    """把一行动态整理成页面用的结构(JSON 字段解开)。"""

    def load(raw: str | None) -> list:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []

    media = load(post.media_json)
    video = any(m.get("duration") for m in media) or post.post_type == 15
    for item in media:
        # 占位卡片上的说明:视频是"0:13 · 15.9 MB",图片是大小
        parts = [duration_label(item.get("duration") or 0) if video else "", size_label(_int(item.get("size")))]
        item["meta"] = " · ".join(part for part in parts if part)
    return {
        "id": int(post.id), "sns_id": post.sns_id, "wxid": post.author_wxid, "time": post.create_time, "content": post.content or "",
        "post_type": post.post_type, "is_mine": bool(post.is_mine), "liked": bool(post.liked), "like_count": int(post.like_count or 0),
        "comment_count": int(post.comment_count or 0), "private": bool(post.private), "media": media,
        "comments": load(post.comments_json), "likes": load(post.likes_json),
        # 视频动态只有一张封面,图片动态可能有多张;模板据此决定画播放标记还是宫格
        "is_video": video,
    }


def build_feed(db: Session, channel: Channel, *, before_id: int = 0, limit: int | None = None) -> dict[str, Any]:
    """按发布时间倒序取一页动态,并解析作者昵称与头像。before_id 是上一页最后一条的主键(时间相同时按主键稳定翻页)。"""

    limit = limit or PAGE_SIZE
    stmt = select(MomentsPost).where(MomentsPost.channel_id == channel.id)
    if before_id:
        anchor = db.get(MomentsPost, int(before_id))
        if anchor is not None and anchor.channel_id == channel.id:
            stmt = stmt.where(
                (MomentsPost.create_time < anchor.create_time) | ((MomentsPost.create_time == anchor.create_time) & (MomentsPost.id < anchor.id))
            )
    rows = list(db.scalars(stmt.order_by(MomentsPost.create_time.desc(), MomentsPost.id.desc()).limit(limit + 1)).all())
    has_more = len(rows) > limit
    posts = [decode(row) for row in rows[:limit]]
    # 每个媒体带上"手机传回来了吗":ready(可直接显示)/ pending / failed / none
    stored = {
        (int(item.post_id), item.mid): item
        for item in db.scalars(select(MomentsMedia).where(MomentsMedia.post_id.in_([post["id"] for post in posts]))).all()
    } if posts else {}
    for post in posts:
        for media in post["media"]:
            item = stored.get((post["id"], media.get("mid")))
            ready = item is not None and item.status == "downloaded" and bool(item.storage_path)
            media["state"] = "ready" if ready else ("failed" if item is not None and item.status == "failed" else ("loading" if item is not None and item.status == "pending" else "none"))
            media["url"] = f"/moments/media/file/{item.id}" if ready else ""
    directory = IdentityDirectory(db)
    authors = {post["wxid"] for post in posts}
    directory.preload(channel.id, authors)
    identities = {wxid: directory.get(channel.id, wxid) for wxid in authors}
    return {"posts": posts, "identities": identities, "has_more": has_more}


def summary(db: Session, channel: Channel) -> dict[str, Any]:
    """页面头部用的统计:缓存条数、最近刷新时间、能否继续取更早的。"""

    state = get_state(channel)
    return {
        "cached": int(db.scalar(select(func.count(MomentsPost.id)).where(MomentsPost.channel_id == channel.id)) or 0),
        "state": state.get("state") or "idle",
        "error": state.get("error") or "",
        "refreshed_at": _parse_time(state.get("refreshed_at")),
        "has_older": bool(_text(state.get("next_cursor"))),
        "new_count": _int(state.get("new_count")),
        "fetched_count": _int(state.get("fetched_count")),
    }
