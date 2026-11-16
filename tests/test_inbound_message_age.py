"""十分钟前的消息保留存档，在入队、消费和 AI 下发边界停止自动执行。"""

from copy import deepcopy
from datetime import timedelta, timezone
import json

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Message, RawWebhookEvent, TaskJob
from app.services import ai_dispatch, ai_intent_worker, async_workers, channel_events, plugin_directory
from app.services.task_queue import PLUGIN_DISPATCH_JOB
from tests.test_ai_dispatch import EVENT
from tests.test_webridge_member_directory import env  # noqa: F401

NOW = now_shanghai().replace(hour=12, minute=0, second=0, microsecond=0)


def _event(age_seconds):
    """只构造微桥当前协议字段，原始发送时间由正式适配器归一化。"""

    return {'event_id': f'message-age-{age_seconds}', 'event_name': 'message', 'body': {
        'room_id': 'directory@chatroom', 'sender_id': 'alice', 'content': '/签到',
        'create_time': int((NOW - timedelta(seconds=age_seconds)).timestamp())}}


@pytest.mark.parametrize('age,queued', [(599, True), (600, True), (601, False), (3600, False)])
def test_old_message_is_stored_without_command_job(env, monkeypatch, age, queued):
    """时间门槛只改变指令入队，原始事件、消息正文和去重均照常落库。"""

    db, channel, _room, _member = env
    monkeypatch.setattr(channel_events, 'now_shanghai', lambda: NOW)
    result = channel_events.ingest_inbound_event(db, channel, _event(age))
    assert result['mapped'] is True
    assert result['dispatch']['queued'] is queued
    assert len(db.scalars(select(TaskJob).where(TaskJob.job_type == PLUGIN_DISPATCH_JOB)).all()) == int(queued)
    raw = db.get(RawWebhookEvent, result['raw_event_id'])
    assert raw.status == 'mapped' and json.loads(raw.payload_json)['body']['create_time'] == _event(age)['body']['create_time']
    message = db.get(Message, result['dispatch']['message_id'])
    assert message.content == '/签到' and message.status == 'received'
    if not queued:
        summary = json.loads(raw.dispatch_summary_json)
        assert summary['reason'] == 'stale_message' and summary['age_seconds'] == age
        assert summary['max_age_seconds'] == 600
        duplicate = channel_events.ingest_inbound_event(db, channel, _event(age))
        assert duplicate['deduplicated'] is True


