from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from app.core.db import Base
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


def current_database_revision(connection: Connection) -> str | None:
    """读取当前连接的 Alembic revision，空库或旧库未建版本表时返回空。"""

    return MigrationContext.configure(connection).get_current_revision()


def prepare_development_database(connection: Connection) -> None:
    """仅为开发和测试临时库创建模型结构并盖上当前 head 标记。

    生产部署必须执行 ``alembic upgrade head``，不能调用此辅助函数。这里保留
    显式开发能力，是为了让大量使用临时 SQLite 的聚焦测试无需启动外部数据库，
    但已存在任意 revision 的数据库绝不会被本函数自动升级或覆盖。
    """

    if not settings.is_dev_like:
        raise RuntimeError("development_database_helper_disabled")
    context = MigrationContext.configure(connection)
    if context.get_current_revision() is not None:
        return
    Base.metadata.create_all(bind=connection)
    script = ScriptDirectory.from_config(build_alembic_config())
    context.stamp(script, expected_database_revision())
