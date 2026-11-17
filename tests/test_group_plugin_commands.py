"""本群指令必须真正执行插件业务，且不能改写聊天原文或其他群的配置。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Message
from app.services import plugin_directory
from app.services.plugin_commands import canonical_command_text
from app.services.plugin_runtime import service as runtime_service
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.plugin_runtime.service import execute_binding_message_action
from test_existing_stateful_plugins import _message_sign_request, message_sign_fixture  # noqa: F401


def _configure(db, ids, **override):
    """创建真实群作用域，特意让机器人图片配置与本群相反，以验证执行边界。"""

    channel = db.get(Channel, ids['channel_id'])
    channel.channel_type = 'wechat_webridge'
    binding = db.get(BotPlugin, ids['binding_id'])
    binding.config_json = json.dumps({'fortune_image_enabled': True, 'min_point': 3, 'max_point': 50})
    room = Chatroom(tenant_id=ids['tenant_id'], bot_id=ids['bot_id'], channel_id=channel.id,
                    external_id='sign-room', name='配置测试群', is_active=True, management_status='enabled')
    db.add(room)
    db.flush()
    db.add(GroupPluginScope(tenant_id=ids['tenant_id'], channel_id=channel.id, chatroom_id=room.id,
                           bot_plugin_id=binding.id, enabled=True, scope_status='overridden',
                           config_override_json=json.dumps(override, ensure_ascii=False)))
    db.commit()
    return channel, binding


def _event(content, room='sign-room', mentioned=False, sender='custom-user'):
    return {'conversation': {'external_id': room, 'chat_type': 'group'},
            'message': {'content': content, 'sender_id': sender, 'message_type': 'text'},
            'details': {'mentioned_self': mentioned}}


@pytest.mark.parametrize('content', ['打卡', '/打卡', '#打卡'])
def test_custom_sign_executes_with_group_config_and_preserves_original(message_sign_fixture, monkeypatch, content):
    """匹配、原生执行和真实 SQL 原文必须同时正确，避免只有表单保存成功。"""

    factory, ids = message_sign_fixture
    monkeypatch.setattr(plugin_directory, '_dispatch_auto_outbound_messages', lambda *args: [])
    monkeypatch.setattr(plugin_directory, '_start_channel_typing_indicator', lambda *args: None)
    with tenant_scope(ids['tenant_id']), tenant_session(factory) as db:
        channel, binding = _configure(db, ids, command_overrides={'签到': ['打卡'], '补签': ['补打卡']},
                                      fortune_image_enabled=False, min_point=17, max_point=17)
        request = _message_sign_request(db, ids, content=content, sender_id='custom-user', sender_name='测试用户')
        report = plugin_directory.build_dispatch_report(db, channel, _event(content),
                    conversation_id=ids['conversation_id'], message_id=request.context.message_id, skip_ai_intent=True)
        assert report.matched_plugin_codes == ('message_sign',)
        execution, = report.executions
        assert execution.ok, execution.message
        output = execution.debug['output']
        assert output['points_awarded'] == 17
        assert output['card_status'] == 'disabled'
        assert output['replies'][0]['type'] == 'text'
        db.commit()
        assert db.get(Message, request.context.message_id).content == content
        assert db.execute(text('SELECT content FROM messages WHERE id=:id'), {'id':request.context.message_id}).scalar() == content
        assert json.loads(binding.config_json)['fortune_image_enabled'] is True


def test_group_matcher_uses_mention_and_does_not_keep_replaced_commands(message_sign_fixture):
    factory, ids = message_sign_fixture
    with tenant_scope(ids['tenant_id']), tenant_session(factory) as db:
        channel, _binding = _configure(db, ids, command_overrides={'签到': ['打卡']}, require_mention=True)
        for content, mentioned, expected in [('签到', True, False), ('打卡', False, False), ('打卡', True, True), ('补签', True, True)]:
            event = _event(content, mentioned=mentioned)
            plan = plugin_directory.build_static_dispatch_plan(db, channel, event)
            decisions = plugin_directory.evaluate_dynamic_matches(db, plan, event)
            assert bool(decisions and decisions[0].matched) is expected
        # 另一个群不读取当前群的映射或 @ 要求；默认签到仍有效。
        event = _event('签到', room='other-sign-room')
        plan = plugin_directory.build_static_dispatch_plan(db, channel, event)
        assert plugin_directory.evaluate_dynamic_matches(db, plan, event)[0].matched
        # 机器人全局停用仍优先于群内启用，不能用本群设置绕过全局开关。
        _binding.status = 'disabled'
        db.commit()
        assert not plugin_directory.build_static_dispatch_plan(db, channel, _event('打卡', mentioned=True)).candidates


def test_native_message_entry_reads_current_group_options(message_sign_fixture, monkeypatch):
    """直接验证服务边界，不能依赖目录层把本群配置临时写回机器人绑定。"""

    factory, ids = message_sign_fixture
    module = plugin_directory.load_directory_plugin('message_sign', force=True).instance._module
    monkeypatch.setattr(module, '_render_fortune_card', lambda _view: 'AA==')
    with tenant_scope(ids['tenant_id']), tenant_session(factory) as db:
        _channel, binding = _configure(db, ids, fortune_image_enabled=False, min_point=17, max_point=17)
        request = _message_sign_request(db, ids, content='签到', sender_id='native-user', sender_name='测试用户')
        result = execute_binding_message_action(db, binding, 'execute', context=request.context)
        assert result.ok, result.message
        assert result.output['card_status'] == 'disabled'
        assert result.output['points_awarded'] == 17


@pytest.mark.parametrize(('content', 'expected'), [
    ('查天气 上海\n明天', '天气 上海\n明天'),
    (' /查天气上海', ' /天气上海'),
    ('# 查天气 上海', '# 天气 上海'),
    ('查天气成功', '查天气成功'),
    ('查 上海', '订阅天气 上海'),
])
def test_command_mapping_preserves_prefix_arguments_and_line_breaks(content, expected):
    """最长触发词优先，业务参数和原有排版必须完整交给插件。"""

    config = {'command_overrides':{'天气':['查天气'], '订阅天气':['查']}}
    assert canonical_command_text(content, config) == expected


def test_second_sign_command_dispatches_to_makeup_business(message_sign_fixture, monkeypatch):
    """补签必须独立进入补签业务；无历史记录时返回业务提示，而非无效命令。"""

    factory, ids = message_sign_fixture
    monkeypatch.setattr(plugin_directory, '_dispatch_auto_outbound_messages', lambda *args: [])
    monkeypatch.setattr(plugin_directory, '_start_channel_typing_indicator', lambda *args: None)
    with tenant_scope(ids['tenant_id']), tenant_session(factory) as db:
        channel, _binding = _configure(db, ids, command_overrides={'签到':['打卡'], '补签':['补打卡']})
        request = _message_sign_request(db, ids, content='补打卡', sender_id='makeup-user', sender_name='补签用户')
        report = plugin_directory.build_dispatch_report(db, channel, _event('补打卡', sender='makeup-user'),
            conversation_id=ids['conversation_id'], message_id=request.context.message_id, skip_ai_intent=True)
        execution, = report.executions
        assert execution.debug['output']['provider_status'] == 'record_not_found'
        assert '还没有签到记录' in execution.message
        assert db.get(Message, request.context.message_id).content == '补打卡'


def test_native_and_scheduled_options_follow_trusted_conversation_without_secrets(message_sign_fixture, monkeypatch):
    """两条执行链都读本群参数，陌生会话不读覆盖，清单敏感键不能从群配置进入。"""

    factory, ids = message_sign_fixture
    discovered = plugin_directory.get_discovered_plugin('message_sign')
    original = plugin_directory.get_discovered_plugin
    monkeypatch.setattr(plugin_directory, 'get_discovered_plugin',
        lambda code: replace(discovered, secret_fields=('service_token',)) if code == 'message_sign' else original(code))
    requests = []

    def capture(_db, request):
        requests.append(request)
        return PluginExecutionResult(ok=True, status='completed', message='已读取本群参数')

    definition = runtime_service.get_plugin_runtime_definition('message_sign')
    executor = SimpleNamespace(execute_message=capture, execute_scheduled=capture)
    monkeypatch.setattr(runtime_service, 'get_plugin_runtime_definition', lambda _code: replace(definition, executor=executor))
    with tenant_scope(ids['tenant_id']), tenant_session(factory) as db:
        _channel, binding = _configure(db, ids, fortune_image_enabled=False, min_point=17, service_token='group-plaintext')
        request = _message_sign_request(db, ids, content='签到', sender_id='native-boundary-user', sender_name='配置边界用户')
        for execute in (runtime_service.execute_binding_message_action, runtime_service.execute_binding_scheduled_action):
            assert execute(db, binding, 'execute', context=request.context).ok
            assert requests[-1].config['min_point'] == 17
            assert requests[-1].config['fortune_image_enabled'] is False
            assert 'service_token' not in requests[-1].config
            context = replace(request.context, conversation_id=ids['other_conversation_id'])
            assert execute(db, binding, 'execute', context=context).ok
            assert requests[-1].config['min_point'] == 3
            assert requests[-1].config['fortune_image_enabled'] is True


def test_weather_custom_command_reaches_runtime_with_complete_arguments(message_sign_fixture, monkeypatch):
    """使用真实天气匹配器验证带参数指令，执行边界同时收到正式命令和完整段落。"""

    factory, ids = message_sign_fixture
    requests = []
    monkeypatch.setattr(plugin_directory, '_dispatch_auto_outbound_messages', lambda *args: [])
    monkeypatch.setattr(plugin_directory, '_start_channel_typing_indicator', lambda *args: None)

    def capture(_db, request):
        requests.append(request)
        return PluginExecutionResult(ok=True, status='completed', message='天气参数已保留')

    definition = runtime_service.get_plugin_runtime_definition('weather')
    monkeypatch.setattr(runtime_service, 'get_plugin_runtime_definition',
        lambda _code: replace(definition, executor=SimpleNamespace(execute_message=capture)))
    with tenant_scope(ids['tenant_id']), tenant_session(factory) as db:
        channel, binding = _configure(db, ids, command_overrides={'天气':['查天气']})
        binding.plugin.code = 'weather'
        db.commit()
        content = '/查天气 上海\n明天'
        request = _message_sign_request(db, ids, content=content, sender_id='weather-user', sender_name='天气用户')
        report = plugin_directory.build_dispatch_report(db, channel, _event(content, sender='weather-user'),
            conversation_id=ids['conversation_id'], message_id=request.context.message_id, skip_ai_intent=True)
        assert report.executions[0].ok
        assert requests[0].payload['content'] == '/天气 上海\n明天'
        assert requests[0].payload['command_name'] == '天气'
        assert db.get(Message, request.context.message_id).content == content
