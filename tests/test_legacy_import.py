from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, create_engine, func, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _upgrade_target_database(database_path: Path) -> None:
    """通过正式迁移创建目标库，确保导入测试覆盖真实生产结构。"""

    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": f"sqlite:///{database_path}",
            "DATA_DIR": str(database_path.parent / "data"),
            "LOG_DIR": str(database_path.parent / "logs"),
            "AUTH_SEED_DEMO_USERS": "false",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _create_legacy_database(database_path: Path, *, channel_type: str = "workwechat") -> None:
    """创建只含首期迁移范围的旧库，旧主键故意使用不连续编号验证映射。"""

    metadata = MetaData()
    bots = Table(
        "bots",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(100), nullable=False),
        Column("description", Text),
        Column("status", String(20), nullable=False),
        Column("is_enabled", Boolean, nullable=False),
    )
    channels = Table(
        "channels",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("bot_id", ForeignKey("bots.id"), nullable=False),
        Column("name", String(100), nullable=False),
        Column("channel_type", String(50), nullable=False),
        Column("bot_token_encrypted", Text),
        Column("bot_token_masked", String(255)),
        Column("status", String(20), nullable=False),
        Column("is_enabled", Boolean, nullable=False),
    )
    plugins = Table(
        "plugins",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(100), nullable=False),
        Column("display_name", String(100), nullable=False),
    )
    bot_plugins = Table(
        "bot_plugins",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("bot_id", ForeignKey("bots.id"), nullable=False),
        Column("plugin_id", ForeignKey("plugins.id"), nullable=False),
        Column("status", String(20), nullable=False),
        Column("config_json", Text),
    )
    conversations = Table(
        "conversations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("bot_id", ForeignKey("bots.id"), nullable=False),
        Column("channel_id", ForeignKey("channels.id")),
        Column("channel_type", String(50), nullable=False),
        Column("external_id", String(128), nullable=False),
        Column("title", String(255)),
        Column("chat_type", String(20), nullable=False),
    )
    chatrooms = Table(
        "chatrooms",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("bot_id", ForeignKey("bots.id"), nullable=False),
        Column("channel_id", ForeignKey("channels.id"), nullable=False),
        Column("external_id", String(128), nullable=False),
        Column("name", String(255)),
    )
    contacts = Table(
        "workwechat_contacts",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("channel_id", ForeignKey("channels.id"), nullable=False),
        Column("contact_kind", String(30), nullable=False),
        Column("contact_external_id", String(128), nullable=False),
        Column("name", String(255)),
    )
    messages = Table(
        "messages",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("conversation_id", ForeignKey("conversations.id"), nullable=False),
        Column("direction", String(10), nullable=False),
        Column("content", Text, nullable=False),
        Column("message_type", String(30), nullable=False),
        Column("status", String(20), nullable=False),
    )
    task_jobs = Table(
        "task_jobs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("job_type", String(50), nullable=False),
        Column("status", String(20), nullable=False),
        Column("priority", Integer, nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("max_attempts", Integer, nullable=False),
        Column("available_at", DateTime(timezone=True), nullable=False),
        Column("related_type", String(50)),
        Column("related_id", Integer),
        Column("payload_json", Text, nullable=False),
    )

    engine = create_engine(f"sqlite:///{database_path}")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(bots.insert(), {"id": 10, "name": "旧企微机器人", "description": "历史数据", "status": "running", "is_enabled": True})
        connection.execute(
            channels.insert(),
            {
                "id": 20,
                "bot_id": 10,
                "name": "旧企微渠道",
                "channel_type": channel_type,
                "bot_token_encrypted": "ciphertext-only",
                "bot_token_masked": "****only",
                "status": "online",
                "is_enabled": True,
            },
        )
        connection.execute(plugins.insert(), {"id": 70, "code": "echo", "display_name": "回声"})
        connection.execute(bot_plugins.insert(), {"id": 80, "bot_id": 10, "plugin_id": 70, "status": "enabled", "config_json": '{"secret":"opaque"}'})
        connection.execute(conversations.insert(), {"id": 30, "bot_id": 10, "channel_id": 20, "channel_type": channel_type, "external_id": "conv-legacy", "title": "历史会话", "chat_type": "direct"})
        connection.execute(chatrooms.insert(), {"id": 40, "bot_id": 10, "channel_id": 20, "external_id": "room-legacy", "name": "历史群聊"})
        connection.execute(contacts.insert(), {"id": 50, "channel_id": 20, "contact_kind": "external", "contact_external_id": "contact-legacy", "name": "历史联系人"})
        connection.execute(messages.insert(), {"id": 60, "conversation_id": 30, "direction": "in", "content": "历史消息", "message_type": "text", "status": "received"})
        connection.execute(
            task_jobs.insert(),
            {
                "id": 90,
                "job_type": "legacy_delivery",
                "status": "pending",
                "priority": 100,
                "attempts": 0,
                "max_attempts": 3,
                "available_at": datetime(2026, 7, 30, 12, 0, 0),
                "related_type": "message",
                "related_id": 60,
                "payload_json": json.dumps({"bot_id": 10, "channel_id": 20, "conversation_id": 30, "message_id": 60}),
            },
        )
    engine.dispose()


def _prepare_target_database(database_path: Path):
    """创建目标租户、其他租户占位数据和全局插件，验证导入不依赖空主键空间。"""

    from app.models.entities import Bot, Plugin
    from app.models.saas import Tenant

    _upgrade_target_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        alpha = Tenant(code="alpha", name="自营租户", status="active")
        beta = Tenant(code="beta", name="其他租户", status="active")
        db.add_all([alpha, beta])
        db.flush()
        db.add(Bot(tenant_id=beta.id, name="其他租户占位机器人", status="stopped", is_enabled=True))
        db.add(Plugin(code="echo", display_name="回声", plugin_type="message", default_enabled=False))
        db.commit()
        return engine, factory, int(alpha.id), int(beta.id)


def test_dry_run_writes_nothing_and_formal_import_remaps_all_relationships(tmp_path: Path) -> None:
    """校验模式零写入；正式导入后核心资源和结构化任务引用全部归属目标租户。"""

    from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, Message, TaskJob, WorkWechatContact
    from app.models.saas import LegacyImportBatch
    from app.services.legacy_import import run_legacy_import

    source_path = tmp_path / "legacy.db"
    target_path = tmp_path / "target.db"
    _create_legacy_database(source_path)
    engine, factory, alpha_id, _beta_id = _prepare_target_database(target_path)
    source_url = f"sqlite:///{source_path}"

    dry_run = run_legacy_import(source_url=source_url, tenant_code="alpha", target_session_factory=factory, dry_run=True)
    assert dry_run["success"] is True
    assert dry_run["mode"] == "dry-run"
    assert all(item["written"] == 0 for item in dry_run["tables"].values())
    with factory() as db:
        assert db.scalar(select(func.count(Bot.id)).where(Bot.tenant_id == alpha_id)) == 0
        assert db.scalar(select(func.count(LegacyImportBatch.id)).where(LegacyImportBatch.tenant_id == alpha_id)) == 0

    report = run_legacy_import(source_url=source_url, tenant_code="alpha", target_session_factory=factory, dry_run=False)
    assert report["success"] is True
    assert report["mode"] == "import"
    assert report["tables"]["bots"] == {"read": 1, "written": 1, "skipped": 0, "errors": 0}
    assert "channel_credentials_require_rotation" in report["warnings"]
    assert "plugin_config_review_required" in report["warnings"]

    with factory() as db:
        bot = db.scalar(select(Bot).where(Bot.tenant_id == alpha_id))
        channel = db.scalar(select(Channel).where(Channel.tenant_id == alpha_id))
        contact = db.scalar(select(WorkWechatContact).where(WorkWechatContact.tenant_id == alpha_id))
        chatroom = db.scalar(select(Chatroom).where(Chatroom.tenant_id == alpha_id))
        conversation = db.scalar(select(Conversation).where(Conversation.tenant_id == alpha_id))
        message = db.scalar(select(Message).where(Message.tenant_id == alpha_id))
        task = db.scalar(select(TaskJob).where(TaskJob.tenant_id == alpha_id))
        binding = db.scalar(select(BotPlugin).where(BotPlugin.tenant_id == alpha_id))
        assert bot.id != 10
        assert channel.bot_id == bot.id
        assert channel.bot_token_encrypted == "ciphertext-only"
        assert contact.channel_id == channel.id
        assert chatroom.bot_id == bot.id and chatroom.channel_id == channel.id
        assert conversation.bot_id == bot.id and conversation.channel_id == channel.id
        assert message.conversation_id == conversation.id
        assert task.related_id == message.id
        assert json.loads(task.payload_json) == {"bot_id": bot.id, "channel_id": channel.id, "conversation_id": conversation.id, "message_id": message.id}
        assert binding.bot_id == bot.id and binding.plugin.code == "echo"
        assert db.scalar(select(func.count(LegacyImportBatch.id)).where(LegacyImportBatch.tenant_id == alpha_id)) == 1

    duplicate = run_legacy_import(source_url=source_url, tenant_code="alpha", target_session_factory=factory, dry_run=False)
    assert duplicate["success"] is False
    assert duplicate["errors"] == ["duplicate_import"]
    with factory() as db:
        assert db.scalar(select(func.count(Bot.id)).where(Bot.tenant_id == alpha_id)) == 1
    engine.dispose()


def test_unsupported_channel_fails_before_target_write(tmp_path: Path) -> None:
    """SaaS 首期之外的旧渠道必须在预检阶段拒绝，不能绕过产品边界写入目标库。"""

    from app.models.entities import Bot
    from app.services.legacy_import import run_legacy_import

    source_path = tmp_path / "legacy-unsupported.db"
    target_path = tmp_path / "target-unsupported.db"
    _create_legacy_database(source_path, channel_type="telegram")
    engine, factory, alpha_id, _beta_id = _prepare_target_database(target_path)

    report = run_legacy_import(
        source_url=f"sqlite:///{source_path}",
        tenant_code="alpha",
        target_session_factory=factory,
        dry_run=False,
    )
    assert report["success"] is False
    assert report["errors"] == ["unsupported_channel_type:telegram"]
    with factory() as db:
        assert db.scalar(select(func.count(Bot.id)).where(Bot.tenant_id == alpha_id)) == 0
    engine.dispose()


def test_cli_dry_run_writes_machine_readable_report(tmp_path: Path, monkeypatch) -> None:
    """命令行 dry-run 返回成功退出码，并把不含来源连接串的 JSON 报告原子落盘。"""

    import scripts.import_legacy_tenant as cli

    source_path = tmp_path / "legacy-cli.db"
    target_path = tmp_path / "target-cli.db"
    report_path = tmp_path / "reports" / "dry-run.json"
    _create_legacy_database(source_path)
    engine, factory, _alpha_id, _beta_id = _prepare_target_database(target_path)
    monkeypatch.setattr(cli, "SessionLocal", factory)

    exit_code = cli.main(
        [
            "--source-url",
            f"sqlite:///{source_path}",
            "--tenant-code",
            "alpha",
            "--report-path",
            str(report_path),
            "--dry-run",
        ]
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["success"] is True
    assert report["mode"] == "dry-run"
    assert "source_url" not in report
    assert "sqlite:///" not in report_path.read_text(encoding="utf-8")
    engine.dispose()
