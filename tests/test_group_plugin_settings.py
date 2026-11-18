"""通过真实认证和群详情路由验证本群插件设置，不连接生产手机。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Plugin
from app.services import group_plugin_settings, plugin_directory
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.plugin_runtime.contracts import PluginExecutionContext
from app.services.plugin_runtime.service import _group_config_override, _resolve_proxy
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture()
def group_settings_context(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids['factory']() as db:
        sync_plugins_from_directory(db, force=True)
        plugin = db.scalar(select(Plugin).where(Plugin.code == 'message_sign'))
        binding = BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=plugin.id, status='enabled',
                            config_json=json.dumps({'fortune_image_enabled': True, 'min_point': 3, 'max_point':50}))
        rooms = [Chatroom(tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'],
                          external_id=f'settings-{index}@chatroom', name=f'自定义测试群{index}',
                          is_active=True, management_status='enabled') for index in range(2)]
        db.add_all([binding, *rooms])
        db.commit()
        ids = {**ids, 'plugin': plugin.id, 'binding':binding.id, 'room':rooms[0].id, 'other_room':rooms[1].id}
    return client, ids


def settings_url(ids, suffix=''):
    return f"/channels/{ids['channel_id']}/chatrooms/{ids['room']}/plugins/{ids['binding']}/settings{suffix}"


def test_save_group_settings_preserves_disabled_scope_and_other_groups(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    with ids['factory']() as db:
        db.add(GroupPluginScope(tenant_id=ids['alpha'], channel_id=ids['channel_id'], chatroom_id=ids['room'],
                               bot_plugin_id=ids['binding'], scope_status='overridden', enabled=False))
        db.commit()
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':'打卡', 'command__1':'补打卡',
        'require_mention':'true', 'schema__fortune_image_enabled':'false', 'schema__background_url':''},
        headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200, response.text
    assert response.json()['ok']
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'], GroupPluginScope.bot_plugin_id == ids['binding']))
        config = json.loads(scope.config_override_json)
        assert config['command_overrides'] == {'签到':['打卡'], '补签':['补打卡']}
        assert config['fortune_image_enabled'] is False and config['require_mention'] is True
        assert not scope.enabled
        assert json.loads(db.get(BotPlugin, ids['binding']).config_json)['fortune_image_enabled'] is True
        other = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['other_room'], GroupPluginScope.bot_plugin_id == ids['binding']))
        assert other is None or other.config_override_json is None
    page = client.get(f"/channels/{ids['channel_id']}/chatrooms/{ids['room']}?tab=plugins").text
    assert '>打卡</textarea>' in page and '>补打卡</textarea>' in page
    assert 'data-group-plugin-settings' in page and 'aria-current="page"' in page


def test_daily_news_group_push_time_is_editable_and_saved_as_override(group_settings_context):
    """声明式定时插件的群级时间必须能通过群详情保存，调度器才会看到覆盖。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    with ids['factory']() as db:
        plugin = db.scalar(select(Plugin).where(Plugin.code == 'daily_news'))
        assert plugin is not None
        binding = BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=plugin.id, status='enabled',
                            config_json=json.dumps({'push_time': '08:30'}))
        db.add(binding)
        db.commit()
        binding_id = int(binding.id)

    target_ids = {**ids, 'binding': binding_id}
    response = client.post(settings_url(target_ids), data={
        'csrf_token': csrf,
        'schema__push_time': '17:35',
    }, headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 200, response.text
    assert response.json()['ok']
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(
            GroupPluginScope.chatroom_id == ids['room'],
            GroupPluginScope.bot_plugin_id == binding_id,
        ))
        assert json.loads(scope.config_override_json) == {'push_time': '17:35'}


