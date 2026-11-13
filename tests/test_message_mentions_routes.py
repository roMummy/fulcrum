"""群成员选择、内联 @ 和 @所有人经过真实网页发送路由，全部使用隔离队列。"""
import json

import pytest
from sqlalchemy import func, select

from app.models.entities import Channel, Chatroom, ChatroomMember, Message, TaskJob
from test_message_actions import bridge  # noqa: F401
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import provider_page_context  # noqa: F401


@pytest.fixture()
def group(bridge):
    client, ids, cid, mids, csrf = bridge
    with ids['factory']() as db:
        room = Chatroom(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'], external_id='room@chatroom')
        db.add(room)
        db.flush()
        for external_id, name, status in [('wxid_quiet', '尚未发言', 'active'), ('wxid_left', '已离群', 'left'), ('wxid_a', '小明', 'active')]:
            db.add(ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'], chatroom_id=room.id,
                                  member_external_id=external_id, display_name=name, status=status))
        db.commit()
    return bridge


def test_member_route_includes_quiet_members_and_searches_names(group):
    client, _ids, cid, _mids, _csrf = group
    data = client.get('/messages/mention-members', params={'conversation_id': cid}).json()
    assert data['ok'] and {row['id'] for row in data['members']} == {'wxid_quiet', 'wxid_a'}
    filtered = client.get('/messages/mention-members', params={'conversation_id': cid, 'q': '未发'}).json()
    assert [row['id'] for row in filtered['members']] == ['wxid_quiet']


def test_inline_mention_keeps_name_and_position_in_card_but_native_id_in_command(group):
    client, ids, cid, _mids, csrf = group
    result = client.post('/messages/send', data={'conversation_id': cid, 'csrf_token': csrf,
        'content': '请{{mention:wxid_quiet}}明天来看', 'mention_ids': json.dumps(['wxid_quiet'])})
    assert result.status_code == 200 and result.json()['ok']
    with ids['factory']() as db:
        message = db.get(Message, result.json()['message_id'])
        assert message.content == '请@尚未发言\u2005明天来看'
        raw = json.loads(message.raw_request_json)
        assert raw['mentions'] == [{'id': 'wxid_quiet', 'name': '尚未发言'}]
        params = json.loads(db.get(TaskJob, raw['job_id']).payload_json)['command']['params']
        assert params['content'] == '请{{mention:wxid_quiet}}明天来看'
        assert params['mention_user_list'] == ['wxid_quiet']
    html = client.get('/messages/history', params={'conversation_id': cid, 'after_id': result.json()['message_id'] - 1}).json()['html']
    assert '请@尚未发言\u2005明天来看' in html and '{{mention:' not in html


@pytest.mark.parametrize('source', ['room-owner', 'member-owner', 'member-admin'])
def test_all_mention_uses_notification_marker_and_literal_name(group, source):
    client, ids, cid, _mids, csrf = group
    with ids['factory']() as db:
        room = db.scalar(select(Chatroom).where(Chatroom.channel_id == ids['channel_id'], Chatroom.external_id == 'room@chatroom'))
        if source == 'room-owner':
            room.owner_external_id = 'wxid_bot'
        else:
            db.add(ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'], chatroom_id=room.id,
                                  member_external_id='wxid_bot', display_name='本人', status='active',
                                  role='owner' if source == 'member-owner' else 'admin'))
        db.commit()
    candidates = client.get('/messages/mention-members', params={'conversation_id': cid}).json()
    assert any(row['id'] == 'notify@all' for row in candidates['members'])
    result = client.post('/messages/send', data={'conversation_id': cid, 'csrf_token': csrf,
        'content': '{{mention:notify@all}}', 'mention_ids': '["notify@all"]'})
    assert result.status_code == 200 and result.json()['ok']
    with ids['factory']() as db:
        message = db.get(Message, result.json()['message_id'])
        assert message.content == '@所有人'
        params = json.loads(db.get(TaskJob, json.loads(message.raw_request_json)['job_id']).payload_json)['command']['params']
        assert params['content'] == '@所有人' and params['mention_user_list'] == ['notify@all']


