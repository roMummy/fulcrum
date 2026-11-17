"""插件卡片统计的真实数据库验证：口径、时间桶、隔离与批量查询。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Plugin, PluginActivityHourly, PluginExecutionLog
from app.services.analytics import record_plugin_dispatch_analytics, record_plugin_execution
from app.services.plugin_usage import build_plugin_usage
from app.services.plugin_directory import sync_plugins_from_directory
from test_provider_node_pages import provider_page_context  # noqa: F401


@pytest.fixture()
def usage_context(provider_page_context):
    """与正式页面共用租户和认证夹具，所有计数只写入临时 SQLite。"""

    client, ids = provider_page_context
    with ids['factory']() as db:
        sync_plugins_from_directory(db)
        plugin = db.scalar(select(Plugin).where(Plugin.code == 'weather'))
        binding = BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=plugin.id, status='enabled')
        db.add(binding)
        db.commit()
        ids.update(plugin=int(plugin.id), binding=int(binding.id))
    return client, ids


def _log(ids, **values):
    """只构造可信资源引用，不将测试正文或配置混入统计记录。"""

    return PluginExecutionLog(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'],
        plugin_id=ids['plugin'], bot_plugin_id=ids['binding'], plugin_code='weather', **values)


@pytest.mark.parametrize('matched,attempted,ok,status,expected', [
    (False, False, False, 'skipped', (0, 0, 0)),
    (True, True, True, 'completed', (1, 1, 0)),
    (True, True, False, 'error', (1, 0, 1)),
    (True, True, False, 'timeout', (1, 0, 1)),
    (True, True, False, 'outbound_failed', (1, 0, 1)),
    (True, True, False, 'business_failure', (1, 0, 1)),
    (True, True, False, 'unsupported', (1, 0, 1)),
    (True, False, False, 'blocked', (1, 0, 0)),
    (True, True, True, 'skipped', (1, 0, 0)),
])
def test_outcomes_are_mutually_exclusive(usage_context, matched, attempted, ok, status, expected):
    """跳过不假装正常，也不把普通未命中和 consume_policy 拦截视为异常。"""

    _, ids = usage_context
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        record_plugin_execution(db, _log(ids, matched=matched, attempted=attempted, ok=ok, status=status))
        db.commit()
        data = build_plugin_usage(db, ['weather'])['plugins']['weather']
        assert tuple(data['totals'][name] for name in ('matched', 'success', 'error')) == expected
        assert db.scalar(select(PluginExecutionLog)) is not None


def test_many_events_accumulate_and_each_timeout_is_counted_once(usage_context):
    """连续与原子增量共用计数路径，超时不重复计入普通失败。"""

    _, ids = usage_context
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        for status in ['completed', 'completed', 'timeout', 'error']:
            record_plugin_execution(db, _log(ids, matched=True, attempted=True, ok=status == 'completed', status=status))
        db.commit()
        data = build_plugin_usage(db, ['weather'])['plugins']['weather']
        assert data['totals']['matched'] == 4
        assert data['totals']['success'] == data['totals']['error'] == 2
        row = db.scalar(select(PluginActivityHourly))
        assert row.timeout_count == row.failed_count == 1
        assert data['last_called_at']


def test_stale_session_cannot_overwrite_another_execution_count(usage_context):
    """显式制造旧 ORM 缓存：另一会话提交后，当前会话仍必须原子增加次数。"""

    _, ids = usage_context
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as first:
        record_plugin_execution(first, _log(ids, matched=True, attempted=True, ok=True, status='completed'))
        first.commit()
        cached = first.scalar(select(PluginActivityHourly))
        assert cached.success_count == 1
        with tenant_session(ids['factory']) as second:
            record_plugin_execution(second, _log(ids, matched=True, attempted=True, ok=True, status='completed'))
            second.commit()
        assert cached.success_count == 1
        record_plugin_execution(first, _log(ids, matched=True, attempted=True, ok=True, status='completed'))
        first.commit()
        assert build_plugin_usage(first, ['weather'])['plugins']['weather']['totals']['success'] == 3


@pytest.mark.parametrize('period,length', [('24h', 24), ('7d', 7), ('30d', 30)])
def test_time_windows_zero_fill_and_exclude_future_and_old_rows(usage_context, period, length):
    """跨午夜／日期聚合仍使用业务时区，边界外的数据不能混入当前时间窗。"""

    _, ids = usage_context
    now = now_shanghai().replace(hour=0, minute=30, second=0, microsecond=0)
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        for delta, count in [(timedelta(hours=-1), 3), (timedelta(days=-40), 90), (timedelta(hours=2), 80)]:
            db.add(PluginActivityHourly(tenant_id=ids['alpha'], plugin_code='weather', bucket_start=(now + delta).replace(minute=0), matched_count=count, success_count=count))
        db.commit()
        data = build_plugin_usage(db, ['weather', 'empty'], period=period, now=now)
        assert len(data['buckets']) == length
        assert data['plugins']['weather']['totals']['matched'] == 3
        assert data['plugins']['weather']['series'][-2]['matched'] == 3
        assert data['plugins']['weather']['series'][-1]['matched'] == 0
        assert all(point['matched'] == 0 for point in data['plugins']['empty']['series'])
        assert data['plugins']['empty']['last_called_at'] is None


def test_tenant_scope_and_platform_scope_have_different_totals(usage_context):
    """平台可汇总两个企业，企业查询不能看到其他企业的同名插件。"""

    _, ids = usage_context
    with ids['factory']() as db:
        for tenant, count in [(ids['alpha'], 2), (ids['beta'], 11)]:
            db.add(PluginActivityHourly(tenant_id=tenant, bucket_start=now_shanghai().replace(minute=0, second=0, microsecond=0), plugin_code='weather', matched_count=count))
        db.commit()
        assert build_plugin_usage(db, ['weather'])['plugins']['weather']['totals']['matched'] == 13
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        assert build_plugin_usage(db, ['weather'])['plugins']['weather']['totals']['matched'] == 2


def test_query_count_is_constant_when_plugin_count_grows(usage_context):
    """所有卡片共享聚合查询，增加卡片不能增加数据库往返次数。"""

    _, ids = usage_context
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        statements = []
        connection = db.connection()
        callback = lambda *args: statements.append(args[2])
        event.listen(connection, 'before_cursor_execute', callback)
        try:
            build_plugin_usage(db, ['weather'])
            count = len(statements)
            statements.clear()
            build_plugin_usage(db, ['weather', *[f'plugin-{i}' for i in range(50)]], period='30d')
            assert len(statements) == count == 2
        finally:
            event.remove(connection, 'before_cursor_execute', callback)


@pytest.mark.parametrize('period', ['24h', '7d', '30d'])
def test_last_activity_is_independent_of_chart_period_and_ignores_scans(usage_context, period):
    """最近活动包含实际命中，未命中扫描和未来日志不能把插件推到前面。"""

    _, ids = usage_context
    now = now_shanghai()
    called = now - timedelta(days=3)
    matched = now - timedelta(days=2)
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        db.add_all([
            _log(ids, matched=True, attempted=True, ok=True, status='completed', created_at=called),
            _log(ids, matched=True, attempted=False, ok=False, status='blocked', created_at=matched),
            _log(ids, matched=False, attempted=False, ok=False, status='skipped', created_at=now),
            _log(ids, matched=True, attempted=True, ok=True, status='completed', created_at=now + timedelta(days=1)),
        ])
        db.commit()
        data = build_plugin_usage(db, ['weather', 'empty'], period=period, now=now)['plugins']
        assert data['weather']['last_activity_at'] == matched.isoformat()
        assert data['empty']['last_activity_at'] is None
        assert data['weather']['last_called_at'] == (None if period == '24h' else called.isoformat())


def test_last_activity_respects_tenant_scope(usage_context):
    """其他企业同名插件的新活动不能改变本企业的卡片顺序。"""

    _, ids = usage_context
    now = now_shanghai()
    older = now - timedelta(hours=2)
    with ids['factory']() as db:
        db.add_all([_log(ids, matched=True, attempted=True, created_at=older),
            PluginExecutionLog(tenant_id=ids['beta'], plugin_code='weather', matched=True, attempted=True, created_at=now)])
        db.commit()
        assert build_plugin_usage(db, ['weather'], now=now)['plugins']['weather']['last_activity_at'] == now.isoformat()
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        item = build_plugin_usage(db, ['weather'], now=now)['plugins']['weather']
        assert item['last_activity_at'] == older.isoformat()


def test_dispatch_adapter_records_one_execution_with_ai_source(usage_context):
    """AI 改写沿用分发结果记录，底层原生桥接不再次增加计数。"""

    _, ids = usage_context
    candidate = SimpleNamespace(plugin_code='weather', plugin_id=ids['plugin'], binding_id=ids['binding'], consume_policy='continue')
    report = SimpleNamespace(static_plan=None, decisions=[SimpleNamespace(candidate=candidate, matched=True, debug={})],
        executions=[SimpleNamespace(candidate=candidate, attempted=True, ok=True, status='completed', message='ok', debug={})])
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        from app.models.entities import Channel
        record_plugin_dispatch_analytics(db, channel=db.get(Channel, ids['channel_id']), dispatch_report=report,
            raw_event_id=None, conversation_id=None, message_id=None, trigger_source='ai_dispatch')
        db.commit()
        rows = db.scalars(select(PluginExecutionLog)).all()
        assert len(rows) == 1 and rows[0].trigger_source == 'ai_dispatch'
        assert build_plugin_usage(db, ['weather'])['plugins']['weather']['totals']['success'] == 1


def test_unsupported_period_is_rejected(usage_context):
    _, ids = usage_context
    with ids['factory']() as db, pytest.raises(ValueError, match='时间范围'):
        build_plugin_usage(db, ['weather'], period='365d')


@pytest.mark.parametrize('outcome', ['success', 'failure', 'exception'])
def test_manual_execution_records_result_and_exception_after_rollback(usage_context, monkeypatch, outcome):
    """请求成功和抛错均能留下真实记录，失败业务事务不随统计提交。"""

    import app.main as main
    from app.services.plugin_runtime.contracts import PluginExecutionResult
    from test_provider_node_pages import _login

    client, ids = usage_context
    token = _login(client, 'operator')
    def execute(*args, **kwargs):
        if outcome == 'exception':
            raise RuntimeError('测试中的执行异常')
        return PluginExecutionResult(ok=outcome == 'success', status='completed' if outcome == 'success' else 'error', message='定向验收')
    monkeypatch.setattr(main, 'execute_binding_message_action', execute)
    if outcome == 'exception':
        with pytest.raises(RuntimeError, match='测试中的执行异常'):
            client.post(f"/plugins/{ids['binding']}/execute", json={}, headers={'X-CSRF-Token': token})
    else:
        assert client.post(f"/plugins/{ids['binding']}/execute", json={}, headers={'X-CSRF-Token': token}).status_code == 200
    with ids['factory']() as db:
        rows = db.scalars(select(PluginExecutionLog)).all()
        assert len(rows) == 1 and rows[0].trigger_source == 'manual'
        assert rows[0].matched and rows[0].attempted
        assert rows[0].ok == (outcome == 'success')


@pytest.mark.parametrize('outcome', ['success', 'failure', 'exception'])
def test_scheduled_execution_is_recorded_and_retries_are_real_attempts(usage_context, monkeypatch, outcome):
    """走正式定时任务处理和 fail_job 提交，异常统计不因任务失败而丢失。"""

    from app.services.plugin_runtime import scheduler
    from app.services.plugin_runtime.contracts import PluginExecutionResult
    from app.services.task_queue import fail_job

    _, ids = usage_context
    def execute(*args, **kwargs):
        if outcome == 'exception':
            raise TimeoutError('测试中的定时超时')
        return PluginExecutionResult(ok=outcome == 'success', status='completed' if outcome == 'success' else 'error', message='定向验收')
    monkeypatch.setattr(scheduler, 'execute_binding_scheduled_action', execute)
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        binding = db.get(BotPlugin, ids['binding'])
        job, _ = scheduler.enqueue_plugin_scheduled_action(db, binding, action_key='scheduled_tick', scheduled_for=now_shanghai())
        db.commit()
        payload = {'binding_id': ids['binding'], 'action_key': 'scheduled_tick', 'parameters': {}}
        if outcome == 'success':
            scheduler.process_plugin_scheduled_action_job(db, job, payload)
            scheduler.process_plugin_scheduled_action_job(db, job, payload)
            db.commit()
        else:
            with pytest.raises((TimeoutError, scheduler.PluginSchedulerError)):
                scheduler.process_plugin_scheduled_action_job(db, job, payload)
            fail_job(db, job, error_message='定向验收失败')
        rows = db.scalars(select(PluginExecutionLog)).all()
        assert len(rows) == (2 if outcome == 'success' else 1)
        assert all(row.trigger_source == 'scheduled' and row.attempted for row in rows)
        assert rows[0].ok == (outcome == 'success')
        if outcome == 'exception':
            assert rows[0].status == 'timeout'


def test_ai_routed_boundary_writes_analytics_once(usage_context, monkeypatch):
    """直接经过 AI 工作线程的正式重分发边界，而不是只测试统计适配器。"""

    from app.models.entities import Channel
    from app.services.ai_intent_worker import _execute_routed
    import app.services.plugin_directory as directory

    _, ids = usage_context
    candidate = SimpleNamespace(plugin_code='weather', plugin_id=ids['plugin'], binding_id=ids['binding'], consume_policy='continue')
    report = SimpleNamespace(static_plan=None, decisions=[SimpleNamespace(candidate=candidate, matched=True, debug={})],
        executions=[SimpleNamespace(candidate=candidate, attempted=True, ok=True, status='completed', message='ok', debug={})])
    monkeypatch.setattr(directory, 'build_dispatch_report', lambda *args, **kwargs: report)
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        _execute_routed(db, db.get(Channel, ids['channel_id']), {}, {'message': {'content': '原话'}}, '/天气 上海')
        db.commit()
        rows = db.scalars(select(PluginExecutionLog)).all()
        assert len(rows) == 1 and rows[0].trigger_source == 'ai_dispatch'


def test_tenant_usage_endpoint_is_authenticated_scoped_and_readonly(usage_context):
    """企业 API 不能通过查询参数切换租户，也不能泄露平台专属插件编码。"""

    from test_provider_node_pages import _login
    client, ids = usage_context
    assert client.get('/plugins/usage').status_code in (303, 401)
    with ids['factory']() as db:
        hidden = Plugin(code='private-usage-test', display_name='私有统计测试', visibility='platform_only')
        db.add(hidden)
        for tenant, value in [(ids['alpha'], 2), (ids['beta'], 17)]:
            db.add(PluginActivityHourly(tenant_id=tenant, plugin_code='weather', bucket_start=now_shanghai().replace(minute=0, second=0, microsecond=0), matched_count=value))
        db.commit()
    _login(client, 'readonly')
    response = client.get(f"/plugins/usage?period=7d&tenant_id={ids['beta']}")
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['plugins']['weather']['totals']['matched'] == 2
    assert 'private-usage-test' not in response.json()['plugins']
    assert client.get('/plugins/usage?period=wrong').status_code == 400
    # 企业身份即使知道平台接口地址，也不能查看平台汇总。
    assert client.get('/platform/plugins/usage').status_code in (303, 401, 403)


@pytest.fixture()
def usage_platform_client(usage_context, monkeypatch):
    """平台 API 使用真实平台会话，最小应用只省略不相关的企业中间件。"""

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from app.models.saas import PlatformUser
    from app.services.saas_auth import create_platform_session, hash_password, load_platform_principal
    from app.web.auth_common import PLATFORM_SESSION_COOKIE
    import app.web.platform_plugins as platform

    _, ids = usage_context
    with ids['factory']() as db:
        user = PlatformUser(username='usage-platform', display_name='统计管理员', password_hash=hash_password('Usage-platform-123'),
            role='platform_admin', is_enabled=True, must_change_password=False, auth_version=1)
        db.add(user)
        db.commit()
        session = create_platform_session(db, user)
    monkeypatch.setattr(platform, 'SessionLocal', ids['factory'])
    app = FastAPI()
    @app.middleware('http')
    async def principal(request: Request, call_next):
        with ids['factory']() as db:
            request.state.platform_principal = load_platform_principal(db, request.cookies.get(PLATFORM_SESSION_COOKIE))
        return await call_next(request)
    app.include_router(platform.router)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get('/platform/plugins/usage').status_code == 401
        client.cookies.set(PLATFORM_SESSION_COOKIE, session.raw_token)
        yield client


def test_platform_usage_endpoint_aggregates_and_uses_same_card_markup(usage_context, usage_platform_client):
    """平台聚合保留租户边界的明确入口；卡片模板直接编译两类共用统计区。"""

    _, ids = usage_context
    with ids['factory']() as db:
        for tenant, value in [(ids['alpha'], 3), (ids['beta'], 13)]:
            db.add(PluginActivityHourly(tenant_id=tenant, plugin_code='weather', bucket_start=now_shanghai().replace(minute=0, second=0, microsecond=0), matched_count=value))
        db.commit()
    response = usage_platform_client.get('/platform/plugins/usage?period=30d')
    assert response.status_code == 200
    assert response.json()['plugins']['weather']['totals']['matched'] == 16
    page = usage_platform_client.get('/platform/plugins')
    assert page.status_code == 200 and 'data-usage-plugin="weather"' in page.text
    assert '/static/js/plugin-usage.js' in page.text
    assert usage_platform_client.get('/platform/plugins/usage?period=bad').status_code == 400
