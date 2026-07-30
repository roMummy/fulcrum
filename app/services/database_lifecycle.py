from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from app.core.settings import settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_alembic_config(database_url: str | None = None) -> Config:
    """构造与当前项目绑定的 Alembic 配置，供启动检查和维护脚本复用。"""

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # Alembic 的 ConfigParser 会解释百分号；转义后仍能把带 URL 编码的生产密码原样传递。
    resolved_url = (database_url or settings.database_url).strip()
    config.set_main_option("sqlalchemy.url", resolved_url.replace("%", "%%"))
    return config


def expected_database_revision() -> str:
    """返回项目唯一的迁移 head，发现分叉时直接拒绝继续启动。"""

    heads = ScriptDirectory.from_config(build_alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"database_revision_heads_invalid:{','.join(heads)}")
    return heads[0]


def assert_database_at_head(connection: Connection) -> None:
    """拒绝在数据库版本落后时启动业务 Worker，避免新代码写坏旧结构。"""

    current_revision = MigrationContext.configure(connection).get_current_revision()
    expected_revision = expected_database_revision()
    if current_revision != expected_revision:
        raise RuntimeError(f"database_revision_mismatch:{current_revision}:{expected_revision}")
