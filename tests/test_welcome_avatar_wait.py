"""复现新人资料比入群通知晚返回的时序，只使用内存数据库和正式后台任务入口。"""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.time import now_shanghai
from app.models.entities import ChatroomMember, RawWebhookEvent, TaskJob, WorkWechatContact
from app.services import async_workers, task_queue
from app.services.group_features import member_change
from app.services.group_features.store import save_state
from app.services.wechat_webridge import member_directory, service
from tests.test_group_features import _join_event, env, sent  # noqa: F401

AVATAR = 'https://wx.qlogo.cn/mmhead/new-member/132'


@pytest.fixture()
def case(env, sent, monkeypatch):
    """固定队列时钟并隔离插件统计，保留真实群功能、资料保存及任务领取流程。"""

    db = env.db
    RawWebhookEvent.__table__.create(db.bind)
    factory = sessionmaker(bind=db.bind, autoflush=False, expire_on_commit=False)
    clock = [now_shanghai().replace(microsecond=0)]
    for module in [async_workers, task_queue, member_change]:
        monkeypatch.setattr(module, 'now_shanghai', lambda: clock[0])
    monkeypatch.setattr(async_workers, 'SessionLocal', factory)
    monkeypatch.setattr(async_workers, 'TenantSessionLocal', factory)
    monkeypatch.setattr(async_workers, 'build_dispatch_report', lambda *a, **k: SimpleNamespace(executions=[], executed_plugin_codes=[]))
    monkeypatch.setattr(async_workers, 'serialize_dispatch_report', lambda report: {'status': 'completed'})
    monkeypatch.setattr(async_workers, 'record_plugin_dispatch_analytics', lambda *a, **k: None)
    monkeypatch.setattr(member_directory.wakeup, 'notify', lambda *a, **k: None)
    config = {'card_enabled': True, 'card_thumb_source': 'member_avatar', 'card_url': 'https://example.test/rules'}
    save_state(env.room, 'member_change', enabled=True, config=config)
    event = _join_event()
    event['event_key'] = 'welcome-avatar-timing'
    event['details']['timestamp'] = clock[0].timestamp()
    member_directory.apply_event(db, env.channel, event, env.conversation)
    raw = RawWebhookEvent(tenant_id=1, channel_id=env.channel.id, source='wechat_webridge',
        event_key=event['event_key'], payload_json=json.dumps(event), status='mapped')
    db.add(raw)
    db.flush()
    payload = {'channel_id': env.channel.id, 'conversation_id': env.conversation.id,
        'raw_event_id': raw.id, 'message_id': None, 'normalized_event': event}
    job = task_queue.enqueue_job(db, job_type=task_queue.PLUGIN_DISPATCH_JOB, payload=payload, available_at=clock[0])
    job.created_at = clock[0]
    db.commit()

    def refresh():
        db.expire_all()
        return db.get(TaskJob, job.id)

    def snapshot(members=None, *, complete=True):
        # 走正式成员结果保存入口，证明新头像确实能被原有卡片生成链读取。
        result = {'room_id': env.room.external_id, 'members': members or [{'account_id': 'wxid_a', 'avatar_url': AVATAR}],
            'has_more': not complete}
        counts = service.persist_directory_result(db, env.channel,
            {'type': 'room_members', 'params': {'room_id': env.room.external_id}}, result)
        member_directory.after_result(db, env.channel, env.room.external_id, complete=bool(counts['members_complete']))
        db.commit()

    return SimpleNamespace(db=db, env=env, sent=sent, clock=clock, config=config, event=event,
        payload=payload, job=job, raw=raw, refresh=refresh, snapshot=snapshot)


def test_new_member_welcome_waits_then_uses_synced_avatar(case):
    """入群先到、头像两秒后返回：先让出任务，随后只发送一次带新人头像的欢迎卡片。"""

    assert async_workers._run_single_job() is True
    assert case.sent == [], '资料同步完成前不能抢先生成无头像卡片'
    job = case.refresh()
    assert job.status == 'pending' and job.attempts == 0
    assert job.started_at is None and job.locked_at is None
    assert json.loads(job.result_json)['reason'] == 'member_avatar_sync'
    case.snapshot()
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True
    job = case.refresh()
    assert job.status == 'completed' and job.attempts == 1
    assert len(case.sent) == 1
    assert case.sent[0]['replies'][1]['thumb_url'] == AVATAR
    assert len(case.db.scalars(select(TaskJob).where(TaskJob.job_type == task_queue.PLUGIN_DISPATCH_JOB)).all()) == 1
    assert async_workers._run_single_job() is False


