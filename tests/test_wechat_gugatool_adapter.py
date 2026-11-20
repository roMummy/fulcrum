"""微信 iOS（GugaTool）适配器的离线映射测试。"""

from __future__ import annotations

import json

import pytest

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation
from app.services import message_view
from app.services.account_connections.registry import get_account_connector
from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter
from app.services.chatroom_invitations import parse_system_invitation
from app.services.group_features.member_change import parse_member_change


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


def test_link_caption_survives_plugin_field_mapping(monkeypatch):
    adapter = WechatGugatoolAdapter()
    monkeypatch.setattr(adapter, "_queued_result", lambda *args: args[-1])
    for field in ("desc", "link_description"):
        params = adapter._send_link(_channel(), _conversation(), {
            "url": "https://example.org/work", "content": "author", field: "caption",
        }, "peer")
        assert params["description"] == "caption"


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


def test_member_nickname_change_builds_structured_member_event() -> None:
    """手机端的改群昵称事件要转成群功能可消费的 member_change。"""

    item = {
        "id": "rename-1",
        "kind": "incoming",
        "eventName": "group_member_nickname_change",
        "messageSubType": "group_member_nickname_change",
        "fromUser": "54383334897@chatroom",
        "realChatUser": "wxid_member",
        "memberID": "wxid_member",
        "memberDisplayName": "1",
        "memberNickName": "只为遇见你",
        "roomNickName": "1",
        "oldRoomNickName": "只为遇见你",
        "newRoomNickName": "1",
        "snapshotBeforeCount": 2,
        "snapshotAfterCount": 2,
        "content": "只为遇见你 -> 1",
        "messageType": 10000,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["message"]["message_type"] == "group_member_nickname_change"
    assert normalized["details"]["member_change"] == {
        "change_type": "rename",
        "members": [{"external_id": "wxid_member", "display_name": "1"}],
        "after_count": 2,
        "new_room_nick_name": "1",
        "old_room_nick_name": "只为遇见你",
        "summary": "只为遇见你 将群昵称改为 1",
    }
    parsed = parse_member_change(normalized)
    assert parsed["change_type"] == "rename"
    assert parsed["new_name"] == "1"
    assert parsed["old_name"] == "只为遇见你"


def test_member_leave_builds_structured_member_event() -> None:
    """退群事件同样走结构化 member_change,不再依赖系统消息 XML。"""

    item = {
        "id": "leave-1",
        "kind": "incoming",
        "eventName": "group_member_leave",
        "messageSubType": "group_member_leave",
        "fromUser": "27602422280@chatroom",
        "realChatUser": "wxid_leaver",
        "memberID": "wxid_leaver",
        "memberDisplayName": "区区糕",
        "memberNickName": "tan 90",
        "roomNickName": "区区糕",
        "oldRoomNickName": "区区糕",
        "snapshotBeforeCount": 39,
        "snapshotAfterCount": 38,
        "content": "区区糕",
        "messageType": 10000,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    assert normalized["message"]["message_type"] == "group_member_leave"
    assert normalized["details"]["member_change"]["change_type"] == "left"
    assert normalized["details"]["member_change"]["after_count"] == 38
    parsed = parse_member_change(normalized)
    assert parsed["change_type"] == "left"
    assert parsed["members"] == [{"external_id": "wxid_leaver", "display_name": "区区糕"}]


def test_member_join_system_xml_builds_member_event() -> None:
    """入群通知在咕嘎里是 10002 系统消息 XML,要从 details.content_xml 解析出来。"""

    content = """54383334897@chatroom:
<sysmsg type="sysmsgtemplate">
	<sysmsgtemplate>
		<content_template type="tmpl_type_profile">
			<template><![CDATA["$username$"邀请"$names$"加入了群聊]]></template>
			<link_list>
				<link name="username" type="link_profile">
					<memberlist><member><username><![CDATA[wxid_inviter]]></username><nickname><![CDATA[主人]]></nickname></member></memberlist>
				</link>
				<link name="names" type="link_profile">
					<memberlist><member><username><![CDATA[wxid_joiner]]></username><nickname><![CDATA[Scorpio9_L]]></nickname></member></memberlist>
				</link>
			</link_list>
		</content_template>
	</sysmsgtemplate>
</sysmsg>
"""
    item = {
        "id": "join-1",
        "kind": "incoming",
        "fromUser": "54383334897@chatroom",
        "toUser": "wxid_self",
        "realChatUser": "",
        "memberID": "",
        "roomID": "",
        "content": content,
        "messageType": 10002,
    }

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), item)

    # 群系统消息不能把群 id 当成发送者写进群成员表。
    assert normalized["message"]["sender_id"] is None
    # 入群 XML 保持 system 类型,由共享邀请解析器消费归一化后的 XML。
    assert normalized["message"]["message_type"] == "system"
    assert normalized["details"]["content_xml"].startswith("<sysmsg")
    assert normalized["details"]["content_xml"].endswith("</sysmsg>")
    assert normalized["details"]["member_change"] == {
        "change_type": "join",
        "members": [{"external_id": "wxid_joiner", "display_name": "Scorpio9_L"}],
        "summary": "Scorpio9_L 加入了群聊",
    }
    invitation = parse_system_invitation(normalized["details"]["content_xml"])
    assert invitation["inviter_external_id"] == "wxid_inviter"
    assert invitation["member_ids"] == ["wxid_joiner"]
    parsed = parse_member_change(normalized)
    assert parsed["change_type"] == "join"
    assert parsed["members"] == [{"external_id": "wxid_joiner", "display_name": "Scorpio9_L"}]


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