def test_reset_config_keeps_explicit_group_switch(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'admin')
    with ids['factory']() as db:
        db.add(GroupPluginScope(tenant_id=ids['alpha'], channel_id=ids['channel_id'], chatroom_id=ids['room'],
                               bot_plugin_id=ids['binding'], enabled=False, scope_status='overridden',
                               config_override_json='{"fortune_image_enabled":false}'))
        db.commit()
    response = client.post(settings_url(ids, '/reset'), data={'csrf_token':csrf}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200, response.text
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'], GroupPluginScope.bot_plugin_id == ids['binding']))
        assert scope.config_override_json is None and not scope.enabled and scope.scope_status == 'overridden'


def test_group_auto_revoke_can_override_and_restore_binding_defaults(group_settings_context):
    """群只保存真正改动的撤回参数，关闭也要覆盖机器人默认的开启状态。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    with ids['factory']() as db:
        binding = db.get(BotPlugin, ids['binding'])
        config = json.loads(binding.config_json)
        config['reply_auto_revoke'] = {'enabled':True, 'min_seconds':60, 'max_seconds':80}
        binding.config_json = json.dumps(config)
        db.commit()
    response = client.post(settings_url(ids), data={'csrf_token':csrf,
        'delivery__revoke_enabled':'false', 'delivery__revoke_min_seconds':'60',
        'delivery__revoke_max_seconds':'80'}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200, response.text
    assert not response.json()['settings']['config_view']['delivery']['enabled']
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'],
            GroupPluginScope.bot_plugin_id == ids['binding']))
        assert json.loads(scope.config_override_json)['reply_auto_revoke'] == {'enabled':False}
        assert json.loads(db.get(BotPlugin, ids['binding']).config_json)['reply_auto_revoke']['enabled']
    reset = client.post(settings_url(ids, '/reset'), data={'csrf_token':csrf}, headers={'X-Requested-With':'fetch'})
    assert reset.status_code == 200, reset.text
    assert reset.json()['settings']['config_view']['delivery']['enabled']
    assert not reset.json()['settings']['config_custom']


@pytest.mark.parametrize('minimum,maximum', [('81','80'), ('0','80'), ('60','120'), ('60.5','80')])
def test_invalid_auto_revoke_interval_is_rejected_without_saving(group_settings_context, minimum, maximum):
    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    response = client.post(settings_url(ids), data={'csrf_token':csrf,
        'delivery__revoke_enabled':'true', 'delivery__revoke_min_seconds':minimum,
        'delivery__revoke_max_seconds':maximum}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 400, response.text
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'],
            GroupPluginScope.bot_plugin_id == ids['binding']))
        assert scope is None or scope.config_override_json is None


def test_binding_auto_revoke_form_saves_public_policy_without_plugin_schema_fields(group_settings_context):
    """公共能力可以独立保存，不要求每个插件在清单里重复声明撤回字段。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    response = client.post(f"/plugins/{ids['binding']}/edit", data={'csrf_token':csrf,
        'delivery__revoke_enabled':'true', 'delivery__revoke_min_seconds':'60',
        'delivery__revoke_max_seconds':'80'}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200, response.text
    with ids['factory']() as db:
        config = json.loads(db.get(BotPlugin, ids['binding']).config_json)
        assert config['reply_auto_revoke'] == {'enabled':True, 'min_seconds':60, 'max_seconds':80}
        assert config['fortune_image_enabled'] is True and config['min_point'] == 3
    invalid = client.post(f"/plugins/{ids['binding']}/edit", data={'csrf_token':csrf,
        'delivery__revoke_enabled':'true', 'delivery__revoke_min_seconds':'90',
        'delivery__revoke_max_seconds':'80'}, headers={'X-Requested-With':'fetch'})
    assert invalid.status_code == 400 and '最短撤回时间' in invalid.json()['notice']


def test_saving_unchanged_fields_keeps_inheriting_robot_defaults(group_settings_context):
    """完整表单会提交未改过的开关；这些值不能被误记为本群独立覆盖。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':'签到', 'command__1':'补签',
        'require_mention':'false', 'schema__fortune_image_enabled':'true', 'schema__background_url':''},
        headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200, response.text
    assert not response.json()['settings']['config_custom']
    assert not response.json()['settings']['custom']
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.bot_plugin_id == ids['binding']))
        assert scope.config_override_json is None and scope.scope_status == 'inherited'


@pytest.mark.parametrize('form', [
    {'command__0':'同一指令', 'command__1':'同一指令'},
    {'command__0':'/打卡'},
    {'schema__unknown_field':'不应保存'},
    {'config_json':'{"任意字段":true}'},
])
def test_invalid_settings_are_rejected_without_partial_writes(group_settings_context, form):
    client, ids = group_settings_context
    csrf = _login(client, 'admin')
    response = client.post(settings_url(ids), data={'csrf_token':csrf, **form}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 400, response.text
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'], GroupPluginScope.bot_plugin_id == ids['binding']))
        assert scope is None or scope.config_override_json is None


def _daily_news_binding(ids) -> int:
    with ids['factory']() as db:
        plugin = db.scalar(select(Plugin).where(Plugin.code == 'daily_news'))
        binding = BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=plugin.id,
                            status='enabled', config_json=json.dumps({'push_time': '08:30'}))
        db.add(binding)
        db.commit()
        return int(binding.id)


def _scope_for(ids, binding_id):
    with ids['factory']() as db:
        return db.scalar(select(GroupPluginScope).where(
            GroupPluginScope.chatroom_id == ids['room'],
            GroupPluginScope.bot_plugin_id == binding_id,
        ))


def test_group_proxy_can_override_binding_proxy_with_address(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    binding_id = _daily_news_binding(ids)
    response = client.post(settings_url({**ids, 'binding': binding_id}), data={
        'csrf_token': csrf,
        'group_proxy_mode': 'proxy',
        'group_proxy_url': 'socks5h://group-proxy.test:1080',
        'group_proxy_bypass': '.internal.test',
    }, headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 200, response.text
    assert response.json()['ok']
    scope = _scope_for(ids, binding_id)
    config = json.loads(scope.config_override_json)
    assert config['group_proxy_mode'] == 'proxy'
    assert config['group_proxy_url'] == 'socks5h://group-proxy.test:1080'
    assert config['group_proxy_bypass'] == 'internal.test'
    assert response.json()['settings']['proxy_view'] == {
        'mode': 'proxy', 'url': 'socks5h://group-proxy.test:1080', 'bypass': 'internal.test',
    }
    page = client.get(f"/channels/{ids['channel_id']}/chatrooms/{ids['room']}?tab=plugins").text
    assert 'socks5h://group-proxy.test:1080' in page


def test_group_proxy_direct_and_inherit_clear_proxy_config(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    binding_id = _daily_news_binding(ids)
    url = settings_url({**ids, 'binding': binding_id})
    custom = client.post(url, data={
        'csrf_token': csrf, 'group_proxy_mode': 'proxy',
        'group_proxy_url': 'http://group-proxy.test:8080',
    }, headers={'X-Requested-With': 'fetch'})
    assert custom.status_code == 200, custom.text

    direct = client.post(url, data={'csrf_token': csrf, 'group_proxy_mode': 'direct'},
                         headers={'X-Requested-With': 'fetch'})
    assert direct.status_code == 200, direct.text
    scope = _scope_for(ids, binding_id)
    assert json.loads(scope.config_override_json) == {'group_proxy_mode': 'direct'}

    # 旧页面可能仍提交 inherit；现在按新的默认直连处理。
    inherit = client.post(url, data={'csrf_token': csrf, 'group_proxy_mode': 'inherit'},
                          headers={'X-Requested-With': 'fetch'})
    assert inherit.status_code == 200, inherit.text
    scope = _scope_for(ids, binding_id)
    assert json.loads(scope.config_override_json) == {'group_proxy_mode': 'direct'}


def test_group_proxy_override_is_resolved_for_group_runtime(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    binding_id = _daily_news_binding(ids)
    response = client.post(settings_url({**ids, 'binding': binding_id}), data={
        'csrf_token': csrf, 'group_proxy_mode': 'proxy',
        'group_proxy_url': 'http://group-proxy.test:8080',
    }, headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 200, response.text
    with ids['factory']() as db:
        conversation = Conversation(
            tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'],
            channel_type='wechat', external_id='settings-0@chatroom', chat_type='group',
        )
        db.add(conversation)
        db.commit()
        binding = db.get(BotPlugin, binding_id)
        context = PluginExecutionContext(
            tenant_id=ids['alpha'], binding_id=binding_id, bot_id=ids['bot'],
            channel_id=ids['channel_id'], conversation_id=int(conversation.id),
        )
        override = _group_config_override(db, binding, context)
    assert override['group_proxy_mode'] == 'proxy'
    assert override['group_proxy_url'] == 'http://group-proxy.test:8080'
    endpoint = _resolve_proxy({**json.loads(binding.config_json), **override})
    assert (endpoint.host, endpoint.port, endpoint.username) == ('group-proxy.test', 8080, '')


def test_group_proxy_precedence_over_binding_proxy() -> None:
    binding = {'proxy_url': 'http://binding.test:8080'}
    assert _resolve_proxy(binding).host == 'binding.test'
    assert _resolve_proxy({**binding, 'group_proxy_mode': 'inherit'}).host == 'binding.test'
    assert _resolve_proxy({**binding, 'group_proxy_mode': 'direct'}) is None
    for mode in ('proxy', 'custom'):
        endpoint = _resolve_proxy({
            **binding,
            'group_proxy_mode': mode,
            'group_proxy_url': 'socks5://group.test:1080',
        })
        assert (endpoint.proxy_type, endpoint.host, endpoint.port) == ('socks5', 'group.test', 1080)


def test_group_default_proxy_mode_is_direct(group_settings_context):
    """群未显式配置代理时跟随绑定页代理；绑定没有代理才直连。"""

    _, ids = group_settings_context
    binding_id = _daily_news_binding(ids)
    with ids['factory']() as db:
        binding = db.get(BotPlugin, binding_id)
        binding.config_json = json.dumps({'push_time': '08:30', 'proxy_url': 'http://binding.test:8080'})
        conversation = Conversation(
            tenant_id=ids['alpha'], bot_id=ids['bot'], channel_id=ids['channel_id'],
            channel_type='wechat', external_id='settings-0@chatroom', chat_type='group',
        )
        db.add(conversation)
        db.commit()
        context = PluginExecutionContext(
            tenant_id=ids['alpha'], binding_id=binding_id, bot_id=ids['bot'],
            channel_id=ids['channel_id'], conversation_id=int(conversation.id),
        )
        override = _group_config_override(db, binding, context)
        assert 'group_proxy_mode' not in override
        endpoint = _resolve_proxy({**json.loads(binding.config_json), **override})
        assert endpoint is not None and endpoint.host == 'binding.test'

        scope = GroupPluginScope(
            tenant_id=ids['alpha'], channel_id=ids['channel_id'], chatroom_id=ids['room'],
            bot_plugin_id=binding_id, config_override_json=json.dumps({'group_proxy_mode': 'direct'}),
        )
        db.add(scope)
        db.commit()
        override = _group_config_override(db, binding, context)
        assert override['group_proxy_mode'] == 'direct'
        assert _resolve_proxy({**json.loads(binding.config_json), **override}) is None


def test_group_proxy_view_prefills_binding_proxy(group_settings_context):
    """群切到代理时应带入插件配置页的地址，而不是空白输入框。"""

    _, ids = group_settings_context
    binding_id = _daily_news_binding(ids)
    with ids['factory']() as db:
        binding = db.get(BotPlugin, binding_id)
        binding.config_json = json.dumps({
            'proxy_url': 'http://binding.test:8080',
            'proxy_username': 'binding-user',
            'proxy_bypass': 'internal.test',
        })
        db.commit()
        view = group_plugin_settings._group_proxy_view(None, binding)
    assert view == {
        'mode': 'proxy',
        'url': 'http://binding.test:8080',
        'bypass': 'internal.test',
    }


def test_group_proxy_rejects_invalid_port(group_settings_context):
    """端口超出 1-65535 时返回具体错误，而不是笼统的配置不完整提示。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    binding_id = _daily_news_binding(ids)
    response = client.post(settings_url({**ids, 'binding': binding_id}), data={
        'csrf_token': csrf,
        'group_proxy_mode': 'proxy',
        'group_proxy_url': 'http://host.docker.internal:78999',
    }, headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 400, response.text
    assert '端口' in response.json()['notice']
    assert '1-65535' in response.json()['notice']


def test_group_proxy_mode_requires_url(group_settings_context):
    """选择代理后必须填写地址，不能把空地址保存成代理配置。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    binding_id = _daily_news_binding(ids)
    response = client.post(settings_url({**ids, 'binding': binding_id}), data={
        'csrf_token': csrf,
        'group_proxy_mode': 'proxy',
        'group_proxy_url': '',
    }, headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 400, response.text
    assert '代理地址' in response.json()['notice']
    scope = _scope_for(ids, binding_id)
    assert scope is None or scope.config_override_json is None


def test_group_broken_proxy_without_url_falls_back_to_binding_proxy() -> None:
    """历史损坏数据回退到绑定代理，保证插件仍能执行并回复。"""

    binding = {'proxy_url': 'http://binding.test:8080'}
    for mode in ('proxy', 'custom'):
        endpoint = _resolve_proxy({**binding, 'group_proxy_mode': mode, 'group_proxy_url': ''})
        assert endpoint is not None and endpoint.host == 'binding.test'


def test_group_proxy_reset_follows_binding(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'admin')
    binding_id = _daily_news_binding(ids)
    url = settings_url({**ids, 'binding': binding_id})
    custom = client.post(url, data={
        'csrf_token': csrf, 'group_proxy_mode': 'proxy',
        'group_proxy_url': 'http://group-proxy.test:8080',
        'group_proxy_username': 'u', 'group_proxy_password': 'p',
    }, headers={'X-Requested-With': 'fetch'})
    assert custom.status_code == 200, custom.text
    reset = client.post(url + '/reset', data={'csrf_token': csrf},
                        headers={'X-Requested-With': 'fetch'})
    assert reset.status_code == 200, reset.text
    scope = _scope_for(ids, binding_id)
    assert scope.config_override_json is None


def test_group_settings_requires_csrf_and_operator_role(group_settings_context):
    client, ids = group_settings_context
    _login(client, 'admin')
    assert client.post(settings_url(ids), data={'command__0':'打卡'}).status_code == 403
    csrf = _login(client, 'readonly')
    assert client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':'打卡'}).status_code == 403


def test_unbound_plugin_cannot_be_configured(group_settings_context):
    client, ids = group_settings_context
    csrf = _login(client, 'admin')
    with ids['factory']() as db:
        db.get(BotPlugin, ids['binding']).status = 'unbound'
        db.commit()
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':'打卡'}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 400


def test_group_settings_preserves_explicit_empty_values_and_keywords(group_settings_context):
    """本群清空值必须覆盖机器人默认值，不能在序列化或匹配阶段被补回。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    with ids['factory']() as db:
        binding = db.get(BotPlugin, ids['binding'])
        binding.config_json = json.dumps({'background_url':'https://images.example/background.jpg'})
        parser = db.scalar(select(Plugin).where(Plugin.code == 'douyin_parser'))
        keyword_binding = BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=parser.id, status='enabled',
                                    config_json=json.dumps({'keywords':['douyin.com']}))
        db.add(keyword_binding)
        db.commit()
        keyword_id = keyword_binding.id
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'schema__background_url':''})
    assert response.status_code == 303
    keyword_url = settings_url({**ids, 'binding':keyword_id})
    response = client.post(keyword_url, data={'csrf_token':csrf, 'keywords':''}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200 and response.json()['settings']['keywords_value'] == ''
    with ids['factory']() as db:
        scopes = db.scalars(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids['room'])).all()
        config = {scope.bot_plugin_id:json.loads(scope.config_override_json or '{}') for scope in scopes}
        assert config[ids['binding']]['background_url'] == ''
        assert config[keyword_id]['keywords'] == []
        matcher = plugin_directory._resolve_effective_matcher(db.get(BotPlugin, keyword_id), plugin_directory.get_discovered_plugin('douyin_parser'), json.dumps(config[keyword_id]))
        assert matcher.keywords == ()


