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
# SessionLocal 保留为平台管理、认证预检、Alembic 和旧代码使用的显式非租户
# 工厂。普通租户业务必须通过 tenant_session()/get_tenant_db() 打开会话，不能
# 直接把 SessionLocal 当作租户隔离边界。
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# 独立命名的工厂便于依赖注入和测试替换；真正的隔离标记由 tenant_session()
# 在每个会话实例上写入，避免影响同一引擎上的平台和迁移操作。
TenantSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """返回历史非租户会话；新租户路由应改用 get_tenant_db。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_tenant_db():
    """为已经建立 tenant_scope 的请求提供强制隔离会话。"""

    # 延迟导入避免 db.py 初始化 Base 和 sessionmaker 时产生循环依赖；FastAPI
    # 实际解析依赖时模块均已加载完成。
    from app.core.tenant_db import tenant_session

    with tenant_session(TenantSessionLocal) as db:
        yield db
