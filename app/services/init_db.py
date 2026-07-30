import threading

from sqlalchemy.orm import Session

from app.core.db import Base, engine
from app.core.logging import get_logger
from app.models import entities  # noqa: F401
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.runtime import backfill_conversation_channels, ensure_runtime_schema
from app.services.seed import seed_auth_users

logger = get_logger(__name__)
_init_lock = threading.Lock()
_initialized = False


def init_db():
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        logger.info("initializing database schema")
        Base.metadata.create_all(bind=engine)
        ensure_runtime_schema()
        logger.info("database schema initialized")
        _initialized = True


def init_db_with_seed(db: Session):
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        logger.info("initializing database with seed data")
        Base.metadata.create_all(bind=engine)
        ensure_runtime_schema()
        logger.info("database schema initialized")
        seed_auth_users(db)
        sync_plugins_from_directory(db)
        backfill_conversation_channels(db)
        logger.info("database initialization with seed data completed")
        _initialized = True
