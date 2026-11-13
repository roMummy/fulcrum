"""用真实模板、样式和脚本验证消息交互；浏览器请求全部拦截，不连接实际账号。"""

from __future__ import annotations

import json
import time
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

from app.services.message_view import preview

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/style.css"></head><body><main class="workspace">
<h1>消息中心</h1>{% block content %}{% endblock %}</main></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runtime:
        options = {"headless": True}
        if not Path(runtime.chromium.executable_path).is_file():
            edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
            if not edge.is_file():
                pytest.skip("需要本机 Chromium 或 Edge 才能验证消息页面交互")
            options["executable_path"] = str(edge)
        instance = runtime.chromium.launch(**options)
        yield instance
        instance.close()


class MessagePage:
    def __init__(self, page, *, count=20, supported=True, chat_type="group", selected=True):
        self.page = page
        self.messages = []
        self.identities = {}
        self.views = {}
        self.sent = []
        self.revoked = []
        self.history_calls = []
        self.removed_ids = []
        self.list_calls = []
        self.member_queries = []
        self.members_fail = False
        self.members = [
            {"id": "notify@all", "name": "所有人", "initial": "全"},
            {"id": "wxid_friend", "name": "小明", "initial": "明"},
            {"id": "wxid_red", "name": "群昵称小红", "initial": "红"},
        ]
        self.selected = selected
        self.send_ok = True
        self.capabilities = dict.fromkeys(("image", "voice", "video", "emoji", "file", "quote", "mention", "revoke"), supported)
        self.conversation = SimpleNamespace(id=7, chat_type=chat_type)
        self.env = Environment(
            loader=ChoiceLoader([DictLoader({"base.html": BASE}), FileSystemLoader(ROOT / "app/templates")]),
            autoescape=select_autoescape(["html"]),
        )
        self.env.filters["bj_time"] = lambda value: value
        self.env.filters["message_display"] = preview
        self.add(count)
        self.conversation_items = ([{"id": 7, "display_name": "测试群聊", "preview": f"第 {count} 条测试消息", "time": "12:00", "avatar_url": "", "initial": "测"}] if count else [])
        self.page_errors = []
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.route("http://messages.test/**", self.route)
        page.clock.install()
        page.goto("http://messages.test/messages?conversation_id=7")
        playwright.expect(page.locator("#msg-center")).to_be_visible()
        if selected:
            playwright.expect(page.locator("#wechat-composer")).to_be_visible()

    def add(self, count):
        for _ in range(count):
            number = len(self.messages) + 1
            own = number % 2 == 0
            self.messages.append(SimpleNamespace(
                id=number, direction="in", sender_id="wxid_bot" if own else "wxid_friend",
                content=f"第 {number} 条测试消息", message_type="text", status="sent", created_at="2026-11-13 12:00:00",
            ))
            self.identities[number] = {"name": "机器人" if own else "小明", "initial": "机" if own else "明", "is_own": own}
            self.views[number] = {
                "kind": "text", "text": f"第 {number} 条测试消息", "version": str(number),
                "revoke_until": int(time.time() * 1000) + 120_000, "revoke_state": "",
            }

    def rows(self, messages):
        return str(self.env.get_template("_message_rows.html").module.message_rows(messages, self.identities, self.views))

    def html(self):
        return self.env.get_template("messages.html").render(
            conversation_summaries=([{"conversation": self.conversation, "identity": {}, "display_name": "测试群聊", "initial": "测", "latest": self.messages[-1]}] if self.messages else []),
            selected_conversation_id=7 if self.selected else None, has_more_messages=False, selected_chatroom=None, channel_id=0,
            messages=self.messages, message_identities=self.identities, message_views=self.views, csrf_token="test-csrf",
        )

    @staticmethod
    def fields(request):
        content = request.post_data_buffer or b""
        content_type = request.headers["content-type"]
        message = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content)
        return {
            part.get_param("name", header="content-disposition"): (part.get_filename() or part.get_payload(decode=True).decode("utf-8"))
            for part in message.iter_parts()
        }

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.path.startswith("/static/"):
            file = ROOT / "app" / parsed.path.lstrip("/")
            if file.is_file():
                route.fulfill(path=str(file))
            else:
                route.abort()
            return
        if parsed.path == "/messages":
            route.fulfill(content_type="text/html", body=self.html())
            return
        if parsed.path == "/messages/conversations":
            self.list_calls.append(parse_qs(parsed.query))
            payload = {"ok": True, "conversations": self.conversation_items}
        elif parsed.path == "/messages/capabilities":
            payload = {"ok": True, "capabilities": self.capabilities}
        elif parsed.path == "/messages/mention-members":
            query = parse_qs(parsed.query, keep_blank_values=True).get("q", [""])[0]
            self.member_queries.append(query)
            if self.members_fail:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"ok": False, "notice": "群成员加载失败"}))
                return
            payload = {"ok": True, "members": [member for member in self.members if query in member["name"]], "has_more": False}
        elif parsed.path == "/messages/history":
            query = parse_qs(parsed.query)
            self.history_calls.append(query)
            after = int(query.get("after_id", ["0"])[0])
            newer = [message for message in self.messages if message.id > after and message.status != "echo"]
            sync = {int(value) for value in query.get("sync_ids", [""])[0].split(",") if value}
            payload = {"html": self.rows(newer[:30]), "has_more": len(newer) > 30, "count": len(newer[:30]),
                       "updates_html": self.rows([message for message in self.messages if message.id in sync and message.status != "echo"]),
                       "removed_ids": [message_id for message_id in self.removed_ids if message_id in sync]}
        elif parsed.path == "/messages/send":
            self.sent.append(self.fields(request))
            payload = {"ok": self.send_ok, "notice": "已提交" if self.send_ok else "手机离线，发送失败"}
        elif parsed.path == "/messages/revoke":
            fields = self.fields(request)
            self.revoked.append(fields)
            self.views[int(fields["message_id"])]["revoke_state"] = "pending"
            payload = {"ok": True, "notice": "撤回请求已提交"}
        else:
            route.fulfill(status=404, body="仅允许测试请求")
            return
        route.fulfill(content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    def poll(self):
        previous = len(self.history_calls)
        self.page.clock.fast_forward(2600)
        self.page.wait_for_function("() => true")
        assert len(self.history_calls) > previous

    def paste(self, files, text=""):
        return self.page.locator("textarea").evaluate("""(editor, value) => {
            const clipboard = new DataTransfer();
            for (const file of value.files) clipboard.items.add(new File([new Uint8Array(file.bytes || [1, 2, 3])], file.name, {type: file.type}));
            if (value.text) clipboard.setData('text/plain', value.text);
            const event = new ClipboardEvent('paste', {clipboardData: clipboard, bubbles: true, cancelable: true});
            editor.dispatchEvent(event);
            return event.defaultPrevented;
        }""", {"files": files, "text": text})


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    yield page
    context.close()


def test_quote_own_and_incoming_messages_mentions_and_revoke(page):
    screen = MessagePage(page)
    own = page.locator('[data-id="20"]')
    own_bubble = own.locator(".wechat-message-bubble")
    assert own_bubble.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(157, 242, 159)"
    assert own.evaluate("el => getComputedStyle(el).flexDirection") == "row-reverse"
    own_bubble.click(button="right")
    playwright.expect(page.locator('[data-message-action="quote"]')).to_be_visible()
    playwright.expect(page.locator('[data-message-action="revoke"]')).to_be_visible()
    page.locator('[data-message-action="quote"]').click()
    playwright.expect(page.locator("#wechat-reply")).to_contain_text("第 20 条测试消息")
    page.wait_for_function("() => { const el = document.getElementById('wechat-thread-body'); return el.scrollHeight - el.scrollTop - el.clientHeight <= 1; }")
    page.locator("textarea").fill("引用自己的消息")
    page.locator("textarea").press("Enter")
    playwright.expect(page.locator("textarea")).to_have_value("")
    assert screen.sent[-1]["reply_to_id"] == "20"

    incoming = page.locator('[data-id="19"]')
    incoming.locator(".wechat-message-bubble").click(button="right")
    playwright.expect(page.locator('[data-message-action="revoke"]')).to_be_hidden()
    page.locator('[data-message-action="quote"]').click()
    playwright.expect(page.locator("#wechat-reply")).to_contain_text("小明")
    page.locator("[data-clear-reply]").click()
    incoming.locator(".wechat-avatar").click(button="right")
    page.locator('[data-message-action="mention"]').click()
    playwright.expect(page.locator("#wechat-mentions")).to_contain_text("@小明")
    playwright.expect(page.locator("textarea")).to_have_value("@小明\u2005")
    page.locator("textarea").press("End")
    page.locator("textarea").press_sequentially("请看一下")
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("textarea")).to_have_value("")
    assert json.loads(screen.sent[-1]["mention_ids"]) == ["wxid_friend"]
    assert screen.sent[-1]["content"] == "{{mention:wxid_friend}}请看一下"

    own_bubble.click(button="right")
    page.locator('[data-message-action="revoke"]').click()
    playwright.expect(own).to_contain_text("撤回中")
    assert screen.revoked[-1]["message_id"] == "20"
    assert screen.revoked[-1]["csrf_token"] == "test-csrf"
    screen.views[20]["revoke_state"] = "revoked"
    screen.messages[19].status = "revoked"
    screen.poll()
    playwright.expect(own).to_contain_text("你撤回了一条消息")
    assert not screen.page_errors


