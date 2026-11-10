"""朋友圈媒体:点击占位卡片 → 让手机下载解密并传回 → 保存、显示。

背景(线上实测):动态里的预览地址是 http://…qpic.cn / …video.qq.com,浏览器直接请求返回 HTTP 400(要微信的 User-Agent),
换成微信 UA 拿到的也不是 JPEG(是加密内容),所以必须让手机下载解密再传回(命令 moments_media_download)。
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="moments-media-logs-"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import wechat_webridge as api
from app.core.time import now_shanghai
from app.models.entities import Channel, MomentsMedia, MomentsPost, TaskJob
from app.services import moments_media
from app.services.wechat_webridge import moments, service
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base
from app.models import saas  # noqa: F401
from tests.test_ai_dispatch import _seed
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
IMAGE = {"mid": "mid-img", "type": 1, "width": 1080, "height": 1440, "duration": 0, "size": 2048, "title": "", "desc": ""}
VIDEO = {"mid": "mid-vid", "type": 5, "width": 224, "height": 398, "duration": 12.9, "size": 16662778, "title": "", "desc": ""}


def _session():
    """内存库会话。上传接口由 TestClient 在另一个线程里执行,所以连接要允许跨线程(StaticPool 保证同一个库)。"""

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    # 裸 SQLite 会话没有租户自动补全:flush 前补 tenant_id。
    sa_event.listen(db, "before_flush", lambda s, c, i: [setattr(o, "tenant_id", 1) for o in s.new if hasattr(o, "tenant_id") and o.tenant_id is None])
    return db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(moments_media.settings, "data_dir", str(tmp_path))
    db = _session()
    channel = _seed(db)
    channel.api_base = "http://mba.test/api/wechat-webridge/1"
    posts = {}
    for key, media, post_type in (("a", [IMAGE, VIDEO], 2), ("b", [dict(IMAGE, mid="other")], 2), ("c", [dict(IMAGE, mid="third")], 2), ("text", [dict(IMAGE, mid="odd", type=3)], 1)):
        post = MomentsPost(tenant_id=1, channel_id=channel.id, sns_id=f"146000000000000{key}"[:20], author_wxid="wxid_f", create_time=now_shanghai(), post_type=post_type, media_json=json.dumps(media))
        db.add(post)
        posts[key] = post
    db.commit()
    return db, channel, posts


def _commands(db) -> list[tuple[TaskJob, dict]]:
    rows = []
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE).order_by(TaskJob.id)).all():
        command = json.loads(job.payload_json).get("command", {})
        if command.get("type") == "moments_media_download":
            rows.append((job, command))
    return rows


# ---------- 发起 ----------

def test_a_click_asks_the_phone_with_an_upload_target_and_a_one_time_token(env) -> None:
    db, channel, posts = env
    outcome = moments_media.request(db, posts["a"], "mid-img")
    db.commit()
    assert outcome["state"] == "loading" and outcome["media_id"]
    (job, command), = _commands(db)
    row = db.get(MomentsMedia, outcome["media_id"])
    params = command["params"]
    # 参数按文档:sns_id、media_id,以及 upload 对象(地址、POST 原始字节、资源 ID、令牌头)
    assert (params["sns_id"], params["media_id"]) == (posts["a"].sns_id, "mid-img")
    upload = params["upload"]
    assert upload["url"] == f"http://mba.test/api/wechat-webridge/1/agent/moments_upload?media_id={row.id}" and upload["method"] == "POST" and upload["resource_id"] == str(row.id)
    token = upload["headers"][moments_media.UPLOAD_HEADER]
    assert len(token) >= 32 and row.token_hash == moments_media.token_hash(token) and token not in (row.token_hash or "")  # 库里只有摘要
    # 获取中:再点不会重复下发
    assert moments_media.request(db, posts["a"], "mid-img")["state"] == "loading" and len(_commands(db)) == 1


def test_unknown_unsupported_and_busy_requests_are_refused_with_a_reason(env) -> None:
    db, channel, posts = env
    with pytest.raises(ValueError):
        moments_media.request(db, posts["a"], "no-such-media")
    assert "暂不支持" in moments_media.request(db, posts["text"], "odd")["error"]  # 不是图片(1)/视频(5)的媒体类型
    # 手机同时最多准备 2 个:第 3 个先提示稍后再点
    moments_media.request(db, posts["a"], "mid-img")
    moments_media.request(db, posts["b"], "other")
    third = moments_media.request(db, posts["c"], "third")
    assert third["state"] == "failed" and "稍后" in third["error"] and len(_commands(db)) == 2
    channel.channel_type = "wechat_ipad"
    assert "暂不支持" in moments_media.request(db, posts["a"], "mid-vid")["error"]


def test_failures_and_timeouts_are_reported_and_a_retry_issues_a_fresh_token(env) -> None:
    db, channel, posts = env
    first = moments_media.request(db, posts["a"], "mid-img")
    db.commit()
    (job, command), = _commands(db)
    old_token = command["params"]["upload"]["headers"][moments_media.UPLOAD_HEADER]
    job.status, job.error_message = "failed", "手机上找不到这个媒体"
    db.commit()
    failed = moments_media.status(db, db.get(MomentsMedia, first["media_id"]))
    assert failed["state"] == "failed" and "找不到" in failed["error"]
    retry = moments_media.request(db, posts["a"], "mid-img")
    db.commit()
    new_token = _commands(db)[-1][1]["params"]["upload"]["headers"][moments_media.UPLOAD_HEADER]
    assert retry["state"] == "loading" and new_token != old_token and len(_commands(db)) == 2
    row = db.get(MomentsMedia, retry["media_id"])
    assert moments_media.accepts_upload(row, old_token, channel.id) is False  # 旧令牌作废
    row.requested_at = now_shanghai() - timedelta(seconds=moments_media.REQUEST_TIMEOUT_SECONDS + 5)
    assert moments_media.status(db, row)["state"] == "failed" and "超时" in row.last_error


def test_upload_acceptance_checks_token_channel_state_and_expiry(env) -> None:
    db, channel, posts = env
    outcome = moments_media.request(db, posts["a"], "mid-img")
    row = db.get(MomentsMedia, outcome["media_id"])
    token = _commands(db)[0][1]["params"]["upload"]["headers"][moments_media.UPLOAD_HEADER]
    assert moments_media.accepts_upload(row, token, channel.id) is True
    assert moments_media.accepts_upload(row, "", channel.id) is False and moments_media.accepts_upload(row, "wrong", channel.id) is False
    assert moments_media.accepts_upload(row, token, channel.id + 1) is False and moments_media.accepts_upload(None, token, channel.id) is False
    row.requested_at = now_shanghai() - timedelta(seconds=moments_media.REQUEST_TIMEOUT_SECONDS + 5)
    assert moments_media.accepts_upload(row, token, channel.id) is False


def test_file_type_is_decided_by_the_bytes_not_the_declared_type() -> None:
    assert moments_media.sniff(JPEG, "application/octet-stream") == ("image/jpeg", ".jpg")
    assert moments_media.sniff(b"\x89PNG\r\n\x1a\n" + b"0" * 8)[1] == ".png" and moments_media.sniff(b"GIF89a" + b"0" * 8)[1] == ".gif"
    assert moments_media.sniff(b"RIFF\x00\x00\x00\x00WEBP" + b"0")[1] == ".webp" and moments_media.sniff(MP4)[0] == "video/mp4"
    assert moments_media.sniff(b"garbage-bytes!!!!", "text/html") == ("application/octet-stream", ".bin")  # 不是图片/视频:不信声明


# ---------- 上传接口 ----------

@pytest.fixture()
def upload_client(env, monkeypatch):
    db, channel, posts = env

    @contextmanager
    def session(_factory):
        yield db

    @contextmanager
    def scope(_tenant_id):
        yield

    monkeypatch.setattr(api, "_channel_tenant", lambda channel_id: (1, int(channel_id)))
    monkeypatch.setattr(api, "tenant_session", session)
    monkeypatch.setattr(api, "tenant_scope", scope)
    app = FastAPI()
    app.include_router(api.router)
    outcome = moments_media.request(db, posts["a"], "mid-img")
    db.commit()
    token = _commands(db)[0][1]["params"]["upload"]["headers"][moments_media.UPLOAD_HEADER]
    return TestClient(app), db, channel, int(outcome["media_id"]), token


def _upload(client, channel_id, media_id, token, body, content_type="application/octet-stream"):
    return client.post(f"/api/wechat-webridge/{channel_id}/agent/moments_upload?media_id={media_id}", content=body, headers={moments_media.UPLOAD_HEADER: token, "Content-Type": content_type})


def test_the_phone_posts_raw_bytes_and_the_file_is_saved_and_the_token_is_spent(upload_client, tmp_path) -> None:
    client, db, channel, media_id, token = upload_client
    response = _upload(client, channel.id, media_id, token, JPEG)
    assert response.status_code == 200 and response.json() == {"ok": True, "asset_id": str(media_id), "file_size": len(JPEG)}
    db.expire_all()
    row = db.get(MomentsMedia, media_id)
    assert (row.status, row.mime_type, row.file_size, row.token_hash) == ("downloaded", "image/jpeg", len(JPEG), None)
    assert (tmp_path / "moments_media" / f"channel_{channel.id}" / f"{media_id}.jpg").read_bytes() == JPEG and not list((tmp_path / "moments_media").rglob("*.part"))
    assert moments_media.status(db, row) == {"state": "ready", "media_id": media_id, "url": f"/moments/media/file/{media_id}", "error": ""}
    # 令牌用过一次就作废:同一个令牌再传(例如地址被别人拿到)被拒绝,已有文件不被覆盖
    assert _upload(client, channel.id, media_id, token, b"\xff\xd8\xffEVIL").status_code == 403
    assert (tmp_path / "moments_media" / f"channel_{channel.id}" / f"{media_id}.jpg").read_bytes() == JPEG


def test_upload_is_refused_without_the_right_token_or_for_the_wrong_channel_and_leaves_nothing_behind(upload_client, tmp_path) -> None:
    client, db, channel, media_id, token = upload_client
    assert _upload(client, channel.id, media_id, "", JPEG).status_code == 403
    assert _upload(client, channel.id, media_id, "bad-token", JPEG).status_code == 403
    assert _upload(client, channel.id + 5, media_id, token, JPEG).status_code == 403  # 令牌属于另一个渠道的记录
    assert _upload(client, channel.id, 999999, token, JPEG).status_code == 403
    assert not (tmp_path / "moments_media").exists() or not list((tmp_path / "moments_media").rglob("*.*"))
    assert db.get(MomentsMedia, media_id).status == "pending"


def test_empty_and_oversized_uploads_are_rejected(upload_client, monkeypatch, tmp_path) -> None:
    client, db, channel, media_id, token = upload_client
    assert _upload(client, channel.id, media_id, token, b"").status_code == 400
    monkeypatch.setattr(moments_media, "MAX_BYTES", 100)
    assert _upload(client, channel.id, media_id, token, b"\xff\xd8\xff" + b"0" * 500).status_code == 413
    assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob("*.jpg")) and db.get(MomentsMedia, media_id).status == "pending"


def test_a_video_is_stored_as_mp4(upload_client, tmp_path) -> None:
    client, db, channel, media_id, token = upload_client
    assert _upload(client, channel.id, media_id, token, MP4, "video/mp4").json()["file_size"] == len(MP4)
    db.expire_all()
    assert db.get(MomentsMedia, media_id).mime_type == "video/mp4" and (tmp_path / "moments_media" / f"channel_{channel.id}" / f"{media_id}.mp4").exists()


# ---------- 页面接口 ----------

@pytest.fixture()
def page_env(provider_page_context, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(moments_media.settings, "data_dir", str(tmp_path))
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True, api_base="http://mba.test/api/wechat-webridge/1")
        db.add(channel)
        db.flush()
        post = MomentsPost(tenant_id=ids["alpha"], channel_id=channel.id, sns_id="1459000000000000001", author_wxid="wxid_f", create_time=now_shanghai(), content="虚构动态", post_type=2, media_json=json.dumps([IMAGE]))
        db.add(post)
        db.commit()
        return client, ids, int(channel.id), int(post.id)


def test_load_status_and_file_endpoints(page_env, tmp_path) -> None:
    client, ids, channel_id, post_id = page_env
    csrf = _login(client, "admin")
    headers = {"X-CSRF-Token": csrf, "X-Requested-With": "fetch"}
    assert client.post("/moments/media/load", data={"post_id": post_id, "mid": "mid-img"}).status_code == 403  # 没有令牌
    loaded = client.post("/moments/media/load", data={"post_id": post_id, "mid": "mid-img"}, headers=headers).json()
    assert loaded["ok"] is True and loaded["state"] == "loading"
    assert client.get(f"/moments/media/status?post_id={post_id}&mid=mid-img").json()["state"] == "loading"
    assert client.get(f"/moments/media/file/{loaded['media_id']}").status_code == 404  # 还没传回来
    bad = client.post("/moments/media/load", data={"post_id": post_id, "mid": "nope"}, headers=headers)
    assert bad.status_code == 400 and bad.json()["ok"] is False
    assert client.post("/moments/media/load", data={"post_id": 999999, "mid": "mid-img"}, headers=headers).status_code == 404
    # 手机传回之后:状态 ready,文件可以取到,并带正确的类型
    path = tmp_path / "saved.jpg"
    path.write_bytes(JPEG)
    with ids["factory"]() as db:
        row = db.get(MomentsMedia, loaded["media_id"])
        moments_media.finish_upload(row, path, len(JPEG), "image/jpeg")
        db.commit()
    assert client.get(f"/moments/media/status?post_id={post_id}&mid=mid-img").json()["state"] == "ready"
    file = client.get(f"/moments/media/file/{loaded['media_id']}")
    assert file.status_code == 200 and file.headers["content-type"] == "image/jpeg" and file.content == JPEG and "private" in file.headers["cache-control"]
    # 页面里这张图已经是 ready,带地址(进入可视区自动显示)
    html = client.get(f"/moments?channel_id={channel_id}").text
    assert "/static/js/lightbox.js" in html  # 放大查看器全站引入(消息中心、朋友圈共用)
    assert 'data-state="ready"' in html and f'data-url="/moments/media/file/{loaded["media_id"]}"' in html and "点击查看" in html
    # 只读成员:可以看进度和文件,不能让手机去下载
    csrf = _login(client, "readonly")
    assert client.get(f"/moments/media/status?post_id={post_id}&mid=mid-img").status_code == 200
    assert client.get(f"/moments/media/file/{loaded['media_id']}").status_code == 200
    assert client.post("/moments/media/load", data={"post_id": post_id, "mid": "mid-img"}, headers={"X-CSRF-Token": csrf}).status_code == 403


def test_real_image_and_video_media_types_are_supported() -> None:
    """线上实测:图片动态的媒体类型是 1,视频是 5;之前误以为图片是 2,导致图片点击被判"暂不支持"。"""

    assert moments_media.SUPPORTED_MEDIA_TYPES == {1, 5}
