"""公众号资料按明确账号 ID 归属，验证真实入站、目录同步及历史消息身份补齐。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.entities import Conversation, Message, TaskJob, WorkWechatContact
from app.services.channel_events import ingest_inbound_event
from app.services.identity_directory import IdentityDirectory
from app.services.wechat_webridge import service
from app.services.wechat_webridge.official_accounts import publisher_name
from tests.test_webridge_member_directory import env  # noqa: F401

OFFICIAL = 'gh_a1b2c3d4'


def article(*, publisher=OFFICIAL, name='天然工坊'):
    """按真实回报将发布者放在 mmreader 内；文章和视频号资料不属于公众号身份。"""
    return (
        '<msg><appmsg><type>5</type><title>文章标题</title><mmreader><category><item>'
        '<thumburl>https://example.test/article.jpg</thumburl>'
        '<finder_feed><nickname>视频作者</nickname>'
        '<avatar>https://example.test/finder.jpg</avatar></finder_feed>'
        '</item></category><publisher>'
        f'<username>{publisher}</username><nickname>{name}</nickname>'
        '</publisher></mmreader></appmsg>'
        f'<fromusername>{OFFICIAL}</fromusername></msg>'
    )


def test_direct_publisher_enriches_conversation_and_old_message_without_rewriting_history(env):
    db, channel, _, _ = env
    conversation = Conversation(tenant_id=1, bot_id=channel.bot_id, channel_id=channel.id,
                                channel_type=channel.channel_type, external_id=OFFICIAL,
                                chat_type='direct', title=OFFICIAL)
    db.add(conversation)
    db.flush()
    old = Message(tenant_id=1, conversation_id=conversation.id, direction='in', sender_id=OFFICIAL,
                  content='历史文章', message_type='link')
    db.add(old)
    db.commit()
    ingest_inbound_event(db, channel, {'event_id': 'official-article', 'body': {
        'chat_id': OFFICIAL, 'sender_id': OFFICIAL, 'msg_type': 'app', 'msg_subtype': 'link',
        'content': article(), 'content_xml': article(),
    }})
    contact = db.scalar(select(WorkWechatContact).where(WorkWechatContact.contact_external_id == OFFICIAL))
    assert contact is not None and contact.contact_kind == 'official'
    assert contact.name == contact.display_name == conversation.title == '天然工坊'
    identity = IdentityDirectory(db).for_messages([old])[old.id]
    assert identity['name'] == '天然工坊' and not identity['avatar_url']
    assert old.sender_name is None  # 历史显示读取身份目录，无需重写旧消息。


@pytest.mark.parametrize('chat_id,sender_id,publisher', [
    ('friend', 'friend', OFFICIAL),
    (OFFICIAL, OFFICIAL, 'gh_other'),
    ('group@chatroom', OFFICIAL, OFFICIAL),
    (OFFICIAL, 'wxid_other', OFFICIAL),
])
def test_forwarded_or_mismatched_publisher_cannot_create_an_official_profile(env, chat_id, sender_id, publisher):
    """文章发布者不是每次转发的会话对象，必须同时核对会话 ID、发送者和发布者。"""
    db, channel, _, _ = env
    ingest_inbound_event(db, channel, {'event_id': 'forwarded', 'body': {
        'chat_id': chat_id, 'sender_id': sender_id, 'msg_type': 'app', 'msg_subtype': 'link',
        'content': article(publisher=publisher),
    }})
    assert db.scalar(select(WorkWechatContact).where(WorkWechatContact.contact_external_id == OFFICIAL)) is None


def test_official_directory_saves_actual_profile_and_enriches_existing_identity(env):
    db, channel, _, _ = env
    counts = service.persist_directory_result(db, channel, {'type': 'official_accounts'}, {'items': [
        {'account_id': OFFICIAL, 'nick_name': '目录中的公众号名称', 'avatar_url': 'https://example.test/account.jpg'},
    ]})
    db.flush()
    assert counts['contacts'] == 1
    contact = db.scalar(select(WorkWechatContact).where(WorkWechatContact.contact_external_id == OFFICIAL))
    assert contact.contact_kind == 'official'
    identity = IdentityDirectory(db).get(channel.id, OFFICIAL)
    assert identity['name'] == '目录中的公众号名称' and identity['avatar_url'] == 'https://example.test/account.jpg'


def test_incomplete_official_directory_keeps_known_name_and_avatar(env):
    db, channel, _, _ = env
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_kind='official',
                            contact_external_id=OFFICIAL, name='已确认的名称', display_name='已确认的名称',
                            avatar_url='https://example.test/account.jpg'))
    db.flush()
    counts = service.persist_directory_result(db, channel, {'type': 'official_accounts'}, {'items': [
        {'account_id': OFFICIAL, 'nick_name': '', 'avatar_url': ''},
    ]})
    db.flush()
    assert counts['contacts'] == 1
    identity = IdentityDirectory(db).get(channel.id, OFFICIAL)
    assert identity['name'] == '已确认的名称' and identity['avatar_url'] == 'https://example.test/account.jpg'


@pytest.mark.parametrize('content', ['<broken>', article() * 2000,
    '<!DOCTYPE msg [<!ENTITY name "伪造名称">]><msg><appmsg><mmreader><publisher><username>gh_a1b2c3d4</username><nickname>&name;</nickname></publisher></mmreader></appmsg></msg>',
])
def test_invalid_or_unbounded_xml_is_not_used_as_profile(content):
    assert publisher_name(OFFICIAL, content) == ''


@pytest.mark.parametrize('parent', ['appmsg', 'appmsg/mmreader/category/item'])
def test_unrelated_publisher_locations_are_not_used_as_account_identity(parent):
    """只认已确认的账号发布者位置，不能递归误取文章内部同名标签。"""
    content = f'<publisher><username>{OFFICIAL}</username><nickname>错误位置的名称</nickname></publisher>'
    for tag in reversed(parent.split('/')):
        content = f'<{tag}>{content}</{tag}>'
    assert publisher_name(OFFICIAL, f'<msg>{content}</msg>') == ''