@pytest.mark.parametrize('content,mentions', [
    ('{{mention:wxid_left}}你好', ['wxid_left']),
    ('{{mention:wxid_outside}}你好', ['wxid_quiet']),
    ('{{mention:wxid_quiet}}你好', []),
])
def test_unknown_or_unselected_mentions_never_reach_phone(group, content, mentions):
    client, ids, cid, _mids, csrf = group
    response = client.post('/messages/send', data={'conversation_id': cid, 'csrf_token': csrf,
        'content': content, 'mention_ids': json.dumps(mentions)})
    assert response.status_code == 400 and not response.json()['ok']
    with ids['factory']() as db:
        assert db.scalar(select(TaskJob.id).limit(1)) is None


@pytest.mark.parametrize('condition', ['unknown-role', 'ordinary-member', 'left-admin', 'left-owner', 'unknown-identity', 'left-room'])
def test_all_permission_cannot_be_forged_through_send_route(group, condition):
    client, ids, cid, _mids, csrf = group
    with ids['factory']() as db:
        room = db.scalar(select(Chatroom).where(Chatroom.channel_id == ids['channel_id'], Chatroom.external_id == 'room@chatroom'))
        if condition != 'unknown-role':
            db.add(ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'], chatroom_id=room.id,
                                  member_external_id='wxid_bot', display_name='本人',
                                  status='left' if condition in {'left-admin', 'left-owner'} else 'active',
                                  role='member' if condition == 'ordinary-member' else 'admin'))
        if condition == 'left-owner':
            room.owner_external_id = 'wxid_bot'
        if condition == 'unknown-identity':
            db.get(Channel, ids['channel_id']).runtime_info_json = '{}'
        if condition == 'left-room':
            room.status = 'left'
        db.commit()
        count = db.scalar(select(func.count(Message.id)))
    candidates = client.get('/messages/mention-members', params={'conversation_id': cid}).json()
    assert not any(row['id'] == 'notify@all' for row in candidates['members'])
    for content in ('{{mention:notify@all}}', '@所有人'):
        response = client.post('/messages/send', data={'conversation_id': cid, 'csrf_token': csrf,
            'content': content, 'mention_ids': '["notify@all"]', 'role': 'admin', 'is_admin': 'true', 'owner_external_id': 'wxid_bot'})
        assert response.status_code == 400 and not response.json()['ok']
        assert '只有群主或管理员才能 @所有人' in response.json()['notice']
    with ids['factory']() as db:
        assert db.scalar(select(TaskJob.id).limit(1)) is None
        assert db.scalar(select(func.count(Message.id))) == count


def test_old_group_with_no_directory_cannot_send_all(bridge):
    client, ids, cid, _mids, csrf = bridge
    candidates = client.get('/messages/mention-members', params={'conversation_id': cid}).json()
    assert not any(row['id'] == 'notify@all' for row in candidates['members'])
    response = client.post('/messages/send', data={'conversation_id': cid, 'csrf_token': csrf,
        'content': '{{mention:notify@all}}', 'mention_ids': '["notify@all"]'})
    assert response.status_code == 400 and '只有群主或管理员才能 @所有人' in response.json()['notice']
    with ids['factory']() as db:
        assert db.scalar(select(TaskJob.id).limit(1)) is None


def test_role_is_checked_again_when_sending_after_picker_was_opened(group):
    client, ids, cid, _mids, csrf = group
    with ids['factory']() as db:
        room = db.scalar(select(Chatroom).where(Chatroom.channel_id == ids['channel_id'], Chatroom.external_id == 'room@chatroom'))
        member = ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'], chatroom_id=room.id,
                                member_external_id='wxid_bot', display_name='本人', role='admin', status='active')
        db.add(member)
        db.commit()
        own_member_id = int(member.id)
    candidates = client.get('/messages/mention-members', params={'conversation_id': cid}).json()
    assert any(row['id'] == 'notify@all' for row in candidates['members'])
    with ids['factory']() as db:
        db.get(ChatroomMember, own_member_id).role = 'member'
        db.commit()
    response = client.post('/messages/send', data={'conversation_id': cid, 'csrf_token': csrf,
        'content': '{{mention:notify@all}}', 'mention_ids': '["notify@all"]'})
    assert response.status_code == 400 and '只有群主或管理员才能 @所有人' in response.json()['notice']
    with ids['factory']() as db:
        assert db.scalar(select(TaskJob.id).limit(1)) is None