def test_polling_drains_pages_without_duplicates_and_preserves_scroll(page):
    screen = MessagePage(page)
    body = page.locator("#wechat-thread-body")
    body.evaluate("el => { el.scrollTop = 200; el.dispatchEvent(new Event('scroll')); }")
    previous = body.evaluate("el => el.scrollTop")
    screen.add(65)
    page.locator("textarea").fill("轮询期间的草稿")
    screen.poll()
    playwright.expect(page.locator(".wechat-message-row")).to_have_count(85)
    playwright.expect(page.locator("textarea")).to_have_value("轮询期间的草稿")
    assert abs(body.evaluate("el => el.scrollTop") - previous) <= 1
    screen.poll()
    playwright.expect(page.locator(".wechat-message-row")).to_have_count(85)
    assert page.locator(".wechat-message-row").evaluate_all("rows => new Set(rows.map(el => el.dataset.id)).size") == 85
    assert not screen.page_errors


def test_polling_removes_reconciled_echo_and_keeps_genuine_repeated_text(page):
    screen = MessagePage(page, count=4)
    for message in screen.messages:
        message.content = "正常重复发送" if message.id <= 2 else "本地发送和手机回显"
        message.sender_id = "wxid_bot"
        screen.identities[message.id]["is_own"] = True
        screen.views[message.id]["text"] = message.content
    screen.messages[2].status = "pending"
    page.reload()
    playwright.expect(page.locator(".wechat-message-row")).to_have_count(4)
    playwright.expect(page.locator(".wechat-message-content", has_text="本地发送和手机回显")).to_have_count(2)

    # 最后一张卡恰好是回显；删除后游标会后退，下一次拉取仍不能带回它。
    screen.messages[2].status = "sent"
    screen.views[3]["version"] = "canonical-ack"
    screen.messages[3].status = "echo"
    screen.removed_ids = [4]
    screen.poll()
    playwright.expect(page.locator(".wechat-message-row")).to_have_count(3)
    playwright.expect(page.locator('[data-id="4"]')).to_have_count(0)
    playwright.expect(page.locator('[data-id="3"]')).to_have_attribute("data-status", "sent")
    playwright.expect(page.locator('[data-id="3"]')).to_have_attribute("data-version", "canonical-ack")
    playwright.expect(page.locator(".wechat-message-content", has_text="正常重复发送")).to_have_count(2)

    screen.add(1)
    screen.poll()
    playwright.expect(page.locator(".wechat-message-row")).to_have_count(4)
    assert page.locator(".wechat-message-row").evaluate_all("rows => rows.map(el => el.dataset.id)") == ["1", "2", "3", "5"]
    playwright.expect(page.locator(".wechat-message-content", has_text="正常重复发送")).to_have_count(2)
    screen.poll()
    playwright.expect(page.locator('[data-id="4"]')).to_have_count(0)
    playwright.expect(page.locator(".wechat-message-row")).to_have_count(4)
    assert not screen.page_errors


