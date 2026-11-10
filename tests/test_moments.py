"""朋友圈:取回流程(受理 → 查询结果 → 入库)、重复取回、游标、超时失败、页面与接口。

用来造数据的动态结构与线上真实手机返回的字段一致(内容、微信号、地址都是虚构的)。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="moments-logs-"))

import pytest
from sqlalchemy import select

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import MomentsPost, TaskJob, WorkWechatContact
from app.services.wechat_webridge import moments, service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "wxid_bot"}
OP = "11111111-1111-4111-8111-111111111111"
CURSOR = "12345678901234567890"


def _item(n: int, *, wxid: str = "wxid_friend1", content: str | None = None, **extra) -> dict:
    return {
        "sns_id": f"1459000000000000{n:04d}", "wxid": wxid, "create_time": 1794300000 + n, "content": content if content is not None else f"虚构的第{n}条动态",
        "type": 2, "is_mine": False, "liked": False, "like_count": 1, "comment_count": 1, "private": False, "visibility_type": 0,
        "media": [{"mid": f"mid{n}", "type": 1, "width": 1080, "height": 1440, "preview_urls": [f"https://img.example.com/{n}.jpg", "https://img.example.com/x.jpg"], "dataUrl": "https://img.example.com/d", "videoDuration": 0, "title": "", "desc": ""}],
        "likes": [{"wxid": "wxid_a", "nickname": "甲"}], "comments": [{"comment_id": "1", "wxid": "wxid_b", "nickname": "乙", "content": "不错", "reply_wxid": "", "create_time": 1794300100, "is_mine": False}],
        "media_truncated": 0, "interactions_truncated": 0, **extra,
    }


def _pending(op: str = OP, direction: str = "head") -> dict:
    return {"ok": True, "result": {"status": "pending", "operation_id": op, "accepted": True, "direction": direction, "cursor": "0", "write_accepted": False}}


def _done(items: list, *, direction: str = "head", next_cursor: str = CURSOR, has_more=None, op: str = OP) -> dict:
    return {"ok": True, "result": {"status": "succeeded", "operation_id": op, "direction": direction, "items": items, "next_cursor": next_cursor, "has_more": has_more, "raw_count": len(items), "revision": 1, "accepted": True, "server_confirmed": True}}


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    db.commit()
    return db, channel


def _commands(db, command_type: str | None = None) -> list[tuple[TaskJob, dict]]:
    rows = []
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE).order_by(TaskJob.id)).all():
        command = json.loads(job.payload_json).get("command", {})
        if command_type is None or command.get("type") == command_type:
            rows.append((job, command))
    return rows


def _report(db, channel, request_id: str, reported: dict) -> dict:
    for job, command in _commands(db):
        if command.get("request_id") == request_id:
            job.status = "running"
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"), patch.object(wakeup, "notify"):
        return service.accept_command_result(db, channel, {**IDENTITY, "request_id": request_id, **reported})


def _start(db, channel, direction: str = "head") -> str:
    assert moments.request_refresh(db, channel, direction)["started"] is True
    db.commit()
    return _commands(db, "moments_list")[-1][1]["request_id"]


def _posts(db) -> list[MomentsPost]:
    return list(db.scalars(select(MomentsPost).order_by(MomentsPost.create_time.desc())).all())


# ---------- 发起 ----------

def test_refresh_enqueues_the_list_command_and_blocks_a_second_one_while_running(env) -> None:
    db, channel = env
    request_id = _start(db, channel)
    job, command = _commands(db, "moments_list")[0]
    assert command["params"] == {"direction": "head"} and job.priority == moments.PRIORITY and request_id.startswith("moments_")
    assert moments.get_state(channel)["state"] == "running"
    again = moments.request_refresh(db, channel, "head")
    assert again == {"started": False, "reason": "已经在刷新了，请稍等"} and len(_commands(db, "moments_list")) == 1
    with pytest.raises(ValueError):
        moments.request_refresh(db, channel, "sideways")


def test_tail_needs_the_cursor_left_by_the_previous_page(env) -> None:
    db, channel = env
    assert moments.request_refresh(db, channel, "tail")["started"] is False  # 还没刷新过,没有游标
    request_id = _start(db, channel)
    _report(db, channel, request_id, _pending())
    _report(db, channel, f"moments_op_{OP}_1", _done([_item(1)]))
    tail_id = _start(db, channel, "tail")
    assert _commands(db, "moments_list")[-1][1]["params"] == {"direction": "tail", "cursor": CURSOR}
    assert tail_id != request_id


def test_a_running_refresh_that_never_answers_times_out_and_can_be_retried(env) -> None:
    db, channel = env
    _start(db, channel)
    runtime = json.loads(channel.runtime_info_json)
    runtime["moments"]["started_at"] = (now_shanghai() - timedelta(seconds=moments.RUNNING_TIMEOUT_SECONDS + 5)).isoformat()
    channel.runtime_info_json = json.dumps(runtime)
    state = moments.get_state(channel)
    assert state["state"] == "failed" and "手机没有响应" in state["error"]
    assert moments.request_refresh(db, channel, "head")["started"] is True  # 超时后可以重新发起


# ---------- 手机回报:已受理 → 查询结果 → 入库 ----------

def test_accepted_reply_schedules_the_result_query_and_the_final_result_is_stored(env) -> None:
    db, channel = env
    request_id = _start(db, channel)
    answer = _report(db, channel, request_id, _pending())
    assert answer["status"] == "completed"  # 取回是业务流程,命令本身一律记为完成,不产生失败任务
    # 第一次回报没有内容:安排一次 moments_operation,稍后才可领取
    (job, command), = _commands(db, "moments_operation")
    assert command["params"] == {"operation_id": OP} and command["request_id"] == f"moments_op_{OP}_1"
    assert ensure_shanghai_datetime(job.available_at) > now_shanghai() and moments.get_state(channel)["state"] == "running"
    # 查询结果:入库,记下游标
    _report(db, channel, command["request_id"], _done([_item(1), _item(2)]))
    assert len(_posts(db)) == 2
    state = moments.get_state(channel)
    assert (state["state"], state["new_count"], state["fetched_count"], state["next_cursor"]) == ("done", 2, 2, CURSOR)
    first = _posts(db)[0]
    assert (first.author_wxid, first.post_type, first.like_count, first.comment_count) == ("wxid_friend1", 2, 1, 1)


def test_job_results_never_contain_the_friends_content(env) -> None:
    db, channel = env
    request_id = _start(db, channel)
    _report(db, channel, request_id, _pending())
    _report(db, channel, f"moments_op_{OP}_1", _done([_item(1, content="这是一段不该出现在任务结果里的好友隐私内容")]))
    for job, _command in _commands(db):
        assert "好友隐私内容" not in (job.result_json or "") and "好友隐私内容" not in (job.payload_json or "")
    op_job = _commands(db, "moments_operation")[0][0]
    assert json.loads(op_job.result_json)["webridge"] == {"ok": True, "status": "succeeded", "count": 1}


def test_an_already_finished_first_reply_is_stored_without_another_query(env) -> None:
    db, channel = env
    request_id = _start(db, channel)
    _report(db, channel, request_id, _done([_item(1)]))
    assert len(_posts(db)) == 1 and _commands(db, "moments_operation") == []


def test_still_pending_is_retried_with_growing_delays_then_gives_up(env) -> None:
    db, channel = env
    _report(db, channel, _start(db, channel), _pending())
    for attempt in range(1, moments.MAX_POLL_ATTEMPTS):
        assert _commands(db, "moments_operation")[-1][1]["request_id"] == f"moments_op_{OP}_{attempt}"
        _report(db, channel, f"moments_op_{OP}_{attempt}", _pending())
    last = _commands(db, "moments_operation")[-1][1]["request_id"]
    assert last == f"moments_op_{OP}_{moments.MAX_POLL_ATTEMPTS}"
    _report(db, channel, last, _pending())  # 最后一次仍在处理:放弃
    state = moments.get_state(channel)
    assert state["state"] == "failed" and "超时" in state["error"] and _posts(db) == []
    assert len(_commands(db, "moments_operation")) == moments.MAX_POLL_ATTEMPTS


@pytest.mark.parametrize(
    "reported, expected",
    [
        ({"ok": False, "result": {"ok": False, "status": "failed", "error": "not_logged_in"}}, "not_logged_in"),
        ({"ok": True, "result": {"ok": True, "status": "unknown", "operation_id": OP}}, "没有确认结果"),
    ],
)
def test_failures_are_recorded_for_the_page_and_never_become_failed_jobs(env, reported, expected) -> None:
    db, channel = env
    request_id = _start(db, channel)
    answer = _report(db, channel, request_id, reported)
    state = moments.get_state(channel)
    assert answer["status"] == "completed" and state["state"] == "failed" and expected in state["error"]


# ---------- 入库细节 ----------

def test_fetching_the_same_posts_again_updates_interactions_without_duplicating(env) -> None:
    db, channel = env
    _report(db, channel, _start(db, channel), _done([_item(1), _item(2)]))
    changed = _item(1, like_count=9, comment_count=3, liked=True, comments=[{"comment_id": "7", "wxid": "wxid_c", "nickname": "丙", "content": "新评论", "reply_wxid": "wxid_b", "create_time": 1, "is_mine": False}])
    _report(db, channel, _start(db, channel), _done([changed, _item(3)]))
    posts = {p.sns_id[-4:]: p for p in _posts(db)}
    assert sorted(posts) == ["0001", "0002", "0003"] and moments.get_state(channel)["new_count"] == 1  # 只有第 3 条是新增
    one = posts["0001"]
    assert (one.like_count, one.comment_count, one.liked) == (9, 3, True) and json.loads(one.comments_json)[0]["content"] == "新评论"


def test_no_more_history_clears_the_cursor_and_bad_items_are_skipped(env) -> None:
    db, channel = env
    bad = [{"sns_id": "", "wxid": "wxid_x"}, {"sns_id": "1", "wxid": ""}, "garbage", {"content": "没有 id"}]
    _report(db, channel, _start(db, channel), _done([_item(1), *bad], has_more=False))
    assert len(_posts(db)) == 1
    assert moments.get_state(channel)["next_cursor"] == "" and moments.summary(db, channel)["has_older"] is False


def test_media_comments_and_likes_are_compacted(env) -> None:
    db, channel = env
    many_media = [{"mid": f"m{i}", "type": 1, "width": 1, "height": 1, "preview_urls": [f"https://img.example.com/{i}"]} for i in range(20)]
    many_comments = [{"comment_id": str(i), "wxid": "w", "nickname": "某", "content": "字" * 900, "create_time": 1} for i in range(80)]
    video = _item(5, type=15, media=[{"mid": "v", "type": 5, "width": 224, "height": 398, "totalSize": 16662778, "preview_urls": ["https://img.example.com/v.jpg"], "videoDuration": 12.967, "dataUrl": "https://img.example.com/vd"}])
    _report(db, channel, _start(db, channel), _done([_item(1, media=many_media, comments=many_comments, likes=["wxid_s", {"wxid": "wxid_d", "nickname": "丁"}]), video]))
    posts = {p.sns_id[-4:]: moments.decode(p) for p in _posts(db)}
    # 预览地址(微信 CDN 上的加密内容,浏览器加载不了)不保存;保存占位卡片需要的信息
    assert len(posts["0001"]["media"]) == moments.MAX_MEDIA and "preview" not in posts["0001"]["media"][0]
    assert all("img.example.com" not in (post.media_json or "") for post in _posts(db))
    assert len(posts["0001"]["comments"]) == moments.MAX_COMMENTS and len(posts["0001"]["comments"][0]["content"]) == 500
    assert posts["0001"]["likes"] == [{"wxid": "wxid_s", "nickname": ""}, {"wxid": "wxid_d", "nickname": "丁"}]
    assert posts["0005"]["is_video"] is True and posts["0005"]["media"][0]["duration"] == pytest.approx(12.967) and posts["0001"]["is_video"] is False
    # 占位卡片上的说明:视频是时长 + 大小
    assert posts["0005"]["media"][0]["meta"] == "0:13 · 15.9 MB"


# ---------- 页面数据 ----------

def test_feed_is_newest_first_pages_stably_and_resolves_friend_names(env) -> None:
    db, channel = env
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id="wxid_friend1", contact_kind="friend", name="张三", display_name="张三", source="wechat_webridge"))
    db.commit()
    _report(db, channel, _start(db, channel), _done([_item(n, wxid="wxid_friend1" if n % 2 else "wxid_stranger") for n in range(1, 8)]))
    page1 = moments.build_feed(db, channel, limit=3)
    assert [p["sns_id"][-4:] for p in page1["posts"]] == ["0007", "0006", "0005"] and page1["has_more"] is True
    assert page1["identities"]["wxid_friend1"]["name"] == "张三"
    assert page1["identities"]["wxid_stranger"]["name"] == "wxid_stranger"  # 不是好友:显示微信 ID
    page2 = moments.build_feed(db, channel, before_id=page1["posts"][-1]["id"], limit=3)
    assert [p["sns_id"][-4:] for p in page2["posts"]] == ["0004", "0003", "0002"] and page2["has_more"] is True
    page3 = moments.build_feed(db, channel, before_id=page2["posts"][-1]["id"], limit=3)
    assert [p["sns_id"][-4:] for p in page3["posts"]] == ["0001"] and page3["has_more"] is False


# ---------- 页面与接口 ----------

@pytest.fixture()
def webridge_channel(provider_page_context):  # noqa: F811
    from app.models.entities import Channel

    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.commit()
        return client, ids, int(channel.id)


def _seed_posts(ids, channel_id: int, count: int, wxid: str = "wxid_friend1") -> None:
    with ids["factory"]() as db:
        for n in range(1, count + 1):
            db.add(MomentsPost(
                tenant_id=ids["alpha"], channel_id=channel_id, sns_id=f"9{n:019d}", author_wxid=wxid, create_time=now_shanghai() - timedelta(minutes=count - n),
                content=f"虚构动态{n}", post_type=2, like_count=n, comment_count=1,
                media_json=json.dumps([{"mid": "m", "type": 1, "width": 100, "height": 200, "size": 2048, "duration": 0, "title": "", "desc": ""}]),
                likes_json=json.dumps([{"wxid": "w", "nickname": "甲"}]), comments_json=json.dumps([{"id": "1", "wxid": "w2", "nickname": "乙", "content": "赞", "reply_wxid": "", "time": 1, "mine": False}]),
            ))
        db.commit()


def test_menu_has_the_moments_entry_and_the_empty_state_without_an_account(provider_page_context) -> None:
    client, _ids = provider_page_context
    _login(client, "admin")
    page = client.get("/moments")
    assert page.status_code == 200 and "还没有接入个人微信账号" in page.text
    nav = page.text.split('class="app-primary-nav"', 1)[1].split("</nav>", 1)[0]
    assert '<a href="/moments"' in nav and "朋友圈" in nav and nav.index("消息") < nav.index("朋友圈")


def test_page_shows_cards_names_media_and_collapsed_comments(webridge_channel) -> None:
    client, ids, channel_id = webridge_channel
    _seed_posts(ids, channel_id, 3)
    with ids["factory"]() as db:
        db.add(WorkWechatContact(tenant_id=ids["alpha"], channel_id=channel_id, contact_external_id="wxid_friend1", contact_kind="friend", name="张三", display_name="张三", source="wechat_webridge"))
        db.commit()
    _login(client, "admin")
    html = client.get("/moments").text
    assert html.count('class="moment-card"') == 3 and "已缓存 3 条" in html and "张三" in html
    assert html.index("虚构动态3") < html.index("虚构动态1")  # 最新在前
    # 媒体是占位卡片(点击才让手机下载解密),不再直接引用微信 CDN 的预览地址
    assert "data-moment-media" in html and 'data-state="none"' in html and "点击获取" in html and "img.example.com" not in html and "<img src=\"https://" not in html.split('class="moment-media')[1].split("moment-foot")[0]
    assert "评论（1）" in html
    assert '<details class="disclosure moment-comments">' in html  # 评论折叠,用统一的展开图标
    # 管理员有「刷新」「加载更早」;只读成员没有
    assert 'id="moments-refresh"' in html
    _login(client, "readonly")
    readonly_html = client.get("/moments").text
    assert "虚构动态3" in readonly_html and 'id="moments-refresh"' not in readonly_html


def test_feed_endpoint_pages_through_the_cache(webridge_channel, monkeypatch) -> None:
    client, ids, channel_id = webridge_channel
    monkeypatch.setattr(moments, "PAGE_SIZE", 2)
    _seed_posts(ids, channel_id, 5)
    _login(client, "readonly")
    first = client.get(f"/moments/feed?channel_id={channel_id}").json()
    assert first["count"] == 2 and first["has_more"] is True and first["html"].count("moment-card") == 2
    second = client.get(f"/moments/feed?channel_id={channel_id}&before_id={first['last_id']}").json()
    third = client.get(f"/moments/feed?channel_id={channel_id}&before_id={second['last_id']}").json()
    assert (second["count"], third["count"], third["has_more"]) == (2, 1, False)
    assert client.get("/moments/feed?channel_id=999999").status_code == 404


def test_refresh_endpoint_needs_csrf_and_the_operator_role_and_reports_status(webridge_channel) -> None:
    client, ids, channel_id = webridge_channel
    csrf = _login(client, "admin")
    headers = {"X-CSRF-Token": csrf, "X-Requested-With": "fetch"}
    assert client.post("/moments/refresh", data={"channel_id": channel_id}).status_code == 403  # 没有令牌
    started = client.post("/moments/refresh", data={"channel_id": channel_id, "direction": "head"}, headers=headers).json()
    assert started == {"ok": True, "started": True, "notice": "正在从手机取回朋友圈…"}
    again = client.post("/moments/refresh", data={"channel_id": channel_id, "direction": "head"}, headers=headers).json()
    assert again["started"] is False and "已经在刷新" in again["notice"]
    status = client.get(f"/moments/status?channel_id={channel_id}").json()
    assert status["ok"] is True and status["state"] == "running" and status["cached"] == 0
    with ids["factory"]() as db:
        commands = [json.loads(j.payload_json)["command"] for j in db.scalars(select(TaskJob)).all()]
        assert [c["type"] for c in commands if c["type"].startswith("moments")] == ["moments_list"]
    # 没有更早的内容时取更早的不会发起
    tail = client.post("/moments/refresh", data={"channel_id": channel_id, "direction": "tail"}, headers=headers).json()
    assert tail["started"] is False
    csrf = _login(client, "readonly")
    assert client.post("/moments/refresh", data={"channel_id": channel_id}, headers={"X-CSRF-Token": csrf}).status_code == 403