def _appmsg_item(xml: str, *, local_id: int = 0, server_id: int = 0) -> dict:
    return {
        "id": "event-appmsg",
        "fromUser": "49273817233@chatroom",
        "toUser": "wxid_self",
        "realChatUser": "wxid_member",
        "messageType": 49,
        "content": xml,
        "localMessageID": local_id,
        "serverMessageID": server_id,
    }


def test_appmsg_link_exposes_structured_details() -> None:
    """链接卡片(5):正文带中文标签,URL/来源/缩略图放进 details.appmsg。"""

    xml = (
        'wxid_member:\n<?xml version="1.0"?>\n<msg><appmsg appid="wx123" sdkver="0">'
        "<title>拆解小米澎程N90底盘结构和用料</title><des>虎扑汽车区热议</des><type>5</type>"
        "<url>https://m.hupu.com/bbs-share/644031742.html?share=share&amp;euid=1</url>"
        "<thumburl>https://example.com/thumb.jpg</thumburl>"
        "<sourcedisplayname>虎扑App</sourcedisplayname>"
        "</appmsg><appinfo><appname>虎扑App</appname></appinfo></msg>"
    )

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), _appmsg_item(xml))

    assert normalized["message"]["message_type"] == "link"
    # 正文改成紧凑 XML,消息中心用现有解析器直接渲染链接卡片。
    assert normalized["message"]["content"].startswith("<msg><appmsg>")
    card = message_view.describe(normalized["message"]["content"], "link")
    assert (card["kind"], card["title"], card["url"], card["source"]) == (
        "link",
        "拆解小米澎程N90底盘结构和用料",
        "https://m.hupu.com/bbs-share/644031742.html?share=share&euid=1",
        "虎扑App",
    )
    assert card["desc"] == "虎扑汽车区热议"
    appmsg = normalized["details"]["appmsg"]
    assert appmsg["type"] == 5
    assert appmsg["kind"] == "link"
    assert appmsg["title"] == "拆解小米澎程N90底盘结构和用料"
    assert appmsg["description"] == "虎扑汽车区热议"
    assert appmsg["url"] == "https://m.hupu.com/bbs-share/644031742.html?share=share&euid=1"
    assert appmsg["thumb_url"] == "https://example.com/thumb.jpg"
    assert appmsg["source"] == "虎扑App"
    assert appmsg["app_id"] == "wx123"
    assert appmsg["app_name"] == "虎扑App"
    assert "media" not in normalized["details"]


