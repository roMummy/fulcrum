from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-fun-command-play"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, Plugin
from app.models.plugin_business import FunCommandRule
from app.models.saas import Tenant
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest


def _load_plugin_module():
    """从目录加载真实插件入口，避免测试复制实现。"""

    plugin_path = PROJECT_ROOT / "plugins" / "fun_command_play" / "plugin.py"
    spec = importlib.util.spec_from_file_location("fun_command_play_plugin_test", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在 Python 3.10 解析延迟注解时会从 sys.modules 读取模块命名空间，
    # 动态加载测试必须先完成注册，行为才与目录运行时的正常 import 一致。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fun_command_fixture(tmp_path: Path):
    """建立两个租户的机器人、渠道和真实入站消息，验证规则不会跨租户或跨绑定。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'fun-command.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        plugin = Plugin(code="fun_command_play", display_name="趣味命令", plugin_type="observer")
        db.add_all([alpha, beta, plugin])
        db.flush()
        alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人", status="running", is_enabled=True)
        beta_bot = Bot(tenant_id=beta.id, name="乙机器人", status="running", is_enabled=True)
        db.add_all([alpha_bot, beta_bot])
        db.flush()
        alpha_binding = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        beta_binding = BotPlugin(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add_all([alpha_binding, beta_binding])
        db.flush()
        alpha_channel = Channel(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            name="甲 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        beta_channel = Channel(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            name="乙 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add_all([alpha_channel, beta_channel])
        db.flush()
        alpha_conversation = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            channel_type="telegram",
            external_id="alpha-group",
            title="甲群",
            chat_type="group",
        )
        beta_conversation = Conversation(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            channel_id=beta_channel.id,
            channel_type="telegram",
            external_id="beta-group",
            title="乙群",
            chat_type="group",
        )
        db.add_all([alpha_conversation, beta_conversation])
        db.flush()
        alpha_message = Message(
            tenant_id=alpha.id,
            conversation_id=alpha_conversation.id,
            direction="in",
            sender_id="alpha-user",
            sender_name="甲用户",
            content="来个节目",
            message_type="text",
            status="received",
        )
        beta_message = Message(
            tenant_id=beta.id,
            conversation_id=beta_conversation.id,
            direction="in",
            sender_id="beta-user",
            sender_name="乙用户",
            content="来个节目",
            message_type="text",
            status="received",
        )
        db.add_all([alpha_message, beta_message])
        db.flush()
        alpha_rule = FunCommandRule(
            tenant_id=alpha.id,
            bot_plugin_id=alpha_binding.id,
            name="甲租户节目",
            trigger_text="来个",
            match_type="prefix",
            replies_json=json.dumps(
                [
                    {"type": "text", "content": "{sender_name}，节目开始"},
                    {
                        "type": "link",
                        "title": "{conversation}活动",
                        "description": "由{sender_name}触发",
                        "url": "https://alpha.example.test/activity",
                    },
                ],
                ensure_ascii=False,
            ),
            priority=10,
            cooldown_seconds=0,
            is_enabled=True,
        )
        beta_rule = FunCommandRule(
            tenant_id=beta.id,
            bot_plugin_id=beta_binding.id,
            name="乙租户节目",
            trigger_text="来个",
            match_type="prefix",
            replies_json='[{"type":"text","content":"乙租户回复"}]',
            priority=10,
            cooldown_seconds=0,
            is_enabled=True,
        )
        db.add_all([alpha_rule, beta_rule])
        db.commit()
        identity = {
            "alpha_tenant_id": int(alpha.id),
            "alpha_bot_id": int(alpha_bot.id),
            "alpha_binding_id": int(alpha_binding.id),
            "alpha_channel_id": int(alpha_channel.id),
            "alpha_conversation_id": int(alpha_conversation.id),
            "alpha_message_id": int(alpha_message.id),
            "alpha_rule_id": int(alpha_rule.id),
            "beta_tenant_id": int(beta.id),
            "beta_bot_id": int(beta_bot.id),
            "beta_binding_id": int(beta_binding.id),
            "beta_channel_id": int(beta_channel.id),
            "beta_conversation_id": int(beta_conversation.id),
            "beta_message_id": int(beta_message.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(identity: dict[str, int], prefix: str = "alpha", **config) -> PluginExecutionRequest:
    """构造只包含服务端可信主键的消息执行请求。"""

    return PluginExecutionRequest(
        action="execute",
        config={"enabled": True, "allow_direct_messages": True, **config},
        payload={"content": "伪造内容不会参与规则匹配"},
        context=PluginExecutionContext(
            tenant_id=identity[f"{prefix}_tenant_id"],
            binding_id=identity[f"{prefix}_binding_id"],
            bot_id=identity[f"{prefix}_bot_id"],
            channel_id=identity[f"{prefix}_channel_id"],
            conversation_id=identity[f"{prefix}_conversation_id"],
            message_id=identity[f"{prefix}_message_id"],
            sender_id=f"{prefix}-user",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_fun_command_matches_standard_text_candidates_only() -> None:
    """观察器只接收已启用的标准文本，禁用私聊时不进入数据库查询。"""

    module = _load_plugin_module()
    group_context = type(
        "Context",
        (),
        {
            "binding_config": {"enabled": True},
            "chat_type": "group",
            "message_type": "text",
            "content": "来个节目",
        },
    )()
    direct_context = type(
        "Context",
        (),
        {
            "binding_config": {"enabled": True, "allow_direct_messages": False},
            "chat_type": "direct",
            "message_type": "text",
            "content": "来个节目",
        },
    )()

    assert module.matches_event(group_context)["matched"] is True
    assert module.matches_event(direct_context)["matched"] is False


def test_fun_command_uses_trusted_message_and_tenant_rule(fun_command_fixture) -> None:
    """规则匹配读取真实 Message，且甲租户不会读取乙租户同名规则。"""

    module = _load_plugin_module()
    factory, identity = fun_command_fixture
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, _request(identity))

    assert result.ok is True
    assert result.output["rule_id"] == identity["alpha_rule_id"]
    assert result.output["rule_name"] == "甲租户节目"
    assert result.output["replies"] == [
        {"type": "text", "content": "甲用户，节目开始"},
        {
            "type": "link",
            "url": "https://alpha.example.test/activity",
            "title": "甲群活动",
            "description": "由甲用户触发",
        },
    ]


def test_fun_command_rejects_cross_tenant_context(fun_command_fixture) -> None:
    """执行上下文混用其他租户主键时直接拒绝，不根据载荷回退查找。"""

    module = _load_plugin_module()
    factory, identity = fun_command_fixture
    forged = _request(identity)
    forged = PluginExecutionRequest(
        action=forged.action,
        config=forged.config,
        payload=forged.payload,
        context=PluginExecutionContext(
            tenant_id=identity["alpha_tenant_id"],
            binding_id=identity["alpha_binding_id"],
            bot_id=identity["alpha_bot_id"],
            channel_id=identity["beta_channel_id"],
            conversation_id=identity["alpha_conversation_id"],
            message_id=identity["alpha_message_id"],
            sender_id="alpha-user",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, forged)

    assert result.ok is False
    assert result.output["provider_status"] == "trusted_context_required"


def test_fun_command_media_stays_inside_tenant_directory(fun_command_fixture, tmp_path: Path, monkeypatch) -> None:
    """图片只能从当前租户媒体目录读取，目录穿越规则返回业务错误。"""

    module = _load_plugin_module()
    factory, identity = fun_command_fixture
    monkeypatch.setattr(module.settings, "data_dir", str(tmp_path / "data"))
    media_root = Path(module.settings.data_dir) / "tenants" / str(identity["alpha_tenant_id"]) / "fun_command"
    media_root.mkdir(parents=True)
    (media_root / "poster.png").write_bytes(b"tenant-image")

    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        rule = db.get(FunCommandRule, identity["alpha_rule_id"])
        assert rule is not None
        rule.replies_json = '[{"type":"image","path":"poster.png"}]'
        db.flush()
        success = module.execute_message_action(db, _request(identity))
        rule.replies_json = '[{"type":"image","path":"../outside.png"}]'
        db.flush()
        rejected = module.execute_message_action(db, _request(identity))

    assert success.ok is True
    assert success.output["replies"][0]["image_path"] == str((media_root / "poster.png").resolve())
    assert rejected.ok is False
    assert rejected.output["provider_status"] == "rule_invalid"


def test_fun_command_cooldown_is_shared_by_group(fun_command_fixture) -> None:
    """同一群规则在冷却窗口内只回复一次，后续消息静默跳过。"""

    module = _load_plugin_module()
    factory, identity = fun_command_fixture
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        rule = db.get(FunCommandRule, identity["alpha_rule_id"])
        assert rule is not None
        rule.cooldown_seconds = 60
        db.flush()
        first = module.execute_message_action(db, _request(identity))
        second = module.execute_message_action(db, _request(identity))

    assert first.ok is True
    assert first.output["provider_status"] == "matched"
    assert second.ok is True
    assert second.status == "skipped"
    assert second.output["provider_status"] == "cooldown"
    assert second.output["replies"] == []


def test_fun_command_trigger_is_unique_inside_binding(fun_command_fixture) -> None:
    """同一机器人不能创建同名触发文本，必须直接维护现有规则。"""

    factory, identity = fun_command_fixture
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        db.add(
            FunCommandRule(
                bot_plugin_id=identity["alpha_binding_id"],
                name="重复规则",
                trigger_text="来个",
                match_type="exact",
                replies_json='[{"type":"text","content":"重复"}]',
                priority=20,
                cooldown_seconds=0,
                is_enabled=True,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