def test_conversation_list_discovers_first_conversation_and_updates_preview(page):
    screen = MessagePage(page, count=0, selected=False)
    playwright.expect(page.locator(".wechat-conversation-list")).to_contain_text("暂无会话消息")
    screen.conversation_items = [{"id": 9, "display_name": "新朋友", "preview": "第一条消息", "time": "12:30", "avatar_url": "", "initial": "新"}]
    page.clock.fast_forward(10_100)
    item = page.locator('.wechat-conversation-item[data-conversation-id="9"]')
    playwright.expect(item).to_contain_text("第一条消息")
    assert "conversation_id=9" in item.get_attribute("href")
    screen.conversation_items[0]["preview"] = "新收到的图片"
    page.clock.fast_forward(10_100)
    playwright.expect(item).to_contain_text("新收到的图片")
    assert len(screen.list_calls) == 2
    assert not screen.history_calls and not screen.page_errors


def test_media_capabilities_upload_and_failed_text_preserve_draft(page):
    screen = MessagePage(page, supported=False)
    chosen = []
    page.on("filechooser", lambda _chooser: chosen.append(True))
    page.locator('[data-send-media="video"]').click()
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("不支持发送视频")
    assert not chosen
    # 成功能力在下一次会话打开时取得；当前页面不能伪造为支持。
    screen.capabilities = dict.fromkeys(screen.capabilities, True)
    page.reload()
    with page.expect_file_chooser() as selected:
        page.locator('[data-send-media="image"]').click()
    selected.value.set_files({"name": "测试图片.png", "mimeType": "image/png", "buffer": b"\x89PNG\r\n\x1a\n"})
    playwright.expect(page.locator("#wechat-attachment")).to_contain_text("测试图片.png")
    page.locator("textarea").fill("图片后继续发送的文字")
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    assert screen.sent[-1]["message_type"] == "image" and screen.sent[-1]["file"] == "测试图片.png"
    assert screen.sent[-1]["csrf_token"] == "test-csrf"
    assert screen.sent[-1]["content"] == ""
    playwright.expect(page.locator("textarea")).to_have_value("图片后继续发送的文字")
    screen.send_ok = False
    page.locator("textarea").fill("失败时保留的草稿")
    page.locator("textarea").dispatch_event("keydown", {"key": "Enter", "isComposing": True})
    assert len(screen.sent) == 1
    page.locator("textarea").press("Enter")
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("手机离线")
    playwright.expect(page.locator("textarea")).to_have_value("失败时保留的草稿")
    assert not screen.page_errors