@pytest.mark.parametrize('format_name', ['seconds', 'milliseconds', 'numeric_text', 'iso', 'utc', 'datetime'])
def test_age_policy_uses_original_time_in_all_supported_formats(format_name):
    """同一条旧消息的秒、毫秒及带时区时间不能因表示格式不同绕过门槛。"""

    from app.services.inbound_policy import expired_message_dispatch

    original = NOW - timedelta(seconds=601)
    value = {'seconds': int(original.timestamp()), 'milliseconds': int(original.timestamp() * 1000),
        'numeric_text': str(int(original.timestamp())), 'iso': original.isoformat(),
        'utc': original.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'), 'datetime': original}[format_name]
    summary = expired_message_dispatch({'details': {'timestamp': value}}, now=NOW)
    assert summary['reason'] == 'stale_message' and summary['age_seconds'] == 601


@pytest.mark.parametrize('value', [None, '', 0, '0', False, 'not-a-time', float('inf'), 10 ** 100])
def test_missing_or_invalid_time_is_not_assumed_to_be_an_old_message(value):
    """没有有效发送时间时沿用即时处理，禁止把缺省零值当成 1970 年。"""

    from app.services.inbound_policy import expired_message_dispatch

    assert expired_message_dispatch({'details': {'timestamp': value}}, now=NOW) is None
    assert expired_message_dispatch({}, now=NOW) is None


@pytest.mark.parametrize('age,expired', [(-30, False), (599.9, False), (600, False), (600.1, True)])
def test_subsecond_boundary_and_future_clock_do_not_expire_valid_messages(age, expired):
    """保留小数秒精度，未来时间和恰好十分钟不应因时钟表示方式被误拦。"""

    from app.services.inbound_policy import expired_message_dispatch

    event = {'details': {'timestamp': (NOW - timedelta(seconds=age)).timestamp()}}
    assert (expired_message_dispatch(event, now=NOW) is not None) is expired


@pytest.mark.parametrize('source', ['wechat_webridge', 'wechat_ipad', 'workwechat',
    'wechat_winauto', 'wechat_gugatool', 'telegram', 'workwechat_provider'])
def test_current_adapters_preserve_original_message_time(source):
    """各现行渠道的原始时间都必须进入统一字段，不能被当前接收时刻替代。"""

    from app.models.entities import Channel
    from app.services.channel_adapters import get_channel_adapter
    from app.services.inbound_policy import expired_message_dispatch

    timestamp = int((NOW - timedelta(minutes=11)).timestamp())
    channel_type = 'workwechat' if source == 'workwechat_provider' else source
    channel = Channel(id=1, tenant_id=1, bot_id=1, name='时间映射', channel_type=channel_type,
        adapter_config_json='{"resolve_names": false}')
    payloads = {
        'wechat_webridge': {'event_name': 'message', 'body': {'chat_id': 'peer', 'sender_id': 'alice', 'content': '/签到', 'create_time': timestamp}},
        'wechat_ipad': {'MsgId': 'm1', 'FromUserName': 'alice', 'ToUserName': 'bot', 'Content': '/签到', 'MsgType': 1, 'CreateTime': timestamp},
        'workwechat': {'type': 11041, 'data': {'sender': 'alice', 'content': '/签到', 'send_time': timestamp}},
        'wechat_winauto': {'conversation_id': 'peer', 'sender_id': 'alice', 'content': '/签到', 'timestamp': timestamp},
        'wechat_gugatool': {'kind': 'incoming', 'fromUser': 'alice', 'toUser': 'bot', 'content': '/签到', 'messageType': 1, 'createTime': timestamp},
        'telegram': {'update_id': 1, 'message': {'message_id': 1, 'chat': {'id': 10, 'type': 'private'}, 'from': {'id': 20}, 'text': '/签到', 'date': timestamp}},
        'workwechat_provider': {'conversation': {'external_id': 'peer', 'chat_type': 'direct'},
            'message': {'sender_id': 'alice', 'content': '/签到', 'message_type': 'text'}, 'details': {'timestamp': timestamp}},
    }
    normalized = get_channel_adapter(channel).normalize_inbound_payload(channel, payloads[source])
    assert normalized['details']['timestamp'] == timestamp
    assert expired_message_dispatch(normalized, now=NOW)['reason'] == 'stale_message'


def test_worker_stops_a_message_that_expired_while_queued(env, monkeypatch):
    """消息入队时新鲜，但队列堵塞超过十分钟后不能再进入群功能或插件。"""

    db, channel, _room, _member = env
    monkeypatch.setattr(channel_events, 'now_shanghai', lambda: NOW)
    incoming = channel_events.ingest_inbound_event(db, channel, _event(10))
    job = db.scalar(select(TaskJob).where(TaskJob.job_type == PLUGIN_DISPATCH_JOB))
    monkeypatch.setattr(async_workers, 'now_shanghai', lambda: NOW + timedelta(minutes=11))
    called = []
    monkeypatch.setattr(async_workers, 'dispatch_group_features', lambda *a, **k: called.append('features') or [])
    monkeypatch.setattr(async_workers, 'build_dispatch_report', lambda *a, **k: pytest.fail('过期消息不能执行插件'))
    result = async_workers._process_plugin_dispatch_job(db, json.loads(job.payload_json))
    assert called == [] and result['executed_plugins'] == [] and result['executed_group_features'] == []
    assert result['dispatch']['reason'] == 'stale_message'
    assert json.loads(db.get(RawWebhookEvent, incoming['raw_event_id']).dispatch_summary_json)['reason'] == 'stale_message'
    assert db.get(Message, incoming['dispatch']['message_id']).content == '/签到'


def _expired_payload(channel):
    event = deepcopy(EVENT)
    event.setdefault('details', {})['timestamp'] = (now_shanghai() - timedelta(minutes=11)).timestamp()
    return {'channel_id': channel.id, 'normalized_event': event, 'raw_event_id': None,
        'conversation_id': None, 'message_id': None}


def test_stale_message_never_creates_an_ai_intent_job(env):
    """AI 独立入队入口共用时效规则，不能让过期原文通过 AI 改写再次执行。"""

    db, channel, _room, _member = env
    payload = _expired_payload(channel)
    plan = plugin_directory.build_static_dispatch_plan(db, channel, payload['normalized_event'])
    assert plugin_directory._enqueue_ai_intent(db, plan, payload['normalized_event'],
        raw_event_id=None, conversation_id=None, message_id=None) is False
    assert db.scalar(select(TaskJob).where(TaskJob.job_type == 'ai_intent')) is None
    # 只改变发送时间，当前消息必须仍能进入同一 AI 流程，排除配置未启用造成的假通过。
    fresh = deepcopy(payload['normalized_event'])
    fresh['details']['timestamp'] = now_shanghai().timestamp()
    assert plugin_directory._enqueue_ai_intent(db, plan, fresh,
        raw_event_id=1, conversation_id=None, message_id=None) is True


def test_stale_ai_job_is_skipped_before_preparing_model_context(env, monkeypatch):
    """已有 AI 任务过期后不准备模型上下文，也不消耗调用额度。"""

    db, channel, _room, _member = env
    monkeypatch.setattr(plugin_directory, 'build_static_dispatch_plan', lambda *a, **k: pytest.fail('过期消息不应准备模型上下文'))
    assert ai_dispatch.prepare_intent(db, _expired_payload(channel)) is None


@pytest.mark.parametrize('status', ['routed', 'asked', 'chatted'])
def test_ai_result_is_not_delivered_after_source_message_expires(env, monkeypatch, status):
    """模型请求期间跨过门槛时，改写指令、追问和聊天回复都停止下发。"""

    db, channel, _room, _member = env
    calls = []
    monkeypatch.setattr(ai_dispatch, 'record_decision', lambda *a, **k: calls.append('decision'))
    monkeypatch.setattr(plugin_directory, 'send_ai_reply', lambda *a, **k: calls.append('reply'))
    monkeypatch.setattr(ai_intent_worker, '_execute_routed', lambda *a, **k: calls.append('plugin'))
    intent = ai_dispatch.AiIntent(status=status, command_text='/签到', question='请确认')
    result = ai_intent_worker._deliver(db, _expired_payload(channel), intent, chat_reply='你好')
    assert calls == [] and result['status'] == 'skipped' and result['reason'] == 'stale_message'