def test_appmsg_transfer_uses_payment_amount_in_content() -> None:
    """转账(2000):正文带金额,支付单号等字段结构化保留。"""

    xml = (
        "<msg><appmsg><title><![CDATA[微信转账]]></title><type>2000</type>"
        "<wcpayinfo><paysubtype>3</paysubtype><feedesc><![CDATA[￥8.00]]></feedesc>"
        "<transcationid>tx-1</transcationid><transferid>tr-1</transferid>"
        "<payer_username>wxid_payer</payer_username><receiver_username>wxid_payee</receiver_username>"
        "<begintransfertime>1795000000</begintransfertime>"
        "<pay_memo>晚饭</pay_memo></wcpayinfo></appmsg></msg>"
    )

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), _appmsg_item(xml))

    card = message_view.describe(normalized["message"]["content"], "transfer")
    assert normalized["message"]["message_type"] == "transfer"
    assert (card["kind"], card["title"], card["desc"]) == ("notice", "微信转账", "￥8.00")
    assert normalized["details"]["appmsg"]["kind"] == "transfer"
    assert normalized["details"]["appmsg"]["payment"] == {
        "subtype": "3",
        "amount": "￥8.00",
        "memo": "晚饭",
        "transaction_id": "tx-1",
        "transfer_id": "tr-1",
        "payer_id": "wxid_payer",
        "receiver_id": "wxid_payee",
        "begin_time": "1795000000",
    }


def test_appmsg_video_channel_extracts_finder_feed() -> None:
    """视频号(51):标题只显示"当前微信版本不支持",必须用 finderFeed 的文案做正文。"""

    xml = (
        "<msg><appmsg><title>当前微信版本不支持展示该内容，请升级至最新版本。</title><type>51</type>"
        "<finderFeed><objectId><![CDATA[15057483877242116331]]></objectId>"
        "<feedType><![CDATA[4]]></feedType><nickname><![CDATA[和美永宁村]]></nickname>"
        "<avatar><![CDATA[https://example.com/avatar.jpg]]></avatar>"
        "<desc><![CDATA[两层方正农房，屋前屋后视野开阔]]></desc>"
        "<mediaCount><![CDATA[1]]></mediaCount></finderFeed>"
        "</appmsg></msg>"
    )

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), _appmsg_item(xml))

    card = message_view.describe(normalized["message"]["content"], "video_channel")
    assert normalized["message"]["message_type"] == "video_channel"
    assert (card["kind"], card["title"], card["desc"], card["source"]) == (
        "link", "和美永宁村", "两层方正农房，屋前屋后视野开阔", "视频号",
    )
    feed = normalized["details"]["appmsg"]["video_channel"]
    assert feed["nickname"] == "和美永宁村"
    assert feed["object_id"] == "15057483877242116331"
    assert feed["description"] == "两层方正农房，屋前屋后视野开阔"
    assert feed["avatar_url"] == "https://example.com/avatar.jpg"
    assert feed["feed_type"] == "4"
    assert feed["media_count"] == 1


def test_appmsg_chat_record_summarizes_item_count() -> None:
    """聊天记录(19):只保留标题、摘要和条数,不把几十 KB 的 recorditem 重复入库。"""

    record = (
        "<recordinfo><title>呱皮的聊天记录</title><desc>呱皮: 女人都在聊些什么</desc>"
        '<datalist count="21"><dataitem datatype="1"/></datalist></recordinfo>'
    )
    xml = (
        "<msg><appmsg><title>呱皮的聊天记录</title><type>19</type>"
        f"<recorditem><![CDATA[{record}]]></recorditem></appmsg></msg>"
    )

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), _appmsg_item(xml))

    card = message_view.describe(normalized["message"]["content"], "merged_records")
    assert normalized["message"]["message_type"] == "merged_records"
    assert (card["kind"], card["title"], card["desc"]) == (
        "merged", "呱皮的聊天记录", "呱皮: 女人都在聊些什么",
    )
    assert normalized["details"]["appmsg"]["chat_record"] == {
        "title": "呱皮的聊天记录",
        "description": "呱皮: 女人都在聊些什么",
        "item_count": 21,
    }