@pytest.mark.parametrize('scenario', [
    'custom_url', 'card_disabled', 'join_disabled', 'feature_disabled', 'group_disabled',
    'other_channel', 'already_synced', 'member_avatar', 'friend_avatar', 'ignored_event',
    'self_sent', 'explicit_skip', 'old_event', 'leave', 'ordinary_message',
])
def test_only_pending_avatar_welcomes_need_to_wait(case, scenario):
    """等待只针对真实需要头像的当前入群通知，其他开关、时效和消息沿用原执行路径。"""

    env = case.env
    if scenario == 'custom_url':
        case.config.update(card_thumb_source='url', card_thumb_url='https://example.test/cover.jpg')
    elif scenario == 'card_disabled':
        case.config['card_enabled'] = False
    elif scenario == 'join_disabled':
        case.config['notify_join'] = False
    save_state(env.room, 'member_change', enabled=scenario != 'feature_disabled', config=case.config)
    if scenario == 'group_disabled':
        env.room.is_active = False
    elif scenario == 'other_channel':
        env.channel.channel_type = 'wechat_ipad'
    elif scenario == 'already_synced':
        member_directory.save_state(env.room, dirty=False)
    elif scenario == 'member_avatar':
        member = case.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == 'wxid_a'))
        member.extra_json = json.dumps({**json.loads(member.extra_json), 'avatar_url': AVATAR})
    elif scenario == 'friend_avatar':
        case.db.add(WorkWechatContact(tenant_id=1, channel_id=env.channel.id, contact_external_id='wxid_a', avatar_url=AVATAR))
    elif scenario == 'ignored_event':
        case.event['details']['member_directory']['applied'] = False
    elif scenario == 'self_sent':
        case.event['details']['from_self'] = True
    elif scenario == 'explicit_skip':
        case.event['details']['skip_plugin_dispatch'] = True
    elif scenario == 'old_event':
        case.event['details']['timestamp'] = (case.clock[0] - timedelta(minutes=11)).timestamp()
    elif scenario == 'leave':
        case.event['details']['member_change']['change_type'] = 'left'
    elif scenario == 'ordinary_message':
        case.event['message'] = {'message_type': 'text', 'content': '你好'}
        case.event['details'].pop('member_change')
    case.db.commit()
    assert member_change.welcome_avatar_retry_at(case.db, case.payload, queued_at=case.job.created_at, now=case.clock[0]) is None


def test_fixed_custom_url_does_not_load_or_wait_for_member_avatar(case, monkeypatch):
    """固定 URL 仍立即发送，不因为群目录未返回而多查身份或延迟。"""

    case.config.update(card_thumb_source='url', card_thumb_url='https://example.test/cover.jpg')
    save_state(case.env.room, 'member_change', enabled=True, config=case.config)
    case.db.commit()
    monkeypatch.setattr(member_change, '_welcome_member_profiles', lambda *a: pytest.fail('固定 URL 不应查询头像'))
    assert async_workers._run_single_job() is True
    assert case.refresh().status == 'completed'
    assert case.sent[0]['replies'][1]['thumb_url'] == 'https://example.test/cover.jpg'


def test_avatar_template_in_url_mode_uses_the_same_wait_rule(case):
    """既有头像变量与来源下拉共用等待规则，不让 URL 模式变量抢先变成空串。"""

    case.config.update(card_thumb_source='url', card_thumb_url='{avatar_url}')
    save_state(case.env.room, 'member_change', enabled=True, config=case.config)
    case.db.commit()
    assert async_workers._run_single_job() is True and case.sent == []
    case.snapshot()
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True
    assert case.sent[0]['replies'][1]['thumb_url'] == AVATAR


def test_waiting_welcome_yields_to_other_messages(case):
    """同一欢迎任务延后时，后面的普通消息仍可被工作线程正常处理。"""

    assert async_workers._run_single_job() is True
    payload = deepcopy(case.payload)
    payload['raw_event_id'] = None
    payload['normalized_event'] = {'conversation': {'external_id': case.env.room.external_id, 'chat_type': 'group'},
        'message': {'message_type': 'text', 'content': '普通消息'}, 'details': {'timestamp': case.clock[0].timestamp()}}
    other = task_queue.enqueue_job(case.db, job_type=task_queue.PLUGIN_DISPATCH_JOB, payload=payload, priority=80,
        available_at=case.clock[0])
    case.db.commit()
    assert async_workers._run_single_job() is True
    case.db.refresh(other)
    assert other.status == 'completed'
    assert case.refresh().status == 'pending' and case.sent == []