def test_group_settings_rejects_other_tenants_channels_and_bot_bindings(group_settings_context):
    """保存和恢复都必须经过相同的归属校验，不能把别的群或机器人的主键拼进路径。"""

    client, ids = group_settings_context
    csrf = _login(client, 'admin')
    with ids['factory']() as db:
        other_bot = Bot(tenant_id=ids['alpha'], name='另一个机器人')
        foreign_bot = Bot(tenant_id=ids['beta'], name='另一个租户')
        db.add_all([other_bot, foreign_bot])
        db.flush()
        other_binding = BotPlugin(tenant_id=ids['alpha'], bot_id=other_bot.id, plugin_id=ids['plugin'], status='enabled')
        other_channel = Channel(tenant_id=ids['alpha'], bot_id=ids['bot'], name='另一个渠道', channel_type='wechat_ipad')
        foreign_channel = Channel(tenant_id=ids['beta'], bot_id=foreign_bot.id, name='其他租户渠道', channel_type='wechat_ipad')
        db.add_all([other_binding, other_channel, foreign_channel])
        db.flush()
        attempts = [({**ids, 'binding':other_binding.id}, 400),
                    ({**ids, 'channel_id':other_channel.id}, 404),
                    ({**ids, 'channel_id':foreign_channel.id}, 404)]
        db.commit()
    for attack, expected in attempts:
        for suffix in ('', '/reset'):
            response = client.post(settings_url(attack, suffix), data={'csrf_token':csrf})
            assert response.status_code == expected, response.text
    with ids['factory']() as db:
        assert db.scalar(select(GroupPluginScope)) is None