def test_appmsg_file_declares_media_for_auto_download() -> None:
    """文件(6):按现有 file 消息契约入库,消息中心显示文件卡片并按需下载。"""

    xml = (
        '<msg><appmsg><title>季度报告.pdf</title><type>6</type>'
        "<appattach><totallen>10240</totallen><fileext>pdf</fileext><attachid>attach-1</attachid></appattach>"
        "</appmsg></msg>"
    )

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(
        _channel(),
        _appmsg_item(xml, local_id=11, server_id=22),
    )

    assert normalized["message"]["message_type"] == "file"
    card = message_view.describe(normalized["message"]["content"], "file")
    assert (card["kind"], card["media_type"], card["title"], card["meta"]) == (
        "media", "file", "季度报告.pdf", "PDF · 10.0 KB",
    )
    assert normalized["details"]["appmsg"]["kind"] == "file"
    assert normalized["details"]["appmsg"]["attachment"] == {
        "file_ext": "pdf",
        "attach_id": "attach-1",
        "file_size": 10240,
    }
    assert normalized["details"]["media"] == {
        "to_wxid": "49273817233@chatroom",
        "local_message_id": 11,
        "server_message_id": 22,
        "message_type": 49,
        "attachment_type": "file",
        "file_name": "季度报告.pdf",
        "file_ext": "pdf",
        "file_size": 10240,
    }


def test_appmsg_type_74_reuses_file_attachment_contract() -> None:
    """类型 74 也是文件(appmsg),与类型 6 共用 file 消息契约。"""

    xml = (
        '<msg><appmsg><title><![CDATA[7701166629241588521.mp3]]></title><type>74</type>'
        "<appattach><totallen>1038166</totallen><fileext><![CDATA[mp3]]></fileext></appattach>"
        "</appmsg></msg>"
    )

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(
        _channel(),
        _appmsg_item(xml, local_id=31, server_id=32),
    )

    assert normalized["message"]["message_type"] == "file"
    card = message_view.describe(normalized["message"]["content"], "file")
    assert (card["kind"], card["media_type"], card["title"]) == ("media", "file", "7701166629241588521.mp3")
    assert normalized["details"]["appmsg"]["kind"] == "file"
    assert normalized["details"]["media"]["attachment_type"] == "file"
    assert normalized["details"]["media"]["file_name"] == "7701166629241588521.mp3"


def test_file_download_reports_phone_unsupported_clearly(monkeypatch) -> None:
    """手机端没有 appmsg 文件下载选择器时,把协议错误翻译成可读提示。"""

    def _fail(*args, **kwargs):
        raise RuntimeError("download_selector_unavailable_or_message_type_unsupported")

    monkeypatch.setattr("app.services.guga_agent.bridge.call_command", _fail)

    with pytest.raises(RuntimeError) as excinfo:
        WechatGugatoolAdapter().download_message_media(_channel(), to_wxid="1@chatroom", local_id=1583)

    assert "手机端暂不支持下载这类文件" in str(excinfo.value)