@pytest.mark.parametrize("name,mime,default_type,types", [
    ("粘贴图片.png", "image/png", "image", ["file", "image", "emoji"]),
    ("粘贴视频.mp4", "video/mp4", "video", ["file", "video"]),
    ("粘贴音乐.mp3", "audio/mpeg", "file", ["file", "voice"]),
    ("粘贴文档.pdf", "application/pdf", "file", ["file"]),
])
def test_pasted_attachment_waits_for_send_and_keeps_text(page, name, mime, default_type, types):
    screen = MessagePage(page)
    page.locator("textarea").fill("原来的文字")
    assert screen.paste([{"name": name, "type": mime}])
    playwright.expect(page.locator("#wechat-attachment")).to_contain_text(name)
    selection = page.locator("#wechat-attachment-type")
    playwright.expect(selection).to_have_value(default_type)
    assert selection.locator("option").evaluate_all("options => options.map(option => option.value)") == types
    assert not screen.sent
    playwright.expect(page.locator("textarea")).to_have_value("原来的文字")
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    assert screen.sent[-1]["message_type"] == default_type
    assert screen.sent[-1]["file"] == name and screen.sent[-1]["content"] == ""
    playwright.expect(page.locator("textarea")).to_have_value("原来的文字")
    assert not screen.page_errors


