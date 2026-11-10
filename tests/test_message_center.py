"""消息中心:整页布局(无介绍语、无页码)、向上滚动加载更早消息的接口、发送框的 JSON 提交。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.models.entities import Conversation, Message
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

TOTAL = 75


@pytest.fixture()
def seeded(provider_page_context):  # noqa: F811
    """一个群会话,75 条消息(奇数条入站、偶数条出站,最后一条出站标记为发送失败)。"""

    client, ids = provider_page_context
    with ids["factory"]() as db:
        conversation = Conversation(
            tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], channel_type="wechat_ipad",
            external_id="room@chatroom", chat_type="group", title="测试群",
        )
        db.add(conversation)
        db.flush()
        messages = []
        for number in range(1, TOTAL + 1):
            outbound = number % 2 == 0
            messages.append(
                Message(
                    tenant_id=ids["alpha"], conversation_id=conversation.id, direction="out" if outbound else "in",
                    sender_id="bot" if outbound else "wxid_a", sender_name="" if outbound else "小明",
                    content=f"第{number}条消息", message_type="text", status="failed" if number == TOTAL - 1 else "sent",
                )
            )
        db.add_all(messages)
        db.commit()
        message_ids = [int(item.id) for item in messages]
        conversation_id = int(conversation.id)
    csrf = _login(client, "admin")
    return client, ids, conversation_id, message_ids, csrf


def _rows(payload: dict) -> list[int]:
    return [int(value) for value in re.findall(r'data-id="(\d+)"', payload["html"])]


# ---------- 页面 ----------

def test_page_has_no_intro_copy_or_page_numbers_and_loads_only_the_latest(seeded) -> None:
    client, _ids, conversation_id, message_ids, _csrf = seeded
    page = client.get(f"/messages?conversation_id={conversation_id}")
    assert page.status_code == 200
    # 介绍语和页码都去掉了
    for removed in ("微信式消息中心", "左侧选择联系人或群聊", "list-pagination", "更早消息", "更新消息"):
        assert removed not in page.text
    # 只渲染最新 30 条;还有更早的,由向上滚动继续加载
    shown = re.findall(r'class="wechat-message-row [^"]*" data-id="(\d+)"', page.text)
    assert [int(value) for value in shown] == message_ids[-30:]
    assert 'data-has-more="1"' in page.text and "/static/js/messages.js" in page.text
    # 发送框是不刷新页面的脚本表单(没有 action,不再整页提交)
    assert 'id="wechat-composer"' in page.text and "<textarea" in page.text and 'action="/messages/send"' not in page.text
    # 发送失败的出站消息在对话里有标记
    assert "发送失败" in page.text


def test_short_conversation_has_nothing_more_to_load(seeded) -> None:
    client, ids, _conversation_id, _message_ids, _csrf = seeded
    with ids["factory"]() as db:
        short = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], channel_type="wechat_ipad", external_id="u1", chat_type="direct", title="张三")
        db.add(short)
        db.flush()
        db.add(Message(tenant_id=ids["alpha"], conversation_id=short.id, direction="in", sender_id="u1", content="你好", message_type="text"))
        db.commit()
        short_id = int(short.id)
    assert 'data-has-more="0"' in client.get(f"/messages?conversation_id={short_id}").text


# ---------- 向上滚动加载更早 / 发送后拉新 ----------

def test_history_pages_backwards_until_the_beginning(seeded) -> None:
    client, _ids, conversation_id, message_ids, _csrf = seeded
    # 首屏是最后 30 条(46..75);往上翻第一页:16..45,还有更早的
    first = client.get(f"/messages/history?conversation_id={conversation_id}&before_id={message_ids[45]}").json()
    assert _rows(first) == message_ids[15:45] and first["has_more"] is True and first["count"] == 30
    # 再往上:1..15,到头了
    second = client.get(f"/messages/history?conversation_id={conversation_id}&before_id={message_ids[15]}").json()
    assert _rows(second) == message_ids[:15] and second["has_more"] is False
    # 消息行与首屏渲染一致(同一个模板)
    assert "第16条消息" in first["html"] and "wechat-message-row" in first["html"]


def test_history_after_id_returns_newer_messages_for_the_send_box(seeded) -> None:
    client, _ids, conversation_id, message_ids, _csrf = seeded
    newer = client.get(f"/messages/history?conversation_id={conversation_id}&after_id={message_ids[69]}").json()
    assert _rows(newer) == message_ids[70:] and newer["has_more"] is False
    nothing = client.get(f"/messages/history?conversation_id={conversation_id}&after_id={message_ids[-1]}").json()
    assert nothing == {"html": "", "has_more": False, "count": 0}


def test_history_requires_a_known_conversation_and_login(seeded) -> None:
    client, _ids, conversation_id, _message_ids, _csrf = seeded
    assert client.get("/messages/history?conversation_id=999999").status_code == 404
    _login(client, "readonly")
    assert client.get(f"/messages/history?conversation_id={conversation_id}").status_code == 200  # 只读角色可以看
    client.cookies.clear()
    assert client.get(f"/messages/history?conversation_id={conversation_id}").status_code in {303, 401}


# ---------- 发送 ----------

def test_send_returns_json_and_validates_content(seeded, monkeypatch) -> None:
    import app.main as main_module

    client, _ids, conversation_id, message_ids, csrf = seeded
    sent: list[dict] = []

    def fake_dispatch(db, channel, conversation, payload):
        sent.append(payload)
        row = db.get(Message, message_ids[0])
        return row, {"ok": not payload["content"].startswith("失败"), "error_message": "手机离线"}

    monkeypatch.setattr(main_module, "dispatch_outbound_message", fake_dispatch)
    url = "/messages/send"
    ok = client.post(url, data={"conversation_id": conversation_id, "content": "  你好呀  ", "csrf_token": csrf})
    assert ok.status_code == 200 and ok.json()["ok"] is True and ok.json()["message_id"] == message_ids[0]
    assert sent[-1] == {"message_type": "text", "content": "你好呀"}
    failed = client.post(url, data={"conversation_id": conversation_id, "content": "失败的消息", "csrf_token": csrf}).json()
    assert failed["ok"] is False and "手机离线" in failed["notice"] and failed["message_id"] == message_ids[0]
    # 空内容、超长内容在发出之前就拒绝
    assert client.post(url, data={"conversation_id": conversation_id, "content": "   ", "csrf_token": csrf}).status_code == 400
    too_long = client.post(url, data={"conversation_id": conversation_id, "content": "长" * 2001, "csrf_token": csrf})
    assert too_long.status_code == 400 and "太长" in too_long.json()["notice"]
    assert len(sent) == 2
    # 没有 CSRF 令牌不能发
    assert client.post(url, data={"conversation_id": conversation_id, "content": "你好"}).status_code == 403
