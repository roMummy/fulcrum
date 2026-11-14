"""邀请关系的轻量验证：只根据 ID 建边，覆盖证据优先级、系统消息和有环/深层成员树。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, GroupPluginScope, Message
from app.services.chatroom_invitations import (
    backfill_chatroom_invitations,
    build_invitation_graph,
    member_invitation,
    parse_system_invitation,
    record_invitation_event,
    save_member_invitation,
)


def invitation_xml(inviter='a', members=('b',), *, method='invite'):
    """仿照微信系统消息的模板和 memberlist 构造稳定 ID，不依赖昵称匹配。"""
    template = '"$username$"邀请"$names$"加入了群聊'
    who, whom = 'username', 'names'
    if method == 'qr':
        template = '"$adder$"通过扫描"$from$"分享的二维码加入群聊'
        who, whom = 'from', 'adder'
    if inviter == 'self':
        template = '你邀请"$names$"加入了群聊'
    links = f'<link name="{who}"><memberlist><member><username>{inviter}</username><nickname>同名</nickname></member></memberlist></link>'
    links += f'<link name="{whom}"><memberlist>' + ''.join(f'<member><username>{key}</username><nickname>同名</nickname></member>' for key in members) + '</memberlist></link>'
    return f'<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template><template>{template}</template><link_list>{links}</link_list></content_template></sysmsgtemplate></sysmsg>'


@pytest.fixture()
def invitation_db():
    """使用隔离的小表集合验证跨群/租户过滤，历史补查不运行调度或真实协议。"""
    engine = create_engine('sqlite://')
    for model in (Chatroom, ChatroomMember, Conversation, Message, BotPlugin, GroupPluginScope):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db, tenant_scope(1):
        channel = Channel(id=1, tenant_id=1, bot_id=1, channel_type='wechat_webridge', runtime_info_json=json.dumps({'webridge': {'account_id': 'my-id'}}))
        room = Chatroom(tenant_id=1, channel_id=1, bot_id=1, external_id='g@chatroom')
        conversation = Conversation(tenant_id=1, channel_id=1, bot_id=1, channel_type='wechat_webridge', external_id=room.external_id, chat_type='group')
        db.add_all([room, conversation])
        db.flush()
        for key in ('a', 'b', 'c'):
            db.add(ChatroomMember(tenant_id=1, channel_id=1, bot_id=1, chatroom_id=room.id, member_external_id=key, nickname='同名', status='active'))
        db.commit()
        yield db, channel, room, conversation
    engine.dispose()


def test_invitation_parser_requires_explicit_ids_and_marks_qr_source():
    parsed = parse_system_invitation(invitation_xml(members=('b', 'c')))
    assert parsed['inviter_external_id'] == 'a' and parsed['member_ids'] == ['b', 'c']
    assert parse_system_invitation(invitation_xml(method='qr'))['method'] == 'qr'
    assert parse_system_invitation(invitation_xml(inviter='self'), own_id='my-id')['inviter_external_id'] == 'my-id'
    assert parse_system_invitation(invitation_xml(inviter='self')) is None
    assert parse_system_invitation(invitation_xml().replace('<username>a</username>', '')) is None
    assert parse_system_invitation('张三邀请李四加入了群聊') is None
    assert parse_system_invitation('<sysmsg') is None


def test_invitation_priority_empty_value_and_old_message_do_not_erase_evidence(invitation_db):
    db, _, _, _ = invitation_db
    member = db.query(ChatroomMember).filter_by(member_external_id='b').one()
    member.extra_json = '{"avatar_url":"/static/avatar.png"}'
    current = now_shanghai()
    assert save_member_invitation(member, 'a', source='system_message', recorded_at=current)
    assert not save_member_invitation(member, '', source='member_profile', recorded_at=current)
    assert not save_member_invitation(member, 'c', source='system_message', recorded_at=current - timedelta(days=1))
    assert save_member_invitation(member, 'c', source='member_profile', recorded_at=current)
    assert not save_member_invitation(member, 'a', source='system_message', recorded_at=current + timedelta(days=1))
    assert member_invitation(member)['inviter_external_id'] == 'c'
    assert json.loads(member.extra_json)['avatar_url'] == '/static/avatar.png'
    assert not save_member_invitation(member, 'b', source='member_profile', recorded_at=current)


def test_repeated_relation_keeps_newest_evidence_ahead_of_a_delayed_other_inviter(invitation_db):
    """同一个人再次邀请也属于新证据，之后晚到的较早消息不能改回中间那次邀请人。"""
    db, _, _, _ = invitation_db
    member = db.query(ChatroomMember).filter_by(member_external_id='b').one()
    first = now_shanghai() - timedelta(days=3)
    latest = first + timedelta(days=2)
    assert save_member_invitation(member, 'a', source='system_message', recorded_at=first, event_key='first-join')
    save_member_invitation(member, 'a', source='system_message', recorded_at=latest, event_key='latest-join')
    assert not save_member_invitation(member, 'c', source='system_message', recorded_at=first + timedelta(days=1), event_key='delayed-join')
    assert member_invitation(member)['inviter_external_id'] == 'a'
    assert member_invitation(member)['recorded_at'] == latest.isoformat()
    # 相同事件重复送达不再推进证据时间，也不计算成新的邀请关系。
    assert not save_member_invitation(member, 'a', source='system_message', recorded_at=latest + timedelta(seconds=1), event_key='latest-join')


def test_workwechat_member_normalization_uses_only_explicit_inviter_id_fields():
    """泛化 inviter 字段可能只有显示名称，不能用它建立成员 ID 关系。"""
    from app.services.workwechat_chatrooms import _normalize_member_item

    assert _normalize_member_item({'user_id': 'b', 'inviter': '张三'})['invite_user_id'] == ''
    assert _normalize_member_item({'user_id': 'b', 'invite_user_id': 'a'})['invite_user_id'] == 'a'


@pytest.mark.parametrize('channel_type', ['wechat_ipad', 'workwechat'])
def test_member_profile_adapters_normalize_inviter_and_keep_it_when_next_snapshot_is_empty(invitation_db, channel_type):
    """直接运行本次改动的成员落库流程，避免只有视图测试而遗漏同步会清空邀请关系。"""
    from app.services.wechat_ipad.chatrooms import _replace_chatroom_members as ipad_members
    from app.services.workwechat_chatrooms import _replace_chatroom_members as work_members

    db, channel, room, _ = invitation_db
    channel.channel_type = channel_type
    replace = ipad_members if channel_type == 'wechat_ipad' else work_members
    field = 'inviter_user_name' if channel_type == 'wechat_ipad' else 'invite_user_id'
    item = {'member_external_id': 'b', 'nickname': '成员', 'display_name': '群昵称', 'role': 'member', field: 'a'}
    replace(db, channel=channel, chatroom=room, members=[item], synced_at=now_shanghai(), source='test-profile')
    member = db.query(ChatroomMember).filter_by(member_external_id='b').one()
    assert member_invitation(member)['inviter_external_id'] == 'a'
    assert field not in json.loads(member.extra_json)
    replace(db, channel=channel, chatroom=room, members=[{**item, field: ''}], synced_at=now_shanghai(), source='test-profile')
    assert member_invitation(member)['inviter_external_id'] == 'a'


@pytest.mark.parametrize('channel_type', ['wechat_ipad', 'workwechat', 'wechat_gugatool'])
def test_ordinary_member_message_preserves_recorded_invitation(invitation_db, channel_type):
    """邀请边取得后继续走真实发言落库流程，验证下一条普通消息不会把关系清空。"""
    from app.services.wechat_gugatool_chatrooms import sync_wechat_gugatool_chatroom_from_message
    from app.services.wechat_ipad.chatrooms import sync_wechat_ipad_chatroom_from_message
    from app.services.workwechat_chatrooms import sync_workwechat_chatroom_from_message

    db, channel, room, conversation = invitation_db
    channel.channel_type = conversation.channel_type = channel_type
    room.name = '邀请关系测试群'
    # 此例已有成员快照，模拟正常发言更新，避免把名册首次发现混入断言。
    room.extra_json = '{"roster_synced":true}'
    member = db.query(ChatroomMember).filter_by(member_external_id='b').one()
    assert save_member_invitation(member, 'a', source='system_message', recorded_at=now_shanghai())
    db.commit()
    sync = {
        'wechat_ipad': sync_wechat_ipad_chatroom_from_message,
        'workwechat': sync_workwechat_chatroom_from_message,
        'wechat_gugatool': sync_wechat_gugatool_chatroom_from_message,
    }[channel_type]
    sync(db, channel, {
        'conversation': {'chat_type': 'group', 'external_id': room.external_id, 'title': room.name},
        'message': {'message_type': 'text', 'content': '正常发言', 'sender_id': 'b', 'sender_name': '成员乙'},
        'details': {}, 'event_key': 'after-invitation',
    }, conversation=conversation)
    db.flush()
    assert member.nickname == '成员乙' and member.last_active_at is not None
    assert member_invitation(member)['inviter_external_id'] == 'a'
    assert member_invitation(member)['source'] == 'system_message'


def test_system_message_records_multiple_members_but_ordinary_text_and_other_tenant_do_not(invitation_db):
    db, channel, _, conversation = invitation_db
    normalized = {'message': {'message_type': 'text', 'content': invitation_xml(members=('b', 'c'))}, 'details': {}, 'event_key': 'join-1'}
    assert record_invitation_event(db, channel, conversation, normalized, recorded_at=now_shanghai()) == 0
    normalized['message']['message_type'] = 'system'
    assert record_invitation_event(db, channel, conversation, normalized, recorded_at=now_shanghai()) == 2
    assert record_invitation_event(db, channel, conversation, normalized, recorded_at=now_shanghai()) == 0
    with tenant_scope(2):
        assert record_invitation_event(db, channel, conversation, normalized, recorded_at=now_shanghai()) == 0
    assert [member_invitation(row)['inviter_external_id'] for row in db.query(ChatroomMember).filter(ChatroomMember.member_external_id.in_(('b', 'c')))] == ['a', 'a']


def test_historical_backfill_is_limited_to_this_group_and_keeps_left_members(invitation_db):
    db, channel, room, conversation = invitation_db
    member = db.query(ChatroomMember).filter_by(member_external_id='b').one()
    member.status = 'left'
    other = Conversation(tenant_id=2, bot_id=2, channel_id=2, channel_type='wechat_webridge', external_id='other@chatroom', chat_type='group')
    db.add(other)
    db.flush()
    db.add_all([
        Message(tenant_id=1, conversation_id=conversation.id, direction='in', content=invitation_xml(members=('b', 'missing')), message_type='system'),
        Message(tenant_id=2, conversation_id=other.id, direction='in', content=invitation_xml(inviter='c'), message_type='system'),
        Message(tenant_id=1, conversation_id=conversation.id, direction='in', content=invitation_xml(inviter='c'), message_type='text'),
    ])
    db.commit()
    report = backfill_chatroom_invitations(db, channel, room)
    assert report['checked'] == 1 and report['updated'] == 1
    assert member_invitation(member)['inviter_external_id'] == 'a' and member.status == 'left'
    assert db.query(ChatroomMember).count() == 3
    assert backfill_chatroom_invitations(db, channel, room)['updated'] == 0


def _view(key, inviter='', *, status='active'):
    """所有节点故意同名，断言关系只能按 ID 建立。"""
    return {'member_external_id': key, 'nickname': '同名', 'display_name': '-', 'role': 'member', 'status': status,
            'avatar_url': '', 'invitation': {'inviter_external_id': inviter, 'source': 'member_profile', 'method': 'invite'}}


def test_graph_separates_isolated_members_and_keeps_unknown_roots_and_unlisted_inviters():
    """根节点上级未知仍可连接下级，真正孤立的成员才单列，缺失邀请人不能丢失。"""
    members = [_view('a'), _view('b', 'a'), _view('c', 'b', status='left'), _view('d', 'missing'), _view('isolated')]
    tree = build_invitation_graph(members)
    assert tree['known_count'] == 3 and tree['unknown_count'] == 2 and tree['unlinked_count'] == 1
    assert len(tree['nodes']) == 6
    depths = {row['external_id']: row['depth'] for row in tree['nodes']}
    assert depths == {'a': 0, 'b': 1, 'c': 2, 'missing': 0, 'd': 1, 'isolated': 0}
    assert {row['external_id'] for row in tree['nodes'] if not row['connected']} == {'isolated'}
    assert members[0]['direct_invites'] == 1 and members[1]['direct_invites'] == 1
    assert members[2]['inviter']['external_id'] == 'b'
    assert members[3]['inviter']['name'] == 'missing'


def test_graph_breaks_cycles_without_changing_saved_evidence_and_handles_deep_chains():
    members = [_view('a', 'b'), _view('b', 'a'), _view('c', 'b')]
    tree = build_invitation_graph(members)
    assert len(tree['nodes']) == 3 and tree['cycle_count'] == 1 and tree['unlinked_count'] == 0
    assert members[0]['inviter']['external_id'] == 'b' and members[1]['inviter']['external_id'] == 'a'
    # 使用合法个人 ID；"0" 是协议常见空占位，不能作为真实邀请人。
    deep = build_invitation_graph([_view(f'wxid_{i}', f'wxid_{i - 1}' if i else '') for i in range(1500)])
    assert len(deep['nodes']) == 1500 and deep['nodes'][-1]['depth'] == 1499


def test_graph_with_only_unknown_members_has_no_fabricated_edges():
    """无邀请证据时不因为群主身份生成星形关系，所有真实成员都在未关联区。"""
    members = [_view('owner'), _view('member')]
    members[0]['role'] = 'owner'
    graph = build_invitation_graph(members)
    assert graph['known_count'] == 0 and graph['unlinked_count'] == graph['total'] == 2
    assert all(not node['connected'] and not node['parent_key'] for node in graph['nodes'])
    assert build_invitation_graph([])['unlinked_count'] == 0