def test_appmsg_pat_and_emoji_get_readable_labels() -> None:
    pat_xml = (
        "<msg><appmsg><title>当前微信版本不支持展示该内容，请升级至最新版本。</title><type>62</type>"
        "<patMsg><records><record><pattedUser>wxid_b</pattedUser>"
        '<templete>"${wxid_a}" 拍了拍 "${wxid_b}"</templete>'
        "<fromUser>wxid_a</fromUser></record><recordNum>1</recordNum></records>"
        "<chatUser>27602422280@chatroom</chatUser></patMsg></appmsg></msg>"
    )
    emoji_xml = (
        "<msg><appmsg><title /><type>8</type>"
        "<appattach><totallen>1297594</totallen><attachid>0:0:07f6</attachid>"
        "<emoticonmd5>07f63efebfe95a5c3516170314f1a36c</emoticonmd5><fileext>jpg</fileext></appattach>"
        "</appmsg></msg>"
    )
    adapter = WechatGugatoolAdapter()

    pat = adapter.normalize_inbound_payload(_channel(), _appmsg_item(pat_xml))
    emoji = adapter.normalize_inbound_payload(_channel(), _appmsg_item(emoji_xml))

    assert pat["message"]["message_type"] == "pat"
    assert pat["message"]["content"] == "[拍一拍]"
    assert pat["details"]["pat"] == {
        "from_id": "wxid_a",
        "patted_id": "wxid_b",
        "patted_is_me": False,
    }
    assert pat["details"]["appmsg"]["pat"] == {
        "template": '"${wxid_a}" 拍了拍 "${wxid_b}"',
        "from_user": "wxid_a",
        "patted_user": "wxid_b",
        "chat_user": "27602422280@chatroom",
    }
    assert emoji["message"]["message_type"] == "emoji"
    assert emoji["message"]["content"] == "[表情]"
    assert message_view.describe(emoji["message"]["content"], "emoji")["kind"] == "media"
    assert emoji["details"]["appmsg"]["attachment"]["emoticon_md5"] == "07f63efebfe95a5c3516170314f1a36c"
    # 表情按图片缓存处理,不能误触发文件自动下载。
    assert "media" not in emoji["details"]


def test_appmsg_mini_program_and_group_announcement() -> None:
    mini_xml = (
        "<msg><appmsg><title>点餐小程序</title><type>33</type>"
        "<weappinfo><appid>wx-mini</appid><pagepath>pages/menu</pagepath>"
        "<username>gh_mini</username><appservicetype>0</appservicetype></weappinfo></appmsg></msg>"
    )
    announcement_xml = (
        "<msg><appmsg><title>群公告</title><type>87</type>"
        "<textannouncement>明天上午停水</textannouncement></appmsg></msg>"
    )
    adapter = WechatGugatoolAdapter()

    mini = adapter.normalize_inbound_payload(_channel(), _appmsg_item(mini_xml))
    announcement = adapter.normalize_inbound_payload(_channel(), _appmsg_item(announcement_xml))

    mini_card = message_view.describe(mini["message"]["content"], "mini_program")
    assert mini["message"]["message_type"] == "mini_program"
    assert (mini_card["kind"], mini_card["title"]) == ("mini_program", "点餐小程序")
    assert mini["details"]["appmsg"]["mini_program"] == {
        "app_id": "wx-mini",
        "page_path": "pages/menu",
        "username": "gh_mini",
        "service_type": "0",
    }
    announcement_card = message_view.describe(announcement["message"]["content"], "group_announcement")
    assert announcement["message"]["message_type"] == "group_announcement"
    assert (announcement_card["kind"], announcement_card["title"], announcement_card["desc"], announcement_card["source"]) == (
        "link", "群公告", "明天上午停水", "群公告",
    )
    assert announcement["details"]["appmsg"]["announcement"] == "明天上午停水"


def test_unknown_appmsg_subtype_keeps_title_fallback() -> None:
    """没有映射的 49 子类型仍保留原标题,不能因为细分改造丢掉正文。"""

    xml = "<msg><appmsg><title>客服卡片</title><type>12345</type></appmsg></msg>"

    normalized = WechatGugatoolAdapter().normalize_inbound_payload(_channel(), _appmsg_item(xml))

    assert normalized["message"]["content"] == "客服卡片"
    assert normalized["details"]["appmsg"]["kind"] == "app"
    assert "media" not in normalized["details"]


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
    assert normalized["details"]["appmsg"]["kind"] == "quote"
    assert normalized["details"]["appmsg"]["quote"] == {
        "ref_type": 1,
        "server_id": "2773629253319918141",
        "display_name": "小明",
        "content": "原始问题\n保留换行",
    }


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