def test_attachment_type_switch_preserves_unsupported_file_and_can_fall_back(page):
    screen = MessagePage(page)
    screen.capabilities["voice"] = False
    page.reload()
    screen.paste([{"name": "音乐.mp3", "type": "audio/mpeg"}])
    selection = page.locator("#wechat-attachment-type")
    selection.select_option("voice")
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("不支持发送语音")
    playwright.expect(page.locator("#wechat-attachment")).to_contain_text("音乐.mp3")
    page.locator(".wechat-send").click()
    assert not screen.sent
    selection.select_option("file")
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    assert screen.sent[-1]["message_type"] == "file"
    assert not screen.page_errors


def test_voice_toolbar_keeps_selected_type_and_pasted_image_can_be_an_emoji(page):
    screen = MessagePage(page)
    with page.expect_file_chooser() as selected:
        page.locator('[data-send-media="voice"]').click()
    selected.value.set_files({"name": "语音.mp3", "mimeType": "audio/mpeg", "buffer": b"test audio"})
    playwright.expect(page.locator("#wechat-attachment-type")).to_have_value("voice")
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    assert screen.sent[-1]["message_type"] == "voice"
    screen.paste([{"name": "表情.png", "type": "image/png"}])
    page.locator("#wechat-attachment-type").select_option("emoji")
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    assert screen.sent[-1]["message_type"] == "emoji"
    assert not screen.page_errors


def test_typing_mentions_searches_names_and_preserves_inline_positions_on_failure(page):
    screen = MessagePage(page)
    editor = page.locator("textarea")
    editor.fill("你好 @")
    page.clock.fast_forward(180)
    playwright.expect(page.get_by_role("option")).to_have_count(3)
    editor.press("ArrowDown")
    editor.press("Enter")
    playwright.expect(editor).to_have_value("你好 @小明\u2005")
    editor.press_sequentially("请看@群")
    page.clock.fast_forward(180)
    page.get_by_role("option", name="群昵称小红", exact=True).click()
    draft = "你好 @小明\u2005请看@群昵称小红\u2005"
    playwright.expect(editor).to_have_value(draft)
    assert "群" in screen.member_queries
    screen.send_ok = False
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("手机离线")
    playwright.expect(editor).to_have_value(draft)
    playwright.expect(page.locator("#wechat-mentions button")).to_have_count(2)
    assert screen.sent[-1]["content"] == "你好 {{mention:wxid_friend}}请看{{mention:wxid_red}}"
    assert json.loads(screen.sent[-1]["mention_ids"]) == ["wxid_friend", "wxid_red"]
    screen.send_ok = True
    page.locator(".wechat-send").click()
    playwright.expect(editor).to_have_value("")
    assert not screen.page_errors


def test_all_members_can_be_selected_and_sent_without_other_text(page):
    screen = MessagePage(page)
    editor = page.locator("textarea")
    editor.fill("@")
    page.clock.fast_forward(180)
    playwright.expect(page.get_by_role("option", name="@所有人", exact=True)).to_be_visible()
    editor.press("Enter")
    playwright.expect(editor).to_have_value("@所有人\u2005")
    assert not screen.sent
    editor.press("Enter")
    playwright.expect(editor).to_have_value("")
    assert screen.sent[-1]["content"] == "{{mention:notify@all}}"
    assert json.loads(screen.sent[-1]["mention_ids"]) == ["notify@all"]
    assert not screen.page_errors


def test_mention_token_removal_keeps_matching_handwritten_body(page):
    screen = MessagePage(page)
    editor = page.locator("textarea")
    editor.fill("@小")
    page.clock.fast_forward(180)
    page.get_by_role("option", name="小明", exact=True).click()
    editor.press_sequentially("普通@小明\u2005正文")
    page.get_by_role("button", name="取消提及小明", exact=True).click()
    playwright.expect(editor).to_have_value("普通@小明\u2005正文")
    page.locator(".wechat-send").click()
    playwright.expect(editor).to_have_value("")
    assert screen.sent[-1]["content"] == "普通@小明\u2005正文"
    assert json.loads(screen.sent[-1]["mention_ids"]) == []
    editor.fill("@小")
    page.clock.fast_forward(180)
    page.get_by_role("option", name="小明", exact=True).click()
    editor.press("Control+A")
    editor.press("Backspace")
    playwright.expect(page.locator("#wechat-mentions")).to_be_hidden()
    playwright.expect(page.locator(".wechat-send")).to_be_disabled()
    assert not screen.page_errors


