"""本账号自己发出的消息不进 AI:机器人回复回流后不能被当成用户提问,否则私聊里 AI 会自问自答。"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ai-self-logs-"))

from types import SimpleNamespace

from sqlalchemy import update

from app.models.entities import TenantAiSetting
from app.services import plugin_directory as pd
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from test_ai_chat import _session
from test_ai_dispatch import _seed

OWN = "wxid_own_account"


def _channel(account_id: str | None):
    runtime = {"webridge": {"account_id": account_id}} if account_id else {}
    return SimpleNamespace(id=1, runtime_info_json=json.dumps(runtime))


def _normalize(channel, sender: str) -> dict:
    payload = {"event_name": "message", "body": {"msg_type": "text", "chat_id": "wxid_friend", "sender_id": sender, "content": "在吗", "message_id": "m1"}}
    return WechatWeBridgeAdapter().normalize_inbound_payload(channel, payload)


def test_the_adapter_marks_messages_sent_by_the_logged_in_account() -> None:
    assert _normalize(_channel(OWN), OWN)["details"]["from_self"] is True
    assert "from_self" not in _normalize(_channel(OWN), "wxid_friend")["details"]  # 对方发的不标记
    assert "from_self" not in _normalize(_channel(None), OWN)["details"]  # 还没登记账号时不猜
    assert "from_self" not in _normalize(_channel(OWN), "")["details"]


def test_private_chat_ai_ignores_self_sent_messages_but_still_answers_the_friend() -> None:
    db = _session()
    channel = _seed(db)
    db.execute(update(TenantAiSetting).values(chat_enabled=True))
    db.commit()

    def direct(sender: str, details: dict) -> dict:
        return {
            "conversation": {"external_id": "wxid_friend", "chat_type": "direct"},
            "message": {"content": "在吗", "sender_id": sender, "message_type": "text"},
            "details": details,
        }

    friend = direct("wxid_friend", {})
    plan = pd.build_static_dispatch_plan(db, channel, friend)
    assert pd._enqueue_ai_intent(db, plan, friend, raw_event_id=1, conversation_id=3, message_id=1) is True  # 朋友发来的照常回答
    own = direct(OWN, {"from_self": True})
    assert pd._enqueue_ai_intent(db, plan, own, raw_event_id=2, conversation_id=3, message_id=2) is False  # 自己发的不进 AI
