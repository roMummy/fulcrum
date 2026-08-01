import threading

from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.logging import get_logger
from app.models import entities  # noqa: F401
from app.models import saas  # noqa: F401
from app.services.database_lifecycle import (
    assert_database_at_head,
    current_database_revision,
    expected_database_revision,
    prepare_development_database,
)
from app.services.installer import ensure_bootstrap_platform_admin
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.runtime import backfill_conversation_channels
from app.services.seed import seed_auth_users

logger = get_logger(__name__)
_init_lock = threading.Lock()
_initialized = False
_database_ready = False
_database_error = "database_not_checked"


def database_readiness() -> dict[str, object]:
    """返回不含连接串和凭证的数据库就绪摘要，供 readyz 和中间件使用。"""

    return {
        "ready": _database_ready,
        "error": "" if _database_ready else _database_error,
        "expected_revision": expected_database_revision(),
    }


def reset_database_lifecycle_state() -> None:
    """重置进程内生命周期状态；主要供启动前和隔离测试使用。"""

    global _initialized, _database_ready, _database_error
    _initialized = False
    _database_ready = False
    _database_error = "database_not_checked"


def mark_database_unready(error: object) -> None:
    """记录数据库未就绪原因，不包含连接串或异常参数中的敏感配置。"""

    global _database_ready, _database_error
    _database_ready = False
    _database_error = str(error or "database_not_ready")[:500]


def init_db(db: Session) -> None:
    """兼容旧调用名称，实际只执行 revision 校验和应用数据初始化。"""

    init_db_with_seed(db)


def init_db_with_seed(db: Session):
    global _initialized, _database_ready, _database_error
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        logger.info("checking database migration revision")
        try:
            connection = db.connection()
            # 开发和测试允许显式辅助创建全新临时库；已有旧 revision 仍必须拒绝，
            # 生产环境则永远不会进入 create_all 辅助分支。
            if settings.is_dev_like and current_database_revision(connection) is None:
                prepare_development_database(connection)
            assert_database_at_head(connection)
            seed_auth_users(db)
            ensure_bootstrap_platform_admin(db)
            sync_plugins_from_directory(db)
            backfill_conversation_channels(db)
        except Exception as exc:
            db.rollback()
            mark_database_unready(exc)
            logger.error("database lifecycle check failed", extra={"database_error": _database_error})
            raise
        _initialized = True
        _database_ready = True
        _database_error = ""
        logger.info("database lifecycle ready", extra={"database_revision": expected_database_revision()})
