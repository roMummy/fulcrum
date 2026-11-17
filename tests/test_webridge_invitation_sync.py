"""逐成员邀请资料的真实队列链路验证，不连接手机、不发送消息。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import ChatroomMember, TaskJob
from app.services.chatroom_invitations import JOIN_INFO_SOURCE, member_invitation, save_member_invitation
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import invitation_sync as sync, service
from tests.test_webridge_member_directory import IDENTITY, env  # noqa: F401


@pytest.fixture(autouse=True)
def trusted_tenant():
    """复用的目录 fixture 使用普通 Session；邀请证据必须补齐生产路由已有的可信租户上下文。"""
    with tenant_scope(1):
        yield


def start(env, count=1):
    """给测试渠道登记实测能力，以真实群成员主键模拟超过一批的名册。"""
    db, channel, room, _ = env
    channel.runtime_info_json = json.dumps({'webridge': {**IDENTITY, 'capabilities': [sync.COMMAND]}})
    for index in range(count - 1):
        db.add(ChatroomMember(tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
                              chatroom_id=room.id, member_external_id=f'person_{index}', status='active'))
    db.commit()
    result = sync.request_sync(db, channel, room)
    db.commit()
    return result


def jobs(db):
    return list(db.scalars(select(TaskJob).where(TaskJob.related_type == sync.RELATED_TYPE).order_by(TaskJob.id)))


def finish(env, job, **changes):
    """用与实际 8080 相同的 partial 返回触发 service 的唯一命令终态路径。"""
    db, channel, _, _ = env
    command = parse_job_payload(job)['command']
    if job.status == 'pending':
        job.status = 'running'
        db.commit()
    data = {**command['params'], 'status': 'partial', 'inviter_wxid': 'inviter', 'inviter_name': '邀请人',
            'join_time': None, 'join_type': 'unknown', 'join_description': '扫描邀请人分享的二维码进群'}
    data.pop('refresh', None)
    data.update(changes)
    return service.accept_command_result(db, channel, {
        **IDENTITY, 'request_id': command['request_id'], 'ok': True, 'result': data,
    })


def test_partial_result_persists_inviter_without_fabricating_join_time(env):
    db, _, room, member = env
    start(env)
    job = jobs(db)[0]
    ack = finish(env, job)
    assert ack['status'] == 'completed'
    saved = member_invitation(member)
    assert saved['inviter_external_id'] == 'inviter' and saved['method'] == 'unknown'
    assert saved['joined_at'] is None and saved['fetched_at'] and saved['description'].startswith('扫描')
    assert sync.view(room)['status'] == 'completed' and sync.view(room)['known'] == 1
    assert finish(env, job)['deduplicated'] is True
    assert sync.view(room)['completed'] == 1


def test_bounded_batches_continue_and_duplicate_start_reuses_current_run(env):
    db, channel, room, _ = env
    initial = start(env, count=7)
    assert len(jobs(db)) == sync.BATCH_SIZE
    assert sync.request_sync(db, channel, room)['run_id'] == initial['run_id']
    db.commit()
    for index in range(7):
        pending = [job for job in jobs(db) if job.status == 'pending']
        assert 1 <= len(pending) <= sync.BATCH_SIZE
        finish(env, pending[0], inviter_wxid='' if index % 2 else 'inviter')
    state = sync.view(room)
    assert state['status'] == 'completed' and state['completed'] == state['total'] == 7
    assert state['known'] == 4 and state['unknown'] == 3 and state['failed'] == 0


@pytest.mark.parametrize('field,value', [('room_id', 'wrong@chatroom'), ('wxid', 'other-person'), ('inviter_wxid', 'alice')])
def test_wrong_target_and_self_inviter_fail_without_overwriting_evidence(env, field, value):
    db, _, room, member = env
    start(env)
    assert finish(env, jobs(db)[0], **{field: value})['status'] == 'failed'
    assert member_invitation(member) == {}
    assert sync.view(room)['failed'] == 1 and sync.view(room)['status'] == 'completed'


def test_terminal_jobs_are_reconciled_after_interrupted_result_handling(env):
    db, channel, room, _ = env
    start(env, count=5)
    for job in jobs(db):
        job.status = 'failed'
        job.error_message = 'webridge_command_timeout'
    db.commit()
    # 模拟进程重启：丢弃 ORM 缓存，只靠数据库中的群进度与任务终态恢复后续批次。
    db.expire_all()
    sync.resume_channel(db, channel)
    db.commit()
    assert len(jobs(db)) == 5 and len([job for job in jobs(db) if job.status == 'pending']) == 2
    assert sync.view(room)['failed'] == 3
    for job in jobs(db):
        if job.status == 'pending':
            finish(env, job)
    assert sync.view(room)['status'] == 'completed'


def test_timeout_is_settled_by_poll_recovery_and_late_result_is_ignored(env):
    db, channel, room, member = env
    start(env, count=4)
    first = jobs(db)[0]
    first.status, first.locked_at = 'running', now_shanghai() - timedelta(minutes=10)
    db.commit()
    assert service.reap_stale_commands(db, channel) == 1
    sync.resume_channel(db, channel)
    db.commit()
    assert len(jobs(db)) == 4 and sync.view(room)['failed'] == 1
    # 超时后的同一请求仍保持终态；不能用迟到回报补写已经放弃的证据。
    command = parse_job_payload(first)['command']
    ack = service.accept_command_result(db, channel, {**IDENTITY, 'request_id': command['request_id'], 'ok': True,
        'result': {**command['params'], 'inviter_wxid': 'too-late'}})
    assert ack['deduplicated'] and member_invitation(member) == {}


def test_missing_capability_does_not_enqueue(env):
    db, channel, room, _ = env
    with pytest.raises(ValueError, match='当前微桥版本'):
        sync.request_sync(db, channel, room)
    assert jobs(db) == []


def test_empty_or_failed_refresh_keeps_existing_relation_and_time(env):
    db, channel, room, member = env
    start(env)
    first = jobs(db)[0]
    joined = now_shanghai() - timedelta(days=5)
    finish(env, first, join_time=int(joined.timestamp()))
    saved = member_invitation(member)
    assert saved['joined_at'] and saved['inviter_external_id'] == 'inviter'
    sync.request_sync(db, channel, room)
    db.commit()
    finish(env, jobs(db)[-1], inviter_wxid='')
    assert member_invitation(member) == saved
    sync.request_sync(db, channel, room)
    db.commit()
    job = jobs(db)[-1]
    job.status = 'running'
    db.commit()
    command = parse_job_payload(job)['command']
    service.accept_command_result(db, channel, {**IDENTITY, 'request_id': command['request_id'], 'ok': False,
                                                'result': {'error': 'phone_offline'}})
    assert sync.view(room)['failed'] == 1 and member_invitation(member) == saved


def test_standalone_result_rejects_older_evidence_and_other_tenant(env):
    db, channel, room, member = env
    now = now_shanghai()
    assert save_member_invitation(member, 'newest', source=JOIN_INFO_SOURCE, recorded_at=now,
                                  fetched_at=now, method='unknown')
    db.commit()
    job = service.enqueue_command(db, channel, command_name=sync.COMMAND,
                                   args={'room_id': room.external_id, 'wxid': member.member_external_id})
    job.created_at = now - timedelta(days=1)
    db.commit()
    finish(env, job, inviter_wxid='older')
    assert member_invitation(member)['inviter_external_id'] == 'newest'
    with tenant_scope(2):
        with pytest.raises(ValueError, match='未找到当前群'):
            sync.request_sync(db, channel, room)


def test_unknown_snapshot_retains_description_and_explicit_time_without_creating_edge(env):
    db, _, _, member = env
    start(env)
    joined = now_shanghai() - timedelta(days=2)
    # 未知邀请人的资料也能保留明确时间与说明，图谱仍不会凭这些文本产生邀请边。
    finish(env, jobs(db)[0], inviter_wxid='', join_time=joined.isoformat())
    saved = member_invitation(member)
    assert saved['inviter_external_id'] == '' and saved['joined_at'] == joined.isoformat()
    assert saved['description']


def test_nested_result_and_concurrent_terminal_recheck(env, monkeypatch):
    db, channel, room, member = env
    start(env)
    job = jobs(db)[0]
    command = parse_job_payload(job)['command']
    job.status = 'running'
    db.commit()
    original = sync._locked_room

    def settled_while_waiting(*args):
        """模拟另一个回报在群锁等待期间提交，通用入口最初读到的 running 已失效。"""
        job.status = 'completed'
        db.commit()
        return original(*args)

    monkeypatch.setattr(sync, '_locked_room', settled_while_waiting)
    ack = service.accept_command_result(db, channel, {**IDENTITY, 'request_id': command['request_id'], 'ok': True,
        'result': {'ok': True, 'result': {'room_id': room.external_id, 'wxid': member.member_external_id,
                                        'inviter_wxid': 'duplicate', 'status': 'partial'}}})
    assert ack['deduplicated'] and ack['status'] == 'completed' and member_invitation(member) == {}


def test_old_sync_generation_cannot_write_current_member(env):
    db, _, room, member = env
    start(env)
    old = jobs(db)[0]
    extra = json.loads(room.extra_json)
    extra[sync.STATE_KEY]['run_id'] = 'new-generation'
    room.extra_json = json.dumps(extra)
    db.commit()
    assert finish(env, old)['status'] == 'failed' and member_invitation(member) == {}
