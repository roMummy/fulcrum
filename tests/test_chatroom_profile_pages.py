"""群资料、双任务同步与邀请图谱的真实路由验证，全部使用临时数据库与构造设备回报。"""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation, Message, TaskJob
from app.services.channel_events import ingest_inbound_event
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service
from test_chatroom_invitations import invitation_xml
from test_provider_node_pages import _login, provider_page_context  # noqa: F401

IDENTITY = {'device_id': 'profile-device', 'account_id': 'wxid_self', 'session_id': 'profile-session'}


@pytest.fixture()
def profile_page_context(provider_page_context):
    """准备角色、发言及退群顺序相互冲突的成员，避免测试只验证角色标签文字。"""
    client, ids = provider_page_context
    current = now_shanghai()
    with ids['factory']() as db:
        channel = Channel(tenant_id=ids['alpha'], bot_id=ids['bot'], name='群资料账号', channel_type='wechat_webridge',
                          is_enabled=True, runtime_info_json=json.dumps({'webridge': IDENTITY}))
        db.add(channel)
        db.flush()
        room = Chatroom(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=channel.id, external_id='profile@chatroom',
                        name='资料测试群', owner_external_id='wxid_owner', management_status='enabled', is_active=True,
                        extra_json='{"avatar_url":"/static/group.png"}')
        db.add(room)
        db.flush()
        for key, nickname, role, status, active in [
            ('wxid_recent', '普通最近', 'member', 'active', current),
            ('wxid_admin_old', '管理员较早', 'admin', 'active', current - timedelta(days=10)),
            ('wxid_left', '已退旧群主', 'owner', 'left', current),
            ('wxid_owner', '安静群主', 'member', 'active', None),
            ('wxid_admin_new', '管理员近期', 'admin', 'active', current - timedelta(days=1)),
            ('wxid_silent', '沉默群友', 'member', 'active', None),
        ]:
            db.add(ChatroomMember(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=channel.id,
                                  chatroom_id=room.id, member_external_id=key, nickname=nickname,
                                  display_name='群主群昵称' if key == 'wxid_owner' else None,
                                  role=role, status=status, last_active_at=active))
        db.commit()
        yield client, ids, channel.id, room.id


@pytest.mark.parametrize('channel_type', ['wechat_webridge', 'workwechat', 'wechat_ipad', 'wechat_gugatool'])
def test_members_share_role_first_order_and_invitation_view(profile_page_context, channel_type):
    client, ids, channel_id, room_id = profile_page_context
    with ids['factory']() as db:
        db.get(Channel, channel_id).channel_type = channel_type
        db.commit()
    _login(client, 'admin')
    response = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=members')
    assert response.status_code == 200
    body = response.text.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
    names = ['安静群主', '管理员近期', '管理员较早', '普通最近', '沉默群友', '已退旧群主']
    assert [body.index(name) for name in names] == sorted(body.index(name) for name in names)
    assert '邀请关系图谱' in response.text and '<th>邀请人</th>' in response.text and '未知' in body
    # 六名成员都没有邀请证据，图谱应为空而成员资料完整保留，不能因角色产生虚假连线。
    assert '暂未记录到邀请关系' in response.text and 'data-invitation-scene' not in response.text
    assert response.text.count('data-connected="false"') == 6
    assert '群主群昵称' in body and 'chip-success">群主' in body
    if channel_type == 'wechat_webridge':
        assert '同步群资料' in response.text


@pytest.mark.parametrize('state,body,expected', [
    ('not_fetched', '', '暂未获取群公告'),
    ('not_cached', '', '手机暂未缓存公告'),
    ('summary', '只有摘要', '公告正文尚未缓存'),
    ('empty', '', '暂未读到文字内容'),
    ('body', '第一行\n&lt;script&gt;原样字符', '获取时间'),
])
def test_profile_renders_notice_states_and_pinned_capability(profile_page_context, state, body, expected):
    client, ids, channel_id, room_id = profile_page_context
    with ids['factory']() as db:
        room = db.get(Chatroom, room_id)
        saved = {'state': state, 'body': body, 'fetched_at': now_shanghai().isoformat()}
        room.extra_json = json.dumps({'group_notice': saved})
        db.commit()
    _login(client, 'admin')
    response = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=profile')
    assert response.status_code == 200 and expected in response.text
    assert '群主群昵称' in response.text and '群内置顶消息' in response.text
    assert '当前渠道暂不支持读取' in response.text and '没有公告' not in response.text
    assert 'name="tab" value="profile"' in response.text
    with ids['factory']() as db:
        assert db.scalars(select(TaskJob)).all() == []


