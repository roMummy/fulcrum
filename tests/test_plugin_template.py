"""文档中心的插件开发规范页与标准示例插件:示例必须始终能通过真实的安装校验并按文档所述工作。"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from test_developer_docs import _load_route, _principal, _request

_load_route()  # 先准备好独立的测试运行环境,再导入应用模块

from app.core.settings import settings  # noqa: E402
from app.services import plugin_directory, plugin_packages  # noqa: E402
from app.services.plugin_runtime.contracts import PluginExecutionRequest  # noqa: E402
from app.web import developer_docs  # noqa: E402


def _call(handler, path: str, logged_in: bool = True):
    request = _request(path)
    request.state.current_user = _principal() if logged_in else None
    return asyncio.run(handler(request))


def test_the_plugin_spec_page_requires_login_and_shows_the_spec_and_the_download() -> None:
    assert _call(developer_docs.plugin_docs_page, "/developer/docs/plugins", logged_in=False).status_code == 303
    response = _call(developer_docs.plugin_docs_page, "/developer/docs/plugins")
    html = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Fulcrum 插件开发规范" in html and "打包与交付" in html
    assert 'href="/developer/docs/plugins/hello_plugin.zip"' in html
    # 两个页签,当前页高亮
    assert 'class="section-tab active" href="/developer/docs/plugins"' in html and 'href="/developer/docs"' in html


def test_the_bridge_docs_page_gets_the_same_tabs() -> None:
    html = _call(developer_docs.developer_docs_page, "/developer/docs").body.decode("utf-8")
    assert 'class="section-tab active" href="/developer/docs"' in html and "插件开发规范" in html


def test_the_template_download_requires_login_and_is_a_zip_of_the_template_directory() -> None:
    assert _call(developer_docs.plugin_template_download, "/developer/docs/plugins/hello_plugin.zip", logged_in=False).status_code == 303
    response = _call(developer_docs.plugin_template_download, "/developer/docs/plugins/hello_plugin.zip")
    assert response.status_code == 200 and response.media_type == "application/zip"
    assert "hello_plugin.zip" in response.headers["content-disposition"]
    names = sorted(zipfile.ZipFile(io.BytesIO(response.body)).namelist())
    assert names == ["hello_plugin/README.md", "hello_plugin/config.toml", "hello_plugin/plugin.py"]


@pytest.fixture()
def installed_template(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins_dir", str(tmp_path))
    plugin_directory._SCAN_CACHE.clear()
    result = plugin_packages.install_or_upgrade(developer_docs.build_plugin_template_zip())  # 走真实的上传校验
    yield result
    plugin_directory._SCAN_CACHE.clear()


def test_the_template_passes_the_real_install_checks_and_its_declared_defaults_match(installed_template) -> None:
    assert (installed_template.action, installed_template.code) == ("install", "hello_plugin")
    (found,) = plugin_directory.discover_plugins(force=True)
    assert found.is_valid and not found.validation_errors
    declared = {field.key: field for field in found.config_fields}
    assert list(declared["commands"].default) == list(found.matcher.commands)
    assert bool(declared["require_mention"].default) == bool(found.matcher.require_mention)
    # 每个声明了的配置项在 [runtime] 里都有同样的默认值(触发方式字段除外)
    for key, field in declared.items():
        if key not in ("commands", "keywords", "require_mention"):
            assert found.runtime_config[key] == field.default, key


def _run(config: dict, command_args: str = "", action: str = "execute"):
    loaded = plugin_directory.load_directory_plugin("hello_plugin")
    return loaded.instance.execute_action(PluginExecutionRequest(action=action, config=config, payload={"command_args": command_args}))


def test_the_template_replies_according_to_its_config_and_survives_bad_values(installed_template) -> None:
    assert _run({}, action="reload").ok
    assert not _run({}, action="explode").ok
    result = _run({"greeting": "早", "times": 2, "loud": True, "signature": ["甲", "乙"]}, "小明")
    assert result.output["reply_text"] == "小明,早! 早!\n—— 甲、乙"
    # 配置被改坏时(类型错误、越界)不抛异常,回到安全值
    bad = _run({"greeting": "", "times": "abc", "style": "???", "signature": "不是列表"})
    assert bad.ok and bad.output["reply_text"] == "你好~"
    assert _run({"times": 999}).output["reply_text"].count("你好") == 5


# ---------- 示例演示的新能力:定时与成员昵称 ----------

def test_the_template_declares_a_schedule_and_the_scheduler_picks_it_up(installed_template) -> None:
    from app.services.plugin_runtime import scheduler

    assert "hello_plugin" in scheduler._declared_schedule_codes()
    (item,) = scheduler._declared_schedules("hello_plugin")
    assert (item.action, item.kind, item.time_key, item.default_time, item.enabled_key) == ("daily_greeting", "daily", "send_time", "09:00", "scheduled_enabled")
    found = plugin_directory.get_discovered_plugin("hello_plugin")
    declared = {field.key for field in found.config_fields}
    assert {"scheduled_enabled", "send_time", "daily_text"} <= declared  # 定时相关配置项在页面上可配
    assert "你好榜" in found.matcher.commands


def _sqlite_env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, PluginStateRecord

    engine = create_engine("sqlite://")
    for table in (Channel, Conversation, Chatroom, ChatroomMember, PluginStateRecord, BotPlugin):
        table.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_the_board_shows_group_nicknames_not_wxids(installed_template) -> None:
    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import tenant_session
    from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation
    from app.services.plugin_runtime.contracts import PluginExecutionContext

    module = plugin_directory.load_directory_plugin("hello_plugin").instance._module
    with tenant_scope(1), tenant_session(_sqlite_env()) as db:
        channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge")
        db.add(channel)
        db.flush()
        conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
        room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="群", is_active=True)
        db.add_all([conversation, room])
        db.flush()
        db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_a1", display_name="小明", status="active"))
        db.flush()

        def say(sender: str, command: str = "你好"):
            context = PluginExecutionContext(tenant_id=1, binding_id=9, bot_id=1, channel_id=channel.id, conversation_id=conversation.id, sender_id=sender)
            request = PluginExecutionRequest(action="execute", config={}, payload={"command_name": command, "command_args": ""}, context=context)
            return module.execute_message_action(db, request)

        assert "还没有人" in say("wxid_a1", "你好榜").output["reply_text"]
        say("wxid_a1"), say("wxid_a1"), say("wxid_zz")
        board = say("wxid_a1", "你好榜").output["reply_text"].splitlines()
        assert board == ["【本群打招呼排行榜】", "1. 小明 - 2次", "2. wxid_zz - 1次"]  # 成员表里有昵称就显示昵称;没有才显示 ID


def test_the_scheduled_action_queues_one_message_per_enabled_group(installed_template, monkeypatch) -> None:
    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import tenant_session
    from app.models.entities import BotPlugin, Channel, Chatroom, Conversation
    from app.services.plugin_runtime.contracts import PluginExecutionContext

    module = plugin_directory.load_directory_plugin("hello_plugin").instance._module
    queued = []
    monkeypatch.setattr(module, "enqueue_outbound_job", lambda db, **kw: queued.append(kw))
    factory = _sqlite_env()
    with tenant_scope(1), tenant_session(factory) as db:
        channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.flush()
        for ext, status in (("a@chatroom", "enabled"), ("b@chatroom", "enabled"), ("c@chatroom", "discovered")):
            db.add(Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id=ext, name=ext, is_active=True, management_status=status))
            db.add(Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id=ext, chat_type="group"))
        db.flush()
        fake_binding = type("B", (), {"id": 9, "status": "enabled", "bot_id": 1, "bot": type("Bot", (), {"is_enabled": True})()})()
        monkeypatch.setattr(db, "get", lambda model, key: fake_binding if model is BotPlugin else None)
        context = PluginExecutionContext(tenant_id=1, binding_id=9, bot_id=1)
        run = lambda config, action="daily_greeting": module.execute_scheduled_action(db, PluginExecutionRequest(action=action, config=config, payload={}, context=context))
        assert run({}).status == "skipped" and queued == []                              # 开关没打开
        assert run({"scheduled_enabled": True}, action="other").status == "skipped"
        result = run({"scheduled_enabled": True, "daily_text": "早!"})
        assert result.ok and "2 个群" in result.message                                  # 只发给启用了群管理的两个群
        assert sorted(item["conversation"].external_id for item in queued) == ["a@chatroom", "b@chatroom"]
        assert {item["payload"]["content"] for item in queued} == {"早!"} and all(item["max_attempts"] == 1 for item in queued)
