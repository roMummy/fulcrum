from __future__ import annotations

from datetime import datetime

from app.api.open_resources import _channel_view, _contact_view, _group_view, _member_view
from app.models.entities import Channel, Chatroom, ChatroomMember, WorkWechatContact


def test_open_resource_serializers_return_stable_business_fields() -> None:
    """资源序列化器只返回业务字段，并按 874 渠道计算能力目录。"""

    channel = Channel(
        id=3,
        name="iPad 账号",
        channel_type="wechat_ipad",
        status="online",
        login_state="online",
        last_login_at=datetime(2026, 11, 8, 12, 0),
    )
    account = _channel_view(channel)
    assert account["id"] == 3
    assert account["channel_type"] == "wechat_ipad"
    assert any(item["code"] == "contact.read" for item in account["capabilities"])
    assert "api_base" not in account

    contact = WorkWechatContact(
        id=4,
        channel_id=3,
        contact_external_id="wxid-contact",
        name="联系人",
        alias="contact",
        contact_type="friend",
        sync_status="active",
    )
    assert _contact_view(contact)["external_id"] == "wxid-contact"
    assert "mobile" not in _contact_view(contact)

    group = Chatroom(
        id=5,
        channel_id=3,
        external_id="room@chatroom",
        name="客户群",
        status="active",
        is_active=True,
        member_count=2,
    )
    assert _group_view(group)["external_id"] == "room@chatroom"

    member = ChatroomMember(
        id=6,
        chatroom_id=5,
        channel_id=3,
        bot_id=1,
        member_external_id="wxid-member",
        display_name="群成员",
        status="active",
    )
    assert _member_view(member)["external_id"] == "wxid-member"