def _notice_result(**changes):
    return {'room_id': 'profile@chatroom', 'cached_notice_found': True, 'html_cached': True,
            'summary': '公告摘要', 'html': '<p>本周活动安排</p><p>&lt;script&gt;原样文字&lt;/script&gt;</p><script>evil()</script><img src="http://127.0.0.1/private">', **changes}


def _complete_notice(ids, channel_id, job_id, result, *, ok=True):
    """直接调用生产回报入口，保持真实租户会话、任务终态与幂等流程。"""
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        channel, job = db.get(Channel, channel_id), db.get(TaskJob, job_id)
        request_id = parse_job_payload(job)['command']['request_id']
        if job.status == 'pending':
            job.status = 'running'
            db.commit()
        return service.accept_command_result(db, channel, {**IDENTITY, 'request_id': request_id, 'ok': ok,
                                                          'error': '' if ok else '手机读取失败', 'result': result})


def test_sync_enqueues_members_and_notice_and_persists_safe_idempotent_reply(profile_page_context):
    client, ids, channel_id, room_id = profile_page_context
    csrf = _login(client, 'operator')
    response = client.post(f'/channels/{channel_id}/chatrooms/{room_id}/sync', data={'csrf_token': csrf, 'tab': 'profile'})
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['tab'] == ['profile']
    job_ids = [int(value) for value in query['operation_jobs'][0].split(',')]
    with ids['factory']() as db:
        commands = {parse_job_payload(db.get(TaskJob, key))['command']['type']: key for key in job_ids}
        assert set(commands) == {'room_members', 'get_group_notice'}
        assert all(parse_job_payload(db.get(TaskJob, key))['command']['params'] == {'room_id': 'profile@chatroom'} for key in job_ids)
    assert _complete_notice(ids, channel_id, commands['get_group_notice'], _notice_result())['status'] == 'completed'
    repeated = _complete_notice(ids, channel_id, commands['get_group_notice'], _notice_result(html='<p>重复覆盖</p>'))
    assert repeated['deduplicated'] is True
    page = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=profile')
    assert page.status_code == 200 and '本周活动安排' in page.text and '重复覆盖' not in page.text
    assert '&lt;script&gt;原样文字&lt;/script&gt;' in page.text
    assert 'evil()' not in page.text and 'http://127.0.0.1/private' not in page.text
    assert '公告包含图片' in page.text and '获取时间' in page.text
    status = client.get(f'/channels/{channel_id}/operations/status?job_ids={query["operation_jobs"][0]}').json()
    assert status['done'] is False and status['successful'] == 1 and status['failed'] == 0


@pytest.mark.parametrize('ok,result', [(False, {}), (True, {}), (True, _notice_result(room_id='wrong@chatroom'))])
def test_failed_or_invalid_reply_acks_and_preserves_existing_notice(profile_page_context, ok, result):
    client, ids, channel_id, room_id = profile_page_context
    csrf = _login(client, 'admin')
    with ids['factory']() as db:
        db.get(Chatroom, room_id).extra_json = json.dumps({'group_notice': {
            'state': 'body', 'body': '上次公告', 'fetched_at': '2026-11-13T10:00:00+08:00'}})
        db.commit()
    queued = client.post(f'/channels/{channel_id}/chatrooms/{room_id}/sync', data={'tab': 'profile'},
                         headers={'X-Requested-With': 'fetch', 'X-CSRF-Token': csrf}).json()
    assert len(queued['jobs']) == 2
    with ids['factory']() as db:
        notice_id = next(row['id'] for row in queued['jobs'] if parse_job_payload(db.get(TaskJob, row['id']))['command']['type'] == 'get_group_notice')
    ack = _complete_notice(ids, channel_id, notice_id, result, ok=ok)
    assert ack['ok'] is True and ack['status'] == 'failed'
    page = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=profile')
    assert page.status_code == 200 and '上次公告' in page.text and '2026-11-13 10:00:00' in page.text
    if result.get('room_id') != 'wrong@chatroom':
        assert '本次公告读取失败' in page.text


