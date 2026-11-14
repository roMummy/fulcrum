"""群公告与成员排序的定向用例，全部使用独立内存数据库和构造的手机回报。"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom
from app.services.chatroom_profile import (
    build_chatroom_profile,
    effective_member_role,
    save_webridge_group_notice,
    sort_chatroom_members,
)


def test_members_prioritize_current_owner_admins_and_members_before_left():
    """角色优先于活跃时间，群主 ID 可补足角色缺失，已退群的旧群主也必须置底。"""
    now = now_shanghai()
    rows = [
        SimpleNamespace(member_external_id=key, role=role, status=status, last_active_at=active, display_name=key, nickname=key, id=index)
        for index, (key, role, status, active) in enumerate([
            ('recent-member', 'member', 'active', now),
            ('admin-old', 'admin', 'active', now - timedelta(days=5)),
            ('left-owner', 'owner', 'left', now),
            ('owner', None, 'active', None),
            ('admin-new', 'admin', 'active', now - timedelta(days=1)),
            ('silent-member', 'member', 'active', None),
        ], 1)
    ]
    ordered = sort_chatroom_members(rows, owner_external_id='owner')
    assert [row.member_external_id for row in ordered] == ['owner', 'admin-new', 'admin-old', 'recent-member', 'silent-member', 'left-owner']
    assert effective_member_role(ordered[0], 'owner') == 'owner'
    assert effective_member_role(ordered[-1], 'owner') == 'member'


@pytest.fixture()
def notice_db():
    """只创建群表；使用裸 Session 验证公告写入自身也具备明确的租户、账号和群边界。"""
    engine = create_engine('sqlite://')
    Chatroom.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        room = Chatroom(tenant_id=1, bot_id=1, channel_id=2, external_id='g@chatroom', name='测试群', extra_json=json.dumps({'group_features': {'welcome': True}, 'avatar_url': '/static/group.png'}))
        other = Chatroom(tenant_id=2, bot_id=3, channel_id=4, external_id='g@chatroom', name='其他企业')
        db.add_all([room, other])
        db.commit()
        channel = Channel(id=2, tenant_id=1, bot_id=1, name='微桥', channel_type='wechat_webridge')
        yield db, channel, room, other
    engine.dispose()


def _save(db, channel, result, *, ok=True, requested_at=None, error=''):
    return save_webridge_group_notice(db, channel, {'type': 'get_group_notice', 'params': {'room_id': 'g@chatroom'}}, result, ok=ok, requested_at=requested_at or now_shanghai(), error=error)


def _result(**updates):
    return {'room_id': 'g@chatroom', 'cached_notice_found': True, 'summary': '公告摘要', 'html': '<p>第一段</p><p>第二段<br>下一行</p>', 'html_cached': True, **updates}


def test_notice_keeps_full_text_paragraphs_and_existing_group_settings(notice_db):
    db, channel, room, _ = notice_db
    with tenant_scope(1):
        assert _save(db, channel, _result()) is None
    data = json.loads(room.extra_json)
    assert data['group_features'] == {'welcome': True} and data['avatar_url'] == '/static/group.png'
    profile = build_chatroom_profile(channel, room, [])
    assert profile['notice']['body'] == '第一段\n第二段\n下一行'
    assert profile['notice']['state'] == 'body' and profile['notice']['fetched_at']
    assert profile['notice']['source_label'] == '手机缓存'
    assert profile['pinned']['supported'] is False


def test_notice_does_not_render_scripts_or_request_external_images(notice_db):
    db, channel, room, _ = notice_db
    html = '<p onclick="alert(1)">安全正文</p><script>secret()</script><style>bad-css</style><img src="http://127.0.0.1/private" onerror="alert(1)"><p>&lt;script&gt;原样文字&lt;/script&gt;</p>'
    with tenant_scope(1):
        assert _save(db, channel, _result(html=html)) is None
    notice = build_chatroom_profile(channel, room, [])['notice']
    assert '安全正文' in notice['body'] and '<script>原样文字</script>' in notice['body']
    assert all(value not in notice['body'] for value in ('secret()', 'bad-css', 'http://127.0.0.1', 'onclick', 'onerror'))
    assert notice['has_images'] is True


@pytest.mark.parametrize(('result', 'state', 'body'), [
    (_result(html=None, html_cached=False), 'summary', '公告摘要'),
    (_result(cached_notice_found=False, html=None, html_cached=False, summary=None), 'not_cached', ''),
])
def test_notice_distinguishes_summary_and_missing_phone_cache(notice_db, result, state, body):
    db, channel, room, _ = notice_db
    with tenant_scope(1):
        assert _save(db, channel, result) is None
    notice = build_chatroom_profile(channel, room, [])['notice']
    assert notice['state'] == state and notice['body'] == body and notice['fetched_at']


def test_notice_failure_preserves_previous_content_and_stale_results_are_ignored(notice_db):
    db, channel, room, _ = notice_db
    started = now_shanghai()
    with tenant_scope(1):
        assert _save(db, channel, _result(), requested_at=started) is None
        before = json.loads(room.extra_json)['group_notice']['fetched_at']
        _save(db, channel, {}, ok=False, error='phone_offline', requested_at=started + timedelta(seconds=2))
        assert _save(db, channel, _result(summary='晚到的旧内容', html=None, html_cached=False), requested_at=started - timedelta(seconds=1)) is None
    notice = build_chatroom_profile(channel, room, [])['notice']
    assert notice['body'] == '第一段\n第二段\n下一行'
    assert notice['last_error'] == 'phone_offline' and notice['fetched_at'] == before


def test_notice_rejects_malformed_success_and_cross_tenant_or_wrong_room(notice_db):
    db, channel, room, other = notice_db
    previous = room.extra_json
    with tenant_scope(1):
        assert _save(db, channel, _result(room_id='another@chatroom'))
        assert room.extra_json == previous
        assert _save(db, channel, {})
        assert build_chatroom_profile(channel, room, [])['notice']['last_error']
    with tenant_scope(2):
        assert _save(db, channel, _result())
    assert other.extra_json is None


def test_other_channel_profiles_report_unconnected_notice_and_pinned_capabilities(notice_db):
    _, channel, room, _ = notice_db
    channel.channel_type = 'wechat_gugatool'
    profile = build_chatroom_profile(channel, room, [])
    assert profile['notice']['supported'] is False
    assert profile['pinned']['supported'] is False


def test_notice_utf8_limit_preserves_previous_body_and_malformed_declarations_are_safe(notice_db):
    """公告按字节校验容量；HTMLParser 可容忍的畸形声明仍应安全提取可读正文。"""
    db, channel, room, _ = notice_db
    with tenant_scope(1):
        assert _save(db, channel, _result()) is None
        assert _save(db, channel, _result(html='<p>' + '公告' * 12_000 + '</p>', summary=None))
        assert build_chatroom_profile(channel, room, [])['notice']['body'] == '第一段\n第二段\n下一行'
        assert len(room.extra_json.encode('utf-8')) < 60_000
        assert _save(db, channel, _result(html='<![invalid]><p>可读正文</p>')) is None
    assert build_chatroom_profile(channel, room, [])['notice']['body'] == '可读正文'
