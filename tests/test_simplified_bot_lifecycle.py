import os
from pathlib import Path


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


DELETED_TABLES = {"bot_groups", "config_templates", "bot_tags", "bot_tag_links"}
DELETED_BOT_FIELDS = {"slug", "command_prefix", "group_id", "config_template_id", "environment"}


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
