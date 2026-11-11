"""消息中心的消息显示:引用/链接/小程序/聊天记录/文件/媒体占位卡片的解析与渲染,以及媒体"点击才加载"的后端流程。

消息内容样例与线上真实格式一致(发送人前缀 + XML),内容与微信号都是虚构的。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="message-cards-logs-"))

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, Message, MessageAttachment, TaskJob
from app.services import inbound_media, message_media, message_view as mv
from app.services.wechat_webridge import service
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

HEAD = '<?xml version="1.0"?>'
QUOTE = f'demo_user_a: {HEAD}<msg><appmsg><title>[掩面]山没有绿色底色</title><type>57</type><refermsg><type>{{t}}</type><displayname>一</displayname><content>{{c}}</content></refermsg></appmsg></msg>'
LINK = f'wxid_x: {HEAD}<msg><appmsg><title>在 Apple TV 上玩马力欧</title><des>在 Mac 编译 Apple TV 上的游戏</des><type>5</type><url><![CDATA[https://example.com/a?b=1&c=2]]></url><sourcedisplayname>少数派</sourcedisplayname></appmsg></msg>'
MINI = 'wxid_x: <msg><appmsg><title>迷你主机 MEPro</title><des>京东购物</des><type>33</type></appmsg></msg>'
MERGED = 'demo_user_b: <msg><appmsg><title>群聊的聊天记录</title><des>甲: [图片] 乙: 好的</des><type>19</type></appmsg></msg>'
FILE = 'wxid_x: <msg><appmsg><title>报价单.pdf</title><type>6</type><appattach><totallen>1617467</totallen><fileext>pdf</fileext></appattach></appmsg></msg>'
IMAGE = f'demo_user_b: {HEAD}<msg><img aeskey="x" length="1234567" cdnthumbwidth="100" /></msg>'
VIDEO = f'wxid_x: {HEAD}<msg><videomsg aeskey="x" length="16662778" playlength="13" /></msg>'
VOICE = 'wxid_x: <msg><voicemsg voicelength="11438" length="20098" /></msg>'
EMOJI = 'wxid_x: <msg><emoji md5="d3a2" len="1195005" type="2" /></msg>'
RED = 'demo_user_b: <msg><appmsg><des><![CDATA[我给你发了一个红包，赶紧去拆!]]></des><url><![CDATA[https://wxapp.tenpay.com/mmpayhb/wxhb_personalreceive?x=1]]></url><type>2001</type></appmsg></msg>'
SYSTEM = '<img src="SystemMessages_HongbaoIcon.png"/> 你领取了小明的<_wc_custom_link_ color="#FD9931" href="weixin://x">红包</_wc_custom_link_>'


# ---------- 解析 ----------

def test_quote_shows_the_reply_and_what_was_quoted() -> None:
    view = mv.describe(QUOTE.format(t=1, c=" 因为是山"), "quote")
    assert (view["kind"], view["title"], view["refer_name"], view["refer_text"]) == ("quote", "[掩面]山没有绿色底色", "一", "因为是山")
    # 被引用的不是文字:显示类型标签;被引用的是卡片:取卡片标题
    assert mv.describe(QUOTE.format(t=3, c=""), "quote")["refer_text"] == "[图片]"
    assert mv.describe(QUOTE.format(t=43, c=""), "quote")["refer_text"] == "[视频]"
    nested = "&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;被引用的链接标题&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;"
    assert mv.describe(QUOTE.format(t=49, c=nested), "quote")["refer_text"] == "[卡片] 被引用的链接标题"


@pytest.mark.parametrize(
    "content, message_type, kind, tag",
    [(LINK, "link", "link", "链接"), (MINI, "mini_program", "mini_program", "小程序"), (MERGED, "merged_records", "merged", "聊天记录"), (RED, "app_unknown", "notice", "通知")],
)
def test_card_kinds_carry_title_description_and_source(content, message_type, kind, tag) -> None:
    view = mv.describe(content, message_type)
    assert view["kind"] == kind and view["tag"] == tag and view["title"]


def test_link_card_keeps_only_safe_urls_and_names_the_source() -> None:
    view = mv.describe(LINK, "link")
    assert view["url"] == "https://example.com/a?b=1&c=2" and view["source"] == "少数派" and view["desc"].startswith("在 Mac")
    assert mv.describe(LINK.replace("https://example.com/a?b=1&c=2", "javascript:alert(1)"), "link")["url"] == ""
    assert mv.describe(LINK.replace("https://example.com/a?b=1&c=2", "weixin://x"), "link")["url"] == ""


@pytest.mark.parametrize(
    "content, message_type, label, meta",
    [(IMAGE, "image", "图片", "1.2 MB"), (VIDEO, "video", "视频", "0:13 · 15.9 MB"), (VOICE, "voice", "语音", "0:11"), (EMOJI, "emoji", "表情", "1.1 MB")],
)
def test_media_messages_become_placeholder_cards_with_size_or_duration(content, message_type, label, meta) -> None:
    view = mv.describe(content, message_type)
    assert (view["kind"], view["media_type"], view["label"], view["meta"]) == ("media", message_type, label, meta)


def test_file_card_shows_the_file_name_extension_and_size() -> None:
    view = mv.describe(FILE, "file")
    assert (view["kind"], view["media_type"], view["title"], view["meta"]) == ("media", "file", "报价单.pdf", "PDF · 1.5 MB")


def test_text_is_untouched_system_messages_lose_their_tags_and_garbage_never_breaks() -> None:
    assert mv.describe("你好：今天 <很冷>吗", "text") == {"kind": "text", "text": "你好：今天 <很冷>吗".strip()} or mv.describe("你好：今天 吗", "text")["kind"] == "text"
    assert mv.describe("普通文字: 带冒号", "text") == {"kind": "text", "text": "普通文字: 带冒号"}  # 文字里的冒号不是发送人前缀
    system = mv.describe(SYSTEM, "system")
    assert system["kind"] == "system" and system["text"] == "你领取了小明的 红包"
    for broken in ("x: <msg><appmsg><title>没闭合", "x: <<<>>>", "", None):
        assert mv.describe(broken, "app")["kind"] in {"text", "unknown", "system"}
    huge = "x: <msg>" + "<a/>" * 30_000 + "</msg>"
    assert mv.describe(huge, "app")["kind"] in {"text", "unknown"}  # 超长内容不解析 XML
    assert mv.describe("g@chatroom: <sysmsg type='x'></sysmsg>", "unknown")["kind"] == "system"


def test_previews_are_short_labels_for_lists() -> None:
    assert mv.preview("还行", "text") == "还行"
    assert mv.preview(IMAGE, "image") == "[图片]" and mv.preview(VOICE, "voice") == "[语音]"
    assert mv.preview(QUOTE.format(t=1, c="x"), "quote") == "[引用] [掩面]山没有绿色底色"
    assert mv.preview(LINK, "link") == "[链接] 在 Apple TV 上玩马力欧" and mv.preview(FILE, "file") == "[文件] 报价单.pdf"
    assert len(mv.preview("长" * 500, "text", limit=30)) == 30 and mv.preview("长" * 500, "text", limit=30).endswith("…")


def test_attachment_state_decides_whether_a_media_card_is_ready() -> None:
    class A:
        def __init__(self, kind, status, path=None, id=1):
            self.attachment_type, self.status, self.storage_path, self.id = kind, status, path, id

    def state(attachments):
        return mv.attach_media(mv.describe(IMAGE, "image"), attachments)

    assert state([])["state"] == "none"
    assert state([A("image", "pending")])["state"] == "pending"
    assert state([A("image", "failed")])["state"] == "failed"
    ready = state([A("image", "pending", id=1), A("image", "downloaded", "/x/1.jpg", id=7)])  # 取最新一条
    assert (ready["state"], ready["url"]) == ("ready", "/media/attachments/7")
    assert state([A("video", "downloaded", "/x/1.mp4")])["state"] == "none"  # 别的类型的附件不算
    assert mv.attach_media(mv.describe("文字", "text"), [A("image", "downloaded", "/x")]) == {"kind": "text", "text": "文字"}


# ---------- 页面 ----------

@pytest.fixture()
def conversation_page(provider_page_context, tmp_path):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True, api_base="http://fulcrum.test/api/wechat-webridge/1")
        db.add(channel)
        db.flush()
        conversation = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group", title="测试群")
        db.add(conversation)
        db.flush()
        rows = {}
        for number, (key, content, message_type) in enumerate(
            [("text", "普通文字消息", "text"), ("quote", QUOTE.format(t=1, c="被引用的话"), "quote"), ("link", LINK, "link"), ("mini", MINI, "mini_program"),
             ("merged", MERGED, "merged_records"), ("file", FILE, "file"), ("image", IMAGE, "image"), ("video", VIDEO, "video"), ("voice", VOICE, "voice"),
             ("emoji", EMOJI, "emoji"), ("ready_image", IMAGE, "image"), ("system", SYSTEM, "system"), ("odd", "wxid_a: <msg><weird/></msg>", "app")], start=1
        ):
            message = Message(tenant_id=ids["alpha"], conversation_id=conversation.id, direction="in", sender_id="wxid_a", sender_name="小明", content=content,
                              message_type=message_type, external_message_id=f"wx{number}", status="received")
            db.add(message)
            db.flush()
            rows[key] = int(message.id)
        db.add(MessageAttachment(tenant_id=ids["alpha"], message_id=rows["ready_image"], channel_id=channel.id, conversation_id=conversation.id, attachment_type="image", provider="wechat_webridge", status="downloaded", storage_path=str(tmp_path / "a.jpg")))
        db.add(MessageAttachment(tenant_id=ids["alpha"], message_id=rows["video"], channel_id=channel.id, conversation_id=conversation.id, attachment_type="video", provider="wechat_webridge", status="pending"))
        db.commit()
        return client, ids, int(conversation.id), rows, int(channel.id)


def _row(html: str, message_id: int) -> str:
    start = html.index(f'data-id="{message_id}"')
    nxt = html.find('class="wechat-message-row', start + 10)
    return html[start: nxt if nxt != -1 else len(html)]


def test_each_kind_renders_as_its_own_card(conversation_page) -> None:
    client, _ids, conversation_id, rows, _channel_id = conversation_page
    _login(client, "admin")
    html = client.get(f"/messages?conversation_id={conversation_id}").text
    assert "普通文字消息" in _row(html, rows["text"])
    quote = _row(html, rows["quote"])
    assert 'class="msg-quote"' in quote and "[掩面]山没有绿色底色" in quote and "被引用的话" in quote and "<strong>一</strong>" in quote
    link = _row(html, rows["link"])
    assert 'href="https://example.com/a?b=1&amp;c=2"' in link and 'rel="noopener noreferrer"' in link and 'target="_blank"' in link and "少数派" in link
    assert "小程序" in _row(html, rows["mini"]) and "聊天记录" in _row(html, rows["merged"]) and "甲: [图片] 乙: 好的" in _row(html, rows["merged"])
    assert "你领取了小明的 红包" in _row(html, rows["system"])
    odd = _row(html, rows["odd"])
    assert "暂不支持显示的消息" in odd and '<details class="disclosure msg-raw">' in odd and "&lt;weird/&gt;" in odd  # 暂不支持的消息:有"原始内容"折叠,用统一的展开图标
    # 看不出结构的 XML 文字不再被拼成 "tag: value · …" 这样的乱码
    assert "appmsg:" not in html and "title:" not in html


def test_media_are_placeholder_cards_and_nothing_is_loaded_up_front(conversation_page) -> None:
    client, _ids, conversation_id, rows, _channel_id = conversation_page
    _login(client, "admin")
    html = client.get(f"/messages?conversation_id={conversation_id}").text
    # 占位卡片:带消息 ID、类型和状态;页面里没有任何已嵌入的图片/视频/音频元素
    assert "<video" not in html and "<audio" not in html and "/media/attachments/" not in html.replace('data-url="/media/attachments/', "")
    states = {key: re.search(r'data-state="(\w+)"', _row(html, rows[key])).group(1) for key in ("image", "video", "voice", "emoji", "file", "ready_image")}
    assert states == {"image": "none", "video": "pending", "voice": "none", "emoji": "none", "file": "none", "ready_image": "ready"}
    ready = _row(html, rows["ready_image"])
    assert f'data-url="/media/attachments/' in ready and "点击查看" in ready
    image = _row(html, rows["image"])
    assert f'data-message-id="{rows["image"]}"' in image and 'data-media-type="image"' in image and "1.2 MB · 点击从手机获取" in image and 'role="button"' in image
    assert "0:13 · 15.9 MB" in _row(html, rows["video"]) and "0:11" in _row(html, rows["voice"]) and "报价单.pdf" in _row(html, rows["file"]) and "PDF · 1.5 MB" in _row(html, rows["file"])
    assert "/static/js/message-media.js" in html


def test_conversation_list_preview_uses_short_labels(conversation_page) -> None:
    client, ids, conversation_id, rows, channel_id = conversation_page
    _login(client, "admin")
    with ids["factory"]() as db:
        db.add(Message(tenant_id=ids["alpha"], conversation_id=conversation_id, direction="in", sender_id="wxid_a", content=IMAGE, message_type="image", external_message_id="wxlast", status="received"))
        conversation = db.get(Conversation, conversation_id)
        conversation.last_message_at = now_shanghai()
        db.commit()
    html = client.get("/messages").text
    preview = re.search(r'<span class="wechat-conversation-preview">(.*?)</span>', html).group(1)
    assert preview == "[图片]"


# ---------- 点击才加载:服务层 ----------

@pytest.fixture()
def media_env(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound_media, "_build_attachment_path", lambda channel_id, attachment_id, suffix: tmp_path / f"{channel_id}-{attachment_id}{suffix}")
    db = _session()
    channel = _seed(db)
    channel.api_base = "http://fulcrum.test/api/wechat-webridge/1"
    conversation = Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
    message = Message(tenant_id=1, conversation_id=3, direction="in", sender_id="wxid_a", content=IMAGE, message_type="image", external_message_id="3451183832849212577", status="received")
    db.add_all([conversation, message])
    db.commit()
    return db, channel, message


def _uploads(db) -> list[tuple[TaskJob, dict]]:
    rows = []
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE).order_by(TaskJob.id)).all():
        command = json.loads(job.payload_json).get("command", {})
        if command.get("type") == "upload_message_media":
            rows.append((job, command))
    return rows


def test_first_click_asks_the_phone_to_upload_and_repeated_clicks_do_not_ask_again(media_env) -> None:
    db, channel, message = media_env
    assert message_media.status(db, message)["state"] == "none"
    first = message_media.request(db, message)
    db.commit()
    assert first["state"] == "loading" and first["attachment_id"]
    (job, command), = _uploads(db)
    attachment = db.get(MessageAttachment, first["attachment_id"])
    # 用微信消息编号和附件上传地址下发给手机(与入站自动下载同一套命令)
    assert command["params"] == {
        "message_id": "3451183832849212577", "media_type": "image",
        "upload_url": f"http://fulcrum.test/api/wechat-webridge/1/agent/upload_media?attachment_id={attachment.id}", "attachment_id": attachment.id,
    }
    assert message_media.request(db, message)["state"] == "loading" and len(_uploads(db)) == 1  # 获取中:不重复下发


def test_when_the_phone_uploads_the_file_the_card_becomes_ready(media_env) -> None:
    db, channel, message = media_env
    attachment_id = message_media.request(db, message)["attachment_id"]
    db.commit()
    inbound_media.store_uploaded_media_attachment(db, attachment=db.get(MessageAttachment, attachment_id), content=b"\xff\xd8jpeg", filename="a.jpg", content_type="image/jpeg")
    db.commit()
    done = message_media.status(db, message)
    assert done == {"state": "ready", "attachment_id": attachment_id, "url": f"/media/attachments/{attachment_id}", "error": ""}
    assert message_media.request(db, message)["state"] == "ready" and len(_uploads(db)) == 1  # 已有文件:直接返回,不再下发


def test_failures_and_timeouts_are_reported_and_a_retry_asks_again(media_env) -> None:
    db, channel, message = media_env
    message_media.request(db, message)
    db.commit()
    job, _command = _uploads(db)[0]
    job.status, job.error_message = "failed", "手机上找不到这个文件"
    db.commit()
    failed = message_media.status(db, message)
    assert failed["state"] == "failed" and "找不到" in failed["error"]
    retry = message_media.request(db, message)  # 点击重试:重新下发
    db.commit()
    assert retry["state"] == "loading" and len(_uploads(db)) == 2
    # 超时:手机一直不传回来
    attachment = db.get(MessageAttachment, retry["attachment_id"])
    meta = json.loads(attachment.metadata_json)
    meta["requested_at"] = (now_shanghai() - timedelta(seconds=message_media.REQUEST_TIMEOUT_SECONDS + 5)).isoformat()
    attachment.metadata_json = json.dumps(meta)
    db.commit()
    timed_out = message_media.status(db, message)
    assert timed_out["state"] == "failed" and "超时" in timed_out["error"]


def test_unsupported_channels_and_messages_without_a_wechat_id_fail_with_a_reason(media_env) -> None:
    db, channel, message = media_env
    message.external_message_id = None
    assert "缺少微信消息编号" in message_media.request(db, message)["error"]
    message.external_message_id = "wx1"
    channel.channel_type = "wechat_ipad"
    assert "暂不支持" in message_media.request(db, message)["error"] and _uploads(db) == []
    text = Message(tenant_id=1, conversation_id=3, direction="in", content="文字", message_type="text")
    db.add(text)
    db.commit()
    with pytest.raises(ValueError):
        message_media.status(db, text)


def test_upload_url_prefers_the_channel_setting_and_makes_relative_ones_absolute() -> None:
    channel = Channel(id=2, name="x", channel_type="wechat_webridge", api_base="http://fulcrum.test/api/wechat-webridge/2/")
    assert service.build_upload_url(channel, 9) == "http://fulcrum.test/api/wechat-webridge/2/agent/upload_media?attachment_id=9"
    channel.adapter_config_json = json.dumps({"upload_url": "/agent/upload_media?attachment_id=5"})
    assert service.build_upload_url(channel, 9) == "http://fulcrum.test/api/wechat-webridge/2/agent/upload_media?attachment_id=5"
    channel.adapter_config_json = json.dumps({"upload_url": "https://cdn.example.com/up"})
    assert service.build_upload_url(channel, 9) == "https://cdn.example.com/up"


def test_event_time_strings_can_be_parsed() -> None:
    """channel_events 里解析事件时间的函数用到了没导入的 ensure_shanghai_datetime,带 ISO 时间的事件会直接 NameError。"""

    from datetime import datetime

    from app.services.channel_events import _parse_timestamp_value

    assert _parse_timestamp_value("2026-11-10T12:00:00Z").hour == 20  # UTC 12 点 = 上海 20 点
    assert _parse_timestamp_value(datetime(2026, 11, 10, 12, 0, 0)).hour == 12 and _parse_timestamp_value(1794300000).year == 2026


# ---------- 点击才加载:接口 ----------

def test_media_endpoints_check_csrf_roles_and_message_type(conversation_page) -> None:
    client, ids, conversation_id, rows, _channel_id = conversation_page
    csrf = _login(client, "admin")
    headers = {"X-CSRF-Token": csrf, "X-Requested-With": "fetch"}
    assert client.post("/messages/media/load", data={"message_id": rows["image"]}).status_code == 403  # 没有令牌
    loaded = client.post("/messages/media/load", data={"message_id": rows["image"]}, headers=headers).json()
    assert loaded["ok"] is True and loaded["state"] == "loading" and loaded["attachment_id"]
    assert client.get(f"/messages/media/status?message_id={rows['image']}").json()["state"] == "loading"
    # 已下载的:直接 ready(带地址),不下发命令
    ready = client.post("/messages/media/load", data={"message_id": rows["ready_image"]}, headers=headers).json()
    assert ready["state"] == "ready" and ready["url"].startswith("/media/attachments/")
    assert client.post("/messages/media/load", data={"message_id": rows["text"]}, headers=headers).status_code == 400
    assert client.post("/messages/media/load", data={"message_id": 999999}, headers=headers).status_code == 404
    # 只读成员可以看进度,但不能让手机去传文件
    csrf = _login(client, "readonly")
    assert client.get(f"/messages/media/status?message_id={rows['image']}").status_code == 200
    assert client.post("/messages/media/load", data={"message_id": rows["voice"]}, headers={"X-CSRF-Token": csrf}).status_code == 403
