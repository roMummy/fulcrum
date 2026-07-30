from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.db import Base
from app.core.settings import settings
from app.models import entities  # noqa: F401
from app.models import saas  # noqa: F401


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic 必须读取与应用相同的模型元数据。这里显式导入 entities，确保所有业务表在
# autogenerate 和空库升级时都被注册，同时不会把已经删除的机器人旧表重新带入基线。
target_metadata = Base.metadata


def _database_url() -> str:
    """优先使用部署环境传入的连接串，避免迁移误连开发默认数据库。"""

    return os.getenv("DATABASE_URL", settings.database_url).strip()


def _configure_context(connection=None, *, url: str | None = None) -> None:
    """集中声明线上和离线迁移共用的比较规则。"""

    options = {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
    }
    if connection is not None:
        # SQLite 后续执行 ALTER 时需要 batch 模式；MariaDB 会按原生 DDL 正常执行。
        options["render_as_batch"] = connection.dialect.name == "sqlite"
        context.configure(connection=connection, **options)
        return
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **options)


def run_migrations_offline() -> None:
    """在不创建数据库连接的场景下生成 SQL 迁移脚本。"""

    _configure_context(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连接目标数据库并在事务中执行迁移。"""

    # ConfigParser 会把百分号当作插值标记，因此写回配置前需要转义连接串中的百分号。
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _configure_context(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