def test_wait_has_a_fixed_deadline_and_does_not_consume_retries(case):
    """手机迟迟不返回时十五秒内让出同一任务，截止后仅发送一次无图卡片。"""

    for _ in range(15):
        assert async_workers._run_single_job() is True
        job = case.refresh()
        assert job.status == 'pending' and job.attempts == 0 and job.error_message is None
        assert case.sent == []
        case.clock[0] += timedelta(seconds=1)
    assert async_workers._run_single_job() is True
    job = case.refresh()
    assert job.status == 'completed' and job.attempts == 1
    assert len(case.sent) == 1 and 'thumb_url' not in case.sent[0]['replies'][1]
    # 迟到的头像不能再次执行已完成任务，避免向群里补发重复通知。
    case.snapshot()
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is False
    assert len(case.sent) == 1


def test_completed_directory_without_avatar_does_not_keep_waiting(case):
    """同步已完成但手机没有头像时沿用缺图规则，不继续等满十五秒。"""

    assert async_workers._run_single_job() is True
    case.snapshot([{'account_id': 'wxid_a', 'avatar_url': ''}])
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True
    assert case.refresh().status == 'completed'
    assert len(case.sent) == 1 and 'thumb_url' not in case.sent[0]['replies'][1]


def test_welcome_disabled_during_wait_is_not_sent(case):
    """用户等待期间关闭群通知时，复读当前开关并直接完成，不再发送旧配置通知。"""

    assert async_workers._run_single_job() is True
    save_state(case.env.room, 'member_change', enabled=False, config=case.config)
    case.db.commit()
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True
    assert case.refresh().status == 'completed' and case.sent == []


def test_message_expiring_during_avatar_wait_still_skips_all_commands(case):
    """等待头像不能突破十分钟消息门槛，跨过时限后原任务只记录跳过。"""

    case.event['details']['timestamp'] = (case.clock[0] - timedelta(seconds=599)).timestamp()
    case.job.payload_json = json.dumps(case.payload)
    case.db.commit()
    assert async_workers._run_single_job() is True and case.sent == []
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True
    job = case.refresh()
    assert job.status == 'completed'
    assert json.loads(job.result_json)['dispatch']['reason'] == 'stale_message'
    assert case.sent == []


def test_multiple_members_wait_for_each_avatar_and_send_once(case):
    """一次多人入群时等缺失的新人，不把第一个已到的头像复用给其他人。"""

    case.event['details']['member_change']['members'].append({'external_id': 'wxid_b', 'display_name': '小红'})
    case.job.payload_json = json.dumps(case.payload)
    case.db.commit()
    assert async_workers._run_single_job() is True
    case.snapshot([{'account_id': 'wxid_a', 'avatar_url': AVATAR}], complete=False)
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True and case.sent == []
    other = 'https://wx.qlogo.cn/mmhead/second-member/132'
    case.snapshot([{'account_id': 'wxid_a', 'avatar_url': AVATAR}, {'account_id': 'wxid_b', 'avatar_url': other}])
    case.clock[0] += timedelta(seconds=2)
    assert async_workers._run_single_job() is True
    assert [card['thumb_url'] for card in case.sent[0]['replies'][1:]] == [AVATAR, other]
    assert len(case.sent) == 1


def test_wait_keeps_the_retry_budget_for_actual_execution_failures(case, monkeypatch):
    """等待资料不是失败；资料就绪后真正的执行错误仍拥有原定重试次数。"""

    for _ in range(4):
        assert async_workers._run_single_job() is True
        case.clock[0] += timedelta(seconds=1)
    case.snapshot()

    def fail_execution(*args, **kwargs):
        raise RuntimeError('test execution failed')

    monkeypatch.setattr(async_workers, '_process_plugin_dispatch_job', fail_execution)
    assert async_workers._run_single_job() is True
    job = case.refresh()
    assert job.status == 'pending' and job.attempts == 1 and job.max_attempts == 3
    assert job.error_message == 'test execution failed'
    assert case.sent == []


def test_member_leaving_while_avatar_syncs_is_not_welcomed_later(case):
    """新人等待头像期间已退群，不能在退群通知之后再发送迟到欢迎。"""

    assert async_workers._run_single_job() is True
    leaving = deepcopy(case.event)
    leaving['event_key'] = 'left-before-avatar'
    leaving['details']['member_change']['change_type'] = 'left'
    leaving['details']['timestamp'] = (case.clock[0] + timedelta(seconds=1)).timestamp()
    member_directory.apply_event(case.db, case.env.channel, leaving, case.env.conversation)
    case.db.commit()
    case.clock[0] += timedelta(seconds=15)
    assert async_workers._run_single_job() is True
    assert case.refresh().status == 'completed'
    assert case.sent == [], '不能再欢迎已经退群的新成员'