def test_invitation_backfill_is_visible_to_ops_and_uses_only_this_groups_system_messages(profile_page_context):
    client, ids, channel_id, room_id = profile_page_context
    with ids['factory']() as db:
        conversation = Conversation(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=channel_id,
                                    channel_type='wechat_webridge', external_id='profile@chatroom', chat_type='group')
        db.add(conversation)
        db.flush()
        db.add(Message(tenant_id=ids['alpha'], conversation_id=conversation.id, direction='in', message_type='system',
                       content=invitation_xml('wxid_owner', ('wxid_recent',))))
        db.commit()
    csrf = _login(client, 'operator')
    path = f'/channels/{channel_id}/chatrooms/{room_id}/invitations/backfill'
    assert client.post(path, data={'csrf_token': 'invalid'}).status_code == 403
    response = client.post(path, data={'csrf_token': csrf})
    assert response.status_code == 303 and 'tab=members' in response.headers['location']
    page = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=members')
    assert page.status_code == 200 and '已知 1 / 6 人' in page.text
    assert '入群系统消息' in page.text and '直接邀请 1 人' in page.text and '群主群昵称' in page.text
    # 只有邀请双方进入画布，其余四人留在未关联区；头像卡片与搜索共用唯一成员节点。
    assert 'data-invitation-scene' in page.text
    assert page.text.count('data-connected="true"') == 2 and page.text.count('data-connected="false"') == 4
    readonly_csrf = _login(client, 'readonly')
    readonly = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=members')
    assert readonly.status_code == 200 and '邀请关系图谱' in readonly.text
    assert '补查历史邀请</button>' not in readonly.text
    assert client.post(path, data={'csrf_token': readonly_csrf}).status_code == 403


def test_invitation_graph_empty_group_and_member_text_are_safe(profile_page_context):
    """真实模板必须保留文本转义；没有成员时返回专用空状态，不能生成图谱或补造节点。"""
    client, ids, channel_id, room_id = profile_page_context
    _login(client, 'readonly')
    with ids['factory']() as db:
        members = db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == room_id)).all()
        members[0].nickname = '\"><script>window.invitationUnsafe=1</script>'
        db.commit()
    page = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=members')
    assert page.status_code == 200 and '&lt;script&gt;window.invitationUnsafe=1&lt;/script&gt;' in page.text
    assert '<script>window.invitationUnsafe=1</script>' not in page.text
    with ids['factory']() as db:
        for member in db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == room_id)):
            db.delete(member)
        db.commit()
    empty = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=members')
    assert empty.status_code == 200 and '暂无成员信息' in empty.text and '已知 0 / 0 人' in empty.text
    assert 'data-invitation-node=' not in empty.text and 'data-invitation-scene' not in empty.text


def test_incoming_invitation_is_recorded_with_member_notifications_disabled(profile_page_context):
    _, ids, channel_id, room_id = profile_page_context
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        channel = db.get(Channel, channel_id)
        result = ingest_inbound_event(db, channel, {**IDENTITY, 'event_name': 'message', 'event_id': 'system-invite-1', 'body': {
            'msg_type': 'system', 'raw_message_type': 10002, 'room_id': 'profile@chatroom', 'is_group': True,
            'content': invitation_xml('wxid_owner', ('wxid_recent',)), 'message_id': 'system-invite-1',
        }})
        assert result['ok'] is True
        member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room_id, ChatroomMember.member_external_id == 'wxid_recent'))
        assert json.loads(member.extra_json)['invitation']['inviter_external_id'] == 'wxid_owner'


def test_group_profile_and_backfill_reject_other_tenant_and_unsupported_notice_is_explicit(profile_page_context):
    client, ids, channel_id, room_id = profile_page_context
    with ids['factory']() as db:
        other_bot = Bot(tenant_id=ids['beta'], name='其他企业机器人')
        db.add(other_bot)
        db.flush()
        other_channel = Channel(tenant_id=ids['beta'], bot_id=other_bot.id, name='其他账号', channel_type='wechat_webridge')
        db.add(other_channel)
        db.flush()
        other_room = Chatroom(tenant_id=ids['beta'], bot_id=other_bot.id, channel_id=other_channel.id, external_id='other@chatroom')
        db.add(other_room)
        db.get(Channel, channel_id).channel_type = 'wechat_ipad'
        db.commit()
        other_channel_id, other_room_id = other_channel.id, other_room.id
    csrf = _login(client, 'admin')
    page = client.get(f'/channels/{channel_id}/chatrooms/{room_id}?tab=profile')
    assert page.status_code == 200 and '当前渠道暂未接入公告读取' in page.text
    for channel_key, room_key in [(channel_id, other_room_id), (other_channel_id, other_room_id)]:
        base = f'/channels/{channel_key}/chatrooms/{room_key}'
        assert client.get(base + '?tab=profile').status_code == 404
        for action in ('sync', 'invitations/backfill'):
            assert client.post(base + '/' + action, data={'csrf_token': csrf}).status_code == 404
