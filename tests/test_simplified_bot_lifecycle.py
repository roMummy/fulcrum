import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "simplified-bot-lifecycle"

# 本文件会直接导入全局数据库引擎和 FastAPI 应用，因此必须在任何 app 模块导入前
# 把运行目录锁定到一次性测试路径，避免契约测试误连开发库或未来的生产数据库。
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'test.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.models.entities import Bot, MessageActivityHourly, PluginActivityHourly
from app.schemas.forms import BotCreate, BotUpdate
from app.services.crud import create_bot, update_bot
from app.services.ops import build_monitoring_snapshot


DELETED_TABLES = {"bot_groups", "config_templates", "bot_tags", "bot_tag_links"}
DELETED_BOT_FIELDS = {"slug", "command_prefix", "group_id", "config_template_id", "environment"}


@pytest.fixture()
def db_session():
    """为服务层契约提供完全独立的内存数据库，并在测试结束后主动释放连接。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_removed_bot_metadata_is_absent_from_database_contract() -> None:
    """新 SaaS 数据库不能再生成机器人分组、模板、标签及其关联结构。"""

    assert DELETED_TABLES.isdisjoint(Base.metadata.tables)
    assert DELETED_BOT_FIELDS.isdisjoint(Bot.__table__.columns.keys())


def test_activity_tables_do_not_split_metrics_by_environment() -> None:
    """单一 SaaS 运行域直接按租户统计，不再维护无实际用途的环境维度。"""

    assert "environment" not in MessageActivityHourly.__table__.columns
    assert "environment" not in PluginActivityHourly.__table__.columns


def test_bot_forms_only_expose_business_fields() -> None:
    """创建只填写名称，编辑只允许维护名称和说明。"""

    assert set(BotCreate.model_fields) == {"name"}
    assert set(BotUpdate.model_fields) == {"name", "description"}


def test_create_and_update_bot_use_minimal_contract(db_session) -> None:
    """服务层必须为创建动作补齐固定状态，编辑动作只维护业务资料。"""

    bot = create_bot(db_session, BotCreate(name=" 客服机器人 "))

    assert bot.name == "客服机器人"
    assert bot.description is None
    assert bot.is_enabled is True
    assert bot.status == "running"

    updated = update_bot(
        db_session,
        bot,
        BotUpdate(name=" 售后机器人 ", description=" 处理售后咨询 "),
    )

    assert updated.name == "售后机器人"
    assert updated.description == "处理售后咨询"
    assert updated.is_enabled is True
    assert updated.status == "running"


def test_plugin_dispatch_uses_fixed_command_prefix() -> None:
    """企业微信 SaaS 统一使用斜杠命令，不再从机器人读取可配置前缀。"""

    source = (PROJECT_ROOT / "app/services/plugin_directory.py").read_text(encoding="utf-8")
    assert 'getattr(channel.bot, "command_prefix"' not in source
    assert 'command_prefix="/"' in source


def test_monitoring_snapshot_no_longer_requires_environment(db_session) -> None:
    """空数据库也应直接生成全租户监控快照，不再依赖环境解析缓存。"""

    snapshot = build_monitoring_snapshot(db_session)

    assert "environment" not in snapshot
    assert snapshot["stats"] == {
        "health_checks": 0,
        "open_alerts": 0,
        "audit_logs": 0,
        "messages_24h": 0,
        "webhook_failures_24h": 0,
    }