def test_mention_picker_handles_ime_empty_results_and_load_failure_without_sending(page):
    screen = MessagePage(page)
    editor = page.locator("textarea")
    editor.fill("@")
    page.clock.fast_forward(180)
    playwright.expect(page.get_by_role("option")).to_have_count(3)
    editor.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
    playwright.expect(editor).to_have_value("@")
    assert not screen.sent
    editor.fill("@不存在")
    page.clock.fast_forward(180)
    playwright.expect(page.locator("#wechat-mention-picker")).to_contain_text("没有找到群成员")
    editor.press("Enter")
    assert not screen.sent
    playwright.expect(editor).to_have_value("@不存在")
    editor.press("Escape")
    playwright.expect(page.locator("#wechat-mention-picker")).to_be_hidden()
    screen.members_fail = True
    editor.fill("@小")
    page.clock.fast_forward(180)
    playwright.expect(page.locator("#wechat-mention-picker")).to_contain_text("加载失败")
    assert not screen.sent
    screen.members_fail = False
    editor.press_sequentially("明")
    page.clock.fast_forward(180)
    playwright.expect(page.get_by_role("option", name="小明", exact=True)).to_be_visible()
    assert not screen.page_errors


def test_member_search_ignores_an_older_response(page):
    screen = MessagePage(page)
    pending = []

    def delayed(route):
        query = parse_qs(urlsplit(route.request.url).query).get("q", [""])[0]
        if query == "小":
            pending.append(route)
        else:
            route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "members": [screen.members[2]], "has_more": False}))

    page.route("http://messages.test/messages/mention-members?**", delayed)
    editor = page.locator("textarea")
    with page.expect_request(lambda request: parse_qs(urlsplit(request.url).query).get("q") == ["小"]):
        editor.fill("@小")
        page.clock.fast_forward(180)
    page.wait_for_timeout(10)
    assert len(pending) == 1
    editor.fill("@小红")
    page.clock.fast_forward(180)
    playwright.expect(page.get_by_role("option", name="群昵称小红", exact=True)).to_be_visible()
    with page.expect_response(lambda response: parse_qs(urlsplit(response.url).query).get("q") == ["小"]):
        pending[0].fulfill(content_type="application/json", body=json.dumps({"ok": True, "members": [screen.members[1]], "has_more": False}))
    playwright.expect(page.get_by_role("option")).to_have_count(1)
    playwright.expect(page.get_by_role("option", name="群昵称小红", exact=True)).to_be_visible()
    assert not screen.page_errors


def test_direct_conversations_do_not_open_the_member_picker(page):
    screen = MessagePage(page, chat_type="direct")
    page.locator("textarea").fill("@")
    page.clock.fast_forward(180)
    playwright.expect(page.locator("#wechat-mention-picker")).to_be_hidden()
    assert screen.member_queries == []
    assert not screen.page_errors


def test_unsupported_mentions_do_not_fetch_members_and_attachment_send_keeps_tokens(page):
    screen = MessagePage(page)
    screen.capabilities["mention"] = False
    page.reload()
    editor = page.locator("textarea")
    editor.fill("@")
    page.clock.fast_forward(180)
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("不支持 @ 群成员")
    playwright.expect(page.locator("#wechat-mention-picker")).to_be_hidden()
    assert not screen.member_queries
    screen.capabilities["mention"] = True
    page.reload()
    editor.fill("@小")
    page.clock.fast_forward(180)
    page.get_by_role("option", name="小明", exact=True).click()
    screen.paste([{"name": "资料.pdf", "type": "application/pdf"}])
    page.locator(".wechat-send").click()
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    assert json.loads(screen.sent[-1]["mention_ids"]) == []
    playwright.expect(editor).to_have_value("@小明\u2005")
    playwright.expect(page.locator("#wechat-mentions")).to_contain_text("@小明")
    page.locator(".wechat-send").click()
    playwright.expect(editor).to_have_value("")
    assert screen.sent[-1]["content"] == "{{mention:wxid_friend}}"
    assert json.loads(screen.sent[-1]["mention_ids"]) == ["wxid_friend"]
    assert not screen.page_errors


