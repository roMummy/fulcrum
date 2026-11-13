"""统一出站队列也要检查全体 @ 权限，不能绕过消息页面的校验。"""
import json

import pytest
from sqlalchemy import func, select

from app.models.entities import Chatroom, ChatroomMember, TaskJob
from app.services.wechat_webridge.service import enqueue_command, enqueue_message_command
from tests.test_webridge_message_echo import OWN, echo_env


def _room(db, channel, conversation, role):
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id,
                    external_id=conversation.external_id, name="测试群")
    db.add(room)
    db.flush()
    db.add(ChatroomMember(tenant_id=1, bot_id=1, channel_id=channel.id, chatroom_id=room.id,
                          member_external_id=OWN, role=role, status="active"))
    db.commit()


@pytest.mark.parametrize("payload", [
    {"content": "@所有人 开会", "mention_user_list": ["notify@all"]},
    {"content": "{{mention:notify@all}}开会"},
    {"content": "{{mention: notify@all }}开会"},
])
def test_member_cannot_enqueue_all_mention(echo_env, payload):
    db, channel, conversation = echo_env
    _room(db, channel, conversation, "member")
    with pytest.raises(ValueError, match="只有群主或管理员"):
        enqueue_message_command(db, channel, conversation, payload)
    assert db.scalar(select(func.count(TaskJob.id))) == 0


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_privileged_account_can_enqueue_all_mention(echo_env, role):
    db, channel, conversation = echo_env
    _room(db, channel, conversation, role)
    job = enqueue_message_command(db, channel, conversation, {"content": "@所有人 开会", "mention_user_list": ["notify@all"]})
    assert json.loads(job.payload_json)["command"]["params"]["mention_user_list"] == ["notify@all"]


def test_another_receiver_cannot_borrow_current_group_privileges(echo_env):
    db, channel, conversation = echo_env
    _room(db, channel, conversation, "admin")
    with pytest.raises(ValueError, match="只有群主或管理员"):
        enqueue_message_command(db, channel, conversation, {"content": "开会", "mention_user_list": ["notify@all"], "receiver_id": "another@chatroom"})
    assert db.scalar(select(func.count(TaskJob.id))) == 0


@pytest.mark.parametrize("role", ["member", "admin"])
def test_raw_command_respects_the_receivers_group_role(echo_env, role):
    db, channel, conversation = echo_env
    _room(db, channel, conversation, role)
    args = {"receiver": conversation.external_id, "content": "{{mention: notify@all }}开会"}
    if role == "admin":
        assert enqueue_command(db, channel, command_name="send_message", args=args).id
    else:
        with pytest.raises(ValueError, match="只有群主或管理员"):
            enqueue_command(db, channel, command_name="send_message", args=args)
        assert db.scalar(select(func.count(TaskJob.id))) == 0
