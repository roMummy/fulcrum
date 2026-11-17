"""微信 iOS（GugaTool）适配器的离线映射测试。"""

from __future__ import annotations

import json

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation
from app.services.account_connections.registry import get_account_connector
from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter


def _channel() -> Channel:
    # 单测跑离线映射，显式关掉昵称补全，避免访问真实手机地址。
    return Channel(
        bot_id=1,
        name="iOS 测试",
        channel_type="wechat_gugatool",
        api_base="http://127.0.0.1:19090",
        adapter_config_json='{"resolve_names": false}',
    )


def _conversation(external_id: str = "wxid_peer") -> Conversation:
    return Conversation(bot_id=1, channel_id=1, external_id=external_id, chat_type="direct")


def test_group_message_uses_room_as_conversation_and_member_as_sender() -> None:
    """群消息的 fromUser 是群 id，真正发送者是 realChatUser。"""

    item = {
        "id": "event-1",
        "kind": "incoming",
        "fromUser": "123456@chatroom",
        "toUser": "wxid_self",
        "realChatUser": "wxid_member",
        "content": "组里好",
        "messageType": 1,
        "createTime": 1794362355,
        "sequence": 10,
        "localMessageID": 100,
        "serverMessageID": 200,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["conversation"] == {
        "external_id": "123456@chatroom",
        "title": "123456@chatroom",
        "chat_type": "group",
    }
    assert normalized["message"]["sender_id"] == "wxid_member"
    assert normalized["message"]["message_type"] == "text"
    assert normalized["message"]["content"] == "组里好"
    assert normalized["event_key"] == "event-1"
    assert normalized["details"]["self_wxid"] == "wxid_self"


def test_image_message_uses_placeholder_and_keeps_raw_xml() -> None:
    """媒体正文是整段 XML，会话列表只展示占位，原文留在 details。"""

    item = {
        "id": "event-2",
        "kind": "incoming",
        "fromUser": "wxid_peer",
        "toUser": "wxid_self",
        "realChatUser": "",
        "content": "<msg><img aeskey=\"k\" /></msg>",
        "messageType": 3,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["message"]["message_type"] == "image"
    assert normalized["message"]["content"] == "[图片]"
    assert normalized["details"]["content_xml"].startswith("<msg>")
    assert normalized["conversation"]["chat_type"] == "direct"
    assert normalized["should_persist_message"] is True


def test_revoke_kind_reports_system_message() -> None:
    """撤回事件不当作普通消息，正文给可读结论。"""

    item = {"id": "event-3", "kind": "revoke", "fromUser": "wxid_peer", "toUser": "wxid_self", "messageType": 10002}

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["event_type"] == "revoke"
    assert normalized["message"]["message_type"] == "system"
    assert normalized["message"]["content"] == "对方撤回了一条消息"


def test_missing_event_id_falls_back_to_message_ids() -> None:
    """没有 UUID 时用本地/服务端消息 ID 做去重键，避免同一消息重复入库。"""

    item = {"kind": "incoming", "fromUser": "wxid_peer", "toUser": "wxid_self", "localMessageID": 7, "serverMessageID": 9}

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["event_key"] == "ios:7:9"


def test_send_without_session_fails_with_actionable_error() -> None:
    """渠道不在数据库会话中时无法入队,直接给出可读错误。"""

    channel = Channel(bot_id=1, name="iOS", channel_type="wechat_gugatool")
    result = WechatGugatoolAdapter().send_message(
        channel,
        _conversation(),
        {"message_type": "text", "content": "hi"},
    )

    assert result["ok"] is False
    assert result["error_code"] == "send_failed"
    assert "无法入队" in result["error_message"]


def test_empty_text_is_rejected_before_request() -> None:
    result = WechatGugatoolAdapter().send_message(_channel(), _conversation(), {"message_type": "text", "content": "  "})

    assert result["ok"] is False
    assert result["error_code"] == "empty_content"


def test_voice_message_requires_audio_content() -> None:
    """语音发送需要真实音频字节；缺失时给出可读错误而不是静默发空包。"""

    result = WechatGugatoolAdapter().send_message(_channel(), _conversation(), {"message_type": "voice"})

    assert result["ok"] is False
    assert result["error_code"] == "send_failed"
    assert "媒体内容" in result["error_message"]


def test_group_mention_from_message_source_sets_flag() -> None:
    """@ 判断只认 atuserlist，并支持 @所有人。"""

    adapter = WechatGugatoolAdapter()
    channel = _channel()

    mentioned = adapter.normalize_inbound_payload(
        channel,
        {"id": "e1", "fromUser": "1@chatroom", "toUser": "wxid_self", "realChatUser": "wxid_a",
         "messageType": 1, "content": "hi", "messageSource": "<msgsource><atuserlist>wxid_self</atuserlist></msgsource>"},
    )
    at_all = adapter.normalize_inbound_payload(
        channel,
        {"id": "e2", "fromUser": "1@chatroom", "toUser": "wxid_self", "realChatUser": "wxid_a",
         "messageType": 1, "content": "hi", "messageSource": "<atuserlist>notify@all</atuserlist>"},
    )
    untouched = adapter.normalize_inbound_payload(
        channel,
        {"id": "e3", "fromUser": "1@chatroom", "toUser": "wxid_self", "realChatUser": "wxid_a",
         "messageType": 1, "content": "hi", "messageSource": "<atuserlist>wxid_other</atuserlist>"},
    )

    assert mentioned["details"]["mentioned_self"] is True
    assert at_all["details"]["mentioned_self"] is True
    assert untouched["details"]["mentioned_self"] is False


def test_media_messages_expose_ids_for_background_download() -> None:
    """入站媒体任务依赖 details.media 里的会话与消息 ID 定位手机缓存。"""

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(
        _channel(),
        {"id": "e4", "fromUser": "2@chatroom", "toUser": "wxid_self", "realChatUser": "wxid_b",
         "messageType": 43, "content": "<msg/>", "localMessageID": 11, "serverMessageID": 22},
    )

    assert normalized["message"]["message_type"] == "video"
    assert normalized["details"]["media"] == {
        "to_wxid": "2@chatroom",
        "local_message_id": 11,
        "server_message_id": 22,
        "message_type": 43,
    }


def test_app_message_title_survives_nested_escaping() -> None:
    """app 消息 XML 可能被转义两层，标题仍要取出来当正文。"""

    xml = "&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;抖音解析成功&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;"

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(
        _channel(),
        {"id": "e5", "fromUser": "3@chatroom", "toUser": "wxid_self", "realChatUser": "wxid_c",
         "messageType": 49, "content": xml},
    )

    assert normalized["message"]["message_type"] == "app"
    assert normalized["message"]["content"] == "抖音解析成功"


def test_account_connector_is_registered() -> None:
    assert get_account_connector("wechat_gugatool").channel_type == "wechat_gugatool"


def test_health_check_reads_registration_heartbeat() -> None:
    channel = _channel()
    channel.runtime_info_json = json.dumps(
        {
            "guga_agent": {
                "device_id": "device-1",
                "account_id": "wxid_self",
                "last_seen_at": now_shanghai().isoformat(),
                "online": True,
            }
        },
        ensure_ascii=False,
    )

    health = WechatGugatoolAdapter().health_check(channel)

    assert health["ok"] is True
    assert health["status"] == "online"


def test_fetch_chatroom_info_uses_agent_command(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def _fake_call(channel, command_name, params, **kwargs):
        calls.update({"command": command_name, "params": params})
        return {"Data": {"Name": "测试群", "Members": []}}

    monkeypatch.setattr("app.services.guga_agent.bridge.call_command", _fake_call)

    data = WechatGugatoolAdapter().fetch_chatroom_info(_channel(), "1@chatroom")

    assert data["Name"] == "测试群"
    assert calls == {"command": "get_chatroom_info", "params": {"chatroom_wxid": "1@chatroom"}}


def _queued_send(monkeypatch, payload: dict) -> dict:
    """替换入队动作，只校验适配器发给手机的命令名与参数。"""

    adapter = WechatGugatoolAdapter()
    captured: dict = {}

    def _fake_enqueue(channel, conversation, command_name, params, *, priority=90):
        captured.update(command_name=command_name, params=params)
        return "guga_test", 1

    monkeypatch.setattr(adapter, "_enqueue", _fake_enqueue)
    result = adapter.send_message(_channel(), _conversation(), payload)
    assert result["ok"] is True
    return captured


def test_emoji_message_queues_send_emoji_command(monkeypatch) -> None:
    """表情走手机专门的 SendEmoji 二进制接口，不能当成普通图片发送。"""

    captured = _queued_send(monkeypatch, {"message_type": "emoji", "base64": "R0lGODlhAQABAAAAACw="})

    assert captured["command_name"] == "send_emoji"
    assert captured["params"]["content_type"] == "image/gif"
    assert captured["params"]["file_name"].endswith(".gif")
    assert captured["params"]["data_base64"] == "R0lGODlhAQABAAAAACw="


def test_quote_text_uses_send_quote_with_phone_message_ids(monkeypatch) -> None:
    """引用回复需要手机本地/服务端消息 ID，不能把事件 UUID 当作引用编号。"""

    captured = _queued_send(
        monkeypatch,
        {"message_type": "text", "content": "收到", "quote_local_id": 1097, "quote_msg_svr_id": 2773629253319918141},
    )

    assert captured["command_name"] == "send_quote"
    assert captured["params"] == {
        "to_wxid": "wxid_peer",
        "content": "收到",
        "local_id": 1097,
        "msg_svr_id": 2773629253319918141,
    }


def test_mention_list_becomes_comma_separated_at_targets(monkeypatch) -> None:
    captured = _queued_send(
        monkeypatch,
        {"message_type": "text", "content": "@张三 你好", "mention_user_list": ["wxid_a", "wxid_b"]},
    )

    assert captured["command_name"] == "send_text"
    assert captured["params"]["at"] == "wxid_a,wxid_b"


def test_quote_message_becomes_text_with_quote_payload() -> None:
    """手机上引用一条消息再发言:正文按文本入库,被引用的人和原文放进 quote_payload。"""

    quote_xml = (
        '<msg><appmsg><title>这个怎么处理</title><type>57</type>'
        '<refermsg><type>1</type><displayname>小明</displayname>'
        '<content>原始问题\n保留换行</content><svrid>2773629253319918141</svrid></refermsg>'
        '</appmsg></msg>'
    )
    item = {
        "id": "event-quote",
        "kind": "incoming",
        "fromUser": "123@chatroom",
        "toUser": "wxid_self",
        "realChatUser": "wxid_member",
        "content": quote_xml,
        "messageType": 49,
        "localMessageID": 1097,
        "serverMessageID": 2773629253319918141,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["message"]["message_type"] == "text"
    assert normalized["message"]["content"] == "这个怎么处理"
    assert normalized["details"]["quote_payload"] == {
        "title": "这个怎么处理",
        "ref_type": 1,
        "ref_content": "原始问题\n保留换行",
        "ref_name": "小明",
    }
    # 原始 XML 仍保留给媒体/排查使用,引用本身不再当成 app 卡片。
    assert normalized["details"]["content_xml"].startswith("<msg>")


def test_quote_message_survives_one_extra_escape() -> None:
    """个别手机版本会把整段 XML 再转义一层,引用仍要能识别。"""

    quote_xml = (
        "&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;收到&lt;/title&gt;&lt;type&gt;57&lt;/type&gt;"
        "&lt;refermsg&gt;&lt;type&gt;1&lt;/type&gt;&lt;content&gt;原文&lt;/content&gt;&lt;/refermsg&gt;"
        "&lt;/appmsg&gt;&lt;/msg&gt;"
    )
    item = {"id": "event-escaped", "fromUser": "wxid_peer", "content": quote_xml, "messageType": 49}

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["message"]["message_type"] == "text"
    assert normalized["message"]["content"] == "收到"
    assert normalized["details"]["quote_payload"]["ref_content"] == "原文"


def test_capabilities_follow_commands_reported_by_phone() -> None:
    """手机没上报的发送能力不能对页面宣称可用，老版本没有清单时按适配器实现放行。"""

    from app.services import message_actions

    channel = _channel()
    channel.runtime_info_json = json.dumps(
        {"guga_agent": {"capabilities": ["send_text", "send_image", "send_voice", "send_video", "send_emoji", "send_file", "send_quote", "revoke_message"]}}
    )
    conversation = _conversation("1@chatroom")
    conversation.chat_type = "group"

    supported = message_actions.capabilities(channel, conversation)
    assert all(supported[name] for name in ("image", "voice", "video", "emoji", "file", "quote", "mention", "revoke"))

    channel.runtime_info_json = json.dumps({"guga_agent": {"capabilities": ["send_text"]}})
    limited = message_actions.capabilities(channel, conversation)
    assert limited["mention"] is True
    assert limited["image"] is False and limited["emoji"] is False and limited["quote"] is False and limited["revoke"] is False

    channel.runtime_info_json = "{}"
    legacy = message_actions.capabilities(channel, conversation)
    assert all(legacy[name] for name in ("image", "voice", "video", "emoji", "file", "quote", "mention", "revoke"))


def test_revokemsg_system_message_keeps_recall_details() -> None:
    """微信撤回是 10002 系统消息(kind 仍是 incoming),要按撤回事件归一,不能显示成对方发言。"""

    content = (
        '<sysmsg type="revokemsgcontent"><revokemsgcontent><link><scene>revokemsgcontent</scene>'
        '<reEditStringContent>123</reEditStringContent><stringContent>你撤回了一条消息</stringContent></link>'
        '<text>你撤回了一条消息</text></revokemsgcontent></sysmsg>'
    )
    item = {
        "id": "event-recall",
        "kind": "incoming",
        "fromUser": "wxid_peer",
        "toUser": "wxid_self",
        "content": content,
        "messageType": 10002,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["event_type"] == "revoke"
    assert normalized["message"]["message_type"] == "system"
    assert normalized["message"]["content"] == "你撤回了一条消息"
    assert normalized["details"]["recall"]["message_ids"] == []
    assert "original_text" not in normalized["details"]["recall"]
    assert normalized["details"]["skip_plugin_dispatch"] is True