def test_group_settings_cleans_manifest_secrets_without_echo_or_write(group_settings_context, monkeypatch):
    """清单可声明不在表单中的敏感键；页面回报和群覆盖仍必须将其彻底剔除。"""

    client, ids = group_settings_context
    csrf = _login(client, 'admin')
    original = group_plugin_settings.get_discovered_plugin
    monkeypatch.setattr(group_plugin_settings, 'get_discovered_plugin',
        lambda code: replace(original(code), secret_fields=('service_token',)) if code == 'message_sign' else original(code))
    with ids['factory']() as db:
        db.get(BotPlugin, ids['binding']).config_json = json.dumps({'service_token':'binding-sensitive-value'})
        db.add(GroupPluginScope(tenant_id=ids['alpha'], channel_id=ids['channel_id'], chatroom_id=ids['room'],
            bot_plugin_id=ids['binding'], enabled=True, scope_status='overridden',
            config_override_json=json.dumps({'service_token':'scope-sensitive-value'})))
        db.commit()
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'schema__min_point':'9'}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200, response.text
    assert 'sensitive-value' not in response.text and 'service_token' not in response.text
    assert client.post(settings_url(ids), data={'csrf_token':csrf, 'schema__service_token':'forged'}).status_code == 400
    with ids['factory']() as db:
        scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.bot_plugin_id == ids['binding']))
        assert 'service_token' not in json.loads(scope.config_override_json)
        assert json.loads(db.get(BotPlugin, ids['binding']).config_json)['service_token'] == 'binding-sensitive-value'


def test_group_settings_rejects_conflicts_and_restores_single_default_command(group_settings_context):
    """其他插件的启用指令不能被抢占；只清空一项指令不影响其他业务映射。"""

    client, ids = group_settings_context
    csrf = _login(client, 'operator')
    with ids['factory']() as db:
        weather = db.scalar(select(Plugin).where(Plugin.code == 'weather'))
        db.add(BotPlugin(tenant_id=ids['alpha'], bot_id=ids['bot'], plugin_id=weather.id, status='enabled'))
        db.commit()
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':'天气'})
    assert response.status_code == 400 and '已由本群' in response.text
    client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':'打卡', 'command__1':'补打卡'})
    response = client.post(settings_url(ids), data={'csrf_token':csrf, 'command__0':''}, headers={'X-Requested-With':'fetch'})
    assert response.status_code == 200
    assert response.json()['settings']['triggers'] == ['签到','补打卡']
