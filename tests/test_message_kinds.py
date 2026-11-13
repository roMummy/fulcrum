"""消息列表区分公众号:判断规则、筛选、列表标识与自动刷新接口。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="message-kinds-logs-"))

import pytest
from sqlalchemy import select

from app.models.entities import Conversation
from app.services.conversation_kinds import KIND_DIRECT, KIND_GROUP, KIND_OFFICIAL, apply_kind_filter, conversation_kind, normalize_kind
from tests.test_provider_node_pages import _login, provider_page_context  # noqa: F401  (含 fixture)


@pytest.mark.parametrize(
    ("chat_type", "external_id", "expected"),
    [
        ("group", "123@chatroom", KIND_GROUP),
        ("direct", "wxid_friend01", KIND_DIRECT),
        ("direct", "gh_1a2b3c4d5e6f", KIND_OFFICIAL),     # 公众号账号 ID 以 gh_ 开头
        ("direct", "GH_ABCDEF", KIND_OFFICIAL),           # 大小写不敏感
        ("direct", "notifymessage", KIND_OFFICIAL),       # 服务通知汇集的是公众号推送
        ("group", "gh_looks_like_official", KIND_GROUP),  # 群聊以 chat_type 为准,不被名字误判
        ("direct", "ghost_user", KIND_DIRECT),            # 只是以 gh 开头不算,必须是 gh_ 前缀
        ("", "", KIND_DIRECT),
    ],
)
def test_conversation_kind_rules(chat_type, external_id, expected) -> None:
    assert conversation_kind(chat_type, external_id) == expected


def test_filter_values_are_whitelisted() -> None:
    assert normalize_kind("OFFICIAL") == "official" and normalize_kind("group") == "group"
    assert normalize_kind("x'; drop table") == "" and normalize_kind(None) == ""


def _add_conversations(ids) -> dict[str, int]:
    rows = {}
    with ids["factory"]() as db:
        channel_id = db.scalar(select(Conversation.channel_id).limit(1)) or 0
        for key, chat_type, external_id, title in [
            ("friend", "direct", "wxid_friend01", "好友小王"),
            ("official", "direct", "gh_1a2b3c4d5e6f", "某某公众号"),
            ("underscore", "direct", "ghXabcdef", "名字像公众号但不是"),   # gh_ 里的下划线不能被当成通配符
            ("room", "group", "888@chatroom", "客户群"),
        ]:
            item = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel_id or None, channel_type="wechat_webridge",
                                chat_type=chat_type, external_id=external_id, title=title)
            db.add(item)
            db.flush()
            rows[key] = int(item.id)
        db.commit()
    return rows


def _names(html: str) -> str:
    return html.split('aria-label="会话列表"', 1)[1].split("</aside>", 1)[0]


def test_the_list_filters_and_marks_official_accounts(provider_page_context) -> None:
    client, ids = provider_page_context
    rows = _add_conversations(ids)
    _login(client, "admin")

    everything = _names(client.get("/messages").text)
    assert all(name in everything for name in ("好友小王", "某某公众号", "客户群"))
    assert everything.count("msg-kind-badge") == 1                       # 只有公众号带标识
    assert "某某公众号" in everything.split("msg-kind-badge")[1][:200]

    official = _names(client.get("/messages?chat_type=official").text)
    assert "某某公众号" in official and "好友小王" not in official and "客户群" not in official
    direct = _names(client.get("/messages?chat_type=direct").text)
    assert "好友小王" in direct and "名字像公众号但不是" in direct and "某某公众号" not in direct   # 私聊里不再混入公众号
    group = _names(client.get("/messages?chat_type=group").text)
    assert "客户群" in group and "某某公众号" not in group and "好友小王" not in group

    page = client.get("/messages?chat_type=official").text
    assert 'class="msg-kind-tab active"' in page and 'href="/messages?chat_type=official"' in page   # 当前类型高亮
    thread = client.get(f"/messages?conversation_id={rows['official']}").text
    assert "公众号" in thread.split("wechat-thread-head", 1)[1][:200]    # 对话头部显示类型


def test_the_refresh_endpoint_reports_each_conversations_kind(provider_page_context) -> None:
    client, ids = provider_page_context
    _add_conversations(ids)
    _login(client, "admin")
    items = {item["display_name"]: item["kind"] for item in client.get("/messages/conversations").json()["conversations"]}
    assert items["某某公众号"] == "official" and items["好友小王"] == "direct" and items["客户群"] == "group"
    only = client.get("/messages/conversations?chat_type=official").json()["conversations"]
    assert [item["kind"] for item in only] == ["official"]
