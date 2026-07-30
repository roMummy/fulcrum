from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.settings import settings

MYSQL_TIME_ZONE = "+08:00"


def _configure_engine(engine):
    database_url = settings.database_url.strip().lower()
    if not (database_url.startswith("mysql") or database_url.startswith("mariadb")):
        return engine

    @event.listens_for(engine, "connect")
    def _set_mysql_timezone(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET time_zone = '{MYSQL_TIME_ZONE}'")
        finally:
            cursor.close()

    @event.listens_for(engine, "checkout")
    def _verify_mysql_timezone(dbapi_connection, _connection_record, _connection_proxy):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET time_zone = '{MYSQL_TIME_ZONE}'")
        finally:
            cursor.close()

    return engine


def _build_engine():
    database_url = settings.database_url.strip()
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
        engine = create_engine(
            database_url,
            connect_args=connect_args,
            echo=settings.database_echo,
        )
        return _configure_engine(engine)

    connect_args = {"init_command": f"SET time_zone = '{MYSQL_TIME_ZONE}'"}
    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=max(30, settings.database_pool_recycle_seconds),
        pool_size=max(1, settings.database_pool_size),
        max_overflow=max(0, settings.database_max_overflow),
        pool_timeout=max(1, settings.database_pool_timeout_seconds),
        echo=settings.database_echo,
    )
    return _configure_engine(engine)


engine = _build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