def test_paste_rejects_multiple_attachments_and_preserves_existing_one(page):
    screen = MessagePage(page)
    screen.paste([{"name": "一.txt", "type": "text/plain"}, {"name": "二.txt", "type": "text/plain"}], text="剪贴板文字")
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("一次只能添加 1 个附件")
    playwright.expect(page.locator("#wechat-attachment")).to_be_hidden()
    playwright.expect(page.locator("textarea")).to_have_value("剪贴板文字")
    screen.paste([{"name": "保留.txt", "type": "text/plain"}])
    screen.paste([{"name": "不能覆盖.txt", "type": "text/plain"}])
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("已有待发送附件")
    playwright.expect(page.locator("#wechat-attachment")).to_contain_text("保留.txt")
    assert not screen.sent
    assert not screen.paste([], text="普通文字")
    assert not screen.page_errors


@pytest.mark.parametrize("width", [1440, 390])
def test_short_bubbles_fit_text_and_failed_icon_sits_to_the_left(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    screen = MessagePage(page, count=2)
    for message in screen.messages:
        message.content = "你好"
        screen.views[message.id]["text"] = "你好"
    screen.messages[1].status = "failed"
    page.reload()
    for message_id in (1, 2):
        row = page.locator(f'[data-id="{message_id}"]')
        bubble = row.locator(".wechat-message-bubble").bounding_box()
        metadata = row.locator(".wechat-message-meta").bounding_box()
        assert bubble["width"] < 100 and bubble["width"] < metadata["width"]
    failed = page.locator('[data-id="2"]')
    icon = failed.get_by_role("img", name="发送失败")
    playwright.expect(icon).to_have_attribute("title", "发送失败，请检查渠道后重试")
    assert "发送失败" not in failed.locator(".wechat-message-meta").inner_text()
    icon_box = icon.bounding_box()
    bubble = failed.locator(".wechat-message-bubble").bounding_box()
    assert icon_box["x"] + icon_box["width"] < bubble["x"]
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    assert not screen.page_errors


@pytest.mark.parametrize("width,touch", [(1440, False), (390, True), (320, True)])
def test_composer_keeps_editor_above_named_icon_toolbar(browser, width, touch):
    context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=touch)
    try:
        page = context.new_page()
        screen = MessagePage(page)
        tools = page.locator(".wechat-tool")
        assert tools.evaluate_all("els => els.map(el => el.dataset.sendMedia)") == ["emoji", "image", "file", "video", "voice"]
        for label in ("表情", "图片", "文件", "视频", "语音"):
            tool = page.get_by_role("button", name="发送" + label, exact=True)
            playwright.expect(tool).to_be_visible()
            playwright.expect(tool).to_have_attribute("title", "发送" + label)
            assert tool.locator("svg").count() == 1 and not tool.inner_text().strip()
        editor = page.locator("textarea").bounding_box()
        toolbar = page.locator(".wechat-composer-footer").bounding_box()
        send = page.locator(".wechat-send").bounding_box()
        assert editor["height"] >= 88 and editor["y"] + editor["height"] <= toolbar["y"] + 1
        if width >= 390:
            assert abs(send["y"] + send["height"] / 2 - toolbar["y"] - toolbar["height"] / 2) <= 1
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
        assert not screen.page_errors
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}])
def test_menu_layout_keyboard_and_expired_revoke(page, viewport):
    page.set_viewport_size(viewport)
    screen = MessagePage(page)
    page.locator('[data-id="20"]').evaluate("el => el.dataset.revokeUntil = String(Date.now() - 1)")
    bubble = page.locator('[data-id="20"] .wechat-message-bubble')
    bubble.focus()
    bubble.press("Shift+F10")
    menu = page.locator("#wechat-context-menu")
    playwright.expect(menu).to_be_visible()
    bounds = menu.bounding_box()
    assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= viewport["width"]
    assert 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= viewport["height"]
    page.locator('[data-message-action="revoke"]').click()
    playwright.expect(page.locator("#wechat-composer-hint")).to_contain_text("超过 2 分钟")
    assert screen.revoked == []
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    assert not screen.page_errors
