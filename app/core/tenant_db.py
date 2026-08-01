from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.tenant_context import TenantScopeError, require_tenant_id
from app.models.mixins import TenantOwnedMixin


SessionFactory = Callable[[], Session]

# 事件监听器注册在 SQLAlchemy Session 基类上，但只有 tenant_session() 显式写入
# 标记后才会生效。这样测试注入的 sessionmaker 也能复用隔离规则，同时原有
# SessionLocal、Alembic 和平台管理会话仍保留不受租户过滤的显式旁路。
_SESSION_MODE_KEY = "multi_bot_saas_session_mode"
_SESSION_TENANT_ID_KEY = "multi_bot_saas_bound_tenant_id"
_TENANT_MODE = "tenant"
_PLATFORM_MODE = "platform"


def _default_tenant_session_factory() -> SessionFactory:
    """延迟导入默认工厂，避免 app.core.db 与本模块形成初始化循环。"""

    from app.core.db import TenantSessionLocal

    return TenantSessionLocal


def _default_platform_session_factory() -> SessionFactory:
    """返回平台和迁移使用的非租户会话工厂。"""

    from app.core.db import SessionLocal

    return SessionLocal


def _open_session(session_factory: SessionFactory) -> Session:
    """统一校验注入工厂，避免错误对象绕过 SQLAlchemy 事件约束。"""

    db = session_factory()
    if not isinstance(db, Session):
        close = getattr(db, "close", None)
        if callable(close):
            close()
        raise TypeError("session_factory 必须返回 SQLAlchemy Session")
    return db


def open_tenant_session(session_factory: SessionFactory | None = None) -> Session:
    """打开并返回绑定当前可信租户的会话，由调用方负责关闭。"""

    # Web 路由沿用项目现有的 try/finally + db.close() 生命周期，无法直接持有
    # contextmanager 对象。这里复用与 tenant_session() 完全相同的绑定逻辑，确保
    # 旧路由只需替换会话入口即可获得查询过滤和写入校验，不必一次性重写数十个页面。
    tenant_id = require_tenant_id()
    factory = session_factory or _default_tenant_session_factory()
    db = _open_session(factory)
    db.info[_SESSION_MODE_KEY] = _TENANT_MODE
    db.info[_SESSION_TENANT_ID_KEY] = tenant_id
    return db


def _require_bound_tenant(db: Session) -> int:
    """校验会话创建时绑定的租户与当前 ContextVar 仍保持一致。"""

    current_tenant_id = require_tenant_id()
    bound_tenant_id = db.info.get(_SESSION_TENANT_ID_KEY)
    if bound_tenant_id != current_tenant_id:
        # 同一 Session 不能跨 tenant_scope 复用，否则 identity map 可能保留上一
        # 租户已经加载的实体。请求和后台任务都应为每个租户建立独立会话。
        raise TenantScopeError("租户数据库会话不能跨租户上下文复用")
    return current_tenant_id


def _is_tenant_owned_mapper(execute_state: ORMExecuteState) -> bool:
    """判断 ORM DML 的目标映射是否属于租户业务模型。"""

    mapper = execute_state.bind_mapper
    return mapper is not None and issubclass(mapper.class_, TenantOwnedMixin)


def _update_changes_tenant_id(statement: Any, parameters: Any) -> bool:
    """识别 ORM 批量更新中对 tenant_id 的赋值，禁止普通业务转移归属。"""

    # SQLAlchemy 暂未为 Update 暴露公共的赋值列枚举接口；_values 和
    # _ordered_values 是 2.x 编译阶段使用的稳定内部结构。这里只读取列键，
    # 不改写语句内部状态，参数化值仍完全交由 SQLAlchemy 处理。
    statement_values = getattr(statement, "_values", None) or {}
    if any(getattr(column, "key", column) == "tenant_id" for column in statement_values):
        return True

    ordered_values = getattr(statement, "_ordered_values", None) or ()
    if any(getattr(column, "key", column) == "tenant_id" for column, _value in ordered_values):
        return True

    parameter_sets: list[dict[str, Any]] = []
    if isinstance(parameters, dict):
        parameter_sets.append(parameters)
    elif isinstance(parameters, (list, tuple)):
        parameter_sets.extend(item for item in parameters if isinstance(item, dict))
    return any("tenant_id" in item for item in parameter_sets)


@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_orm_statement(execute_state: ORMExecuteState) -> None:
    """为租户会话的 ORM 查询和批量修改统一附加可信租户条件。"""

    db = execute_state.session
    if db.info.get(_SESSION_MODE_KEY) != _TENANT_MODE:
        return

    tenant_id = _require_bound_tenant(db)
    if execute_state.is_select:
        # 顶层 SELECT 增加一次 loader criteria 即可；该选项会自动传播到懒加载
        # 和 eager load，重复给关系/列加载追加条件会制造冗余 SQL。
        if not execute_state.is_column_load and not execute_state.is_relationship_load:
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(
                    TenantOwnedMixin,
                    lambda model: model.tenant_id == tenant_id,
                    include_aliases=True,
                )
            )
        return

    if execute_state.is_insert and _is_tenant_owned_mapper(execute_state):
        # Session.execute(insert(Model)) 不会把对象放入 Session.new，因此无法
        # 通过 before_flush 自动补 tenant_id 或逐条核验归属。租户业务新增统一
        # 使用 db.add()/add_all()；确需批量导入时只能走经过审计的平台旁路。
        raise TenantScopeError("租户业务会话禁止对租户模型执行 ORM 批量新增")

    if not (execute_state.is_update or execute_state.is_delete):
        return
    if not _is_tenant_owned_mapper(execute_state):
        return

    if execute_state.is_update and _update_changes_tenant_id(
        execute_state.statement,
        execute_state.parameters,
    ):
        raise TenantScopeError("租户业务会话禁止通过批量更新修改 tenant_id")

    # ORM 批量 UPDATE/DELETE 不经过 before_flush，因此必须在执行事件中补上
    # tenant_id 条件。即使调用方遗漏过滤，也只可能影响当前可信租户的数据。
    mapper = execute_state.bind_mapper
    execute_state.statement = execute_state.statement.where(
        mapper.class_.tenant_id == tenant_id
    )


@event.listens_for(Session, "before_flush")
def _validate_tenant_owned_writes(db: Session, _flush_context: Any, _instances: Any) -> None:
    """在 ORM flush 前自动补归属，并拒绝跨租户新增、修改和删除。"""

    if db.info.get(_SESSION_MODE_KEY) != _TENANT_MODE:
        return

    tenant_id = _require_bound_tenant(db)

    for instance in db.new:
        if not isinstance(instance, TenantOwnedMixin):
            continue
        owner_id = getattr(instance, "tenant_id", None)
        if owner_id is None:
            # 只使用可信 ContextVar 自动补值，从不使用表单、JSON 或 URL 中的
            # tenant_id；父资源归属校验仍由对应业务服务在 Task 9 中完成。
            instance.tenant_id = tenant_id
        elif owner_id != tenant_id:
            raise TenantScopeError("禁止为当前租户新增其他租户归属的数据")

    for instance in db.dirty:
        if not isinstance(instance, TenantOwnedMixin):
            continue
        state = inspect(instance)
        if state.attrs.tenant_id.history.has_changes():
            raise TenantScopeError("租户业务对象创建后禁止修改 tenant_id")
        if getattr(instance, "tenant_id", None) != tenant_id:
            raise TenantScopeError("禁止修改其他租户归属的数据")

    for instance in db.deleted:
        if isinstance(instance, TenantOwnedMixin) and getattr(instance, "tenant_id", None) != tenant_id:
            raise TenantScopeError("禁止删除其他租户归属的数据")


@contextmanager
def tenant_session(session_factory: SessionFactory | None = None) -> Iterator[Session]:
    """创建绑定当前可信 tenant_scope 的租户感知 SQLAlchemy 会话。"""

    # 在打开连接前即要求可信作用域，确保“缺少租户上下文”默认拒绝，并避免
    # 无效请求白白占用连接池。一个会话从创建到关闭只能服务这个 tenant_id。
    db = open_tenant_session(session_factory)
    try:
        yield db
    except BaseException:
        # flush 或业务逻辑失败后主动回滚，调用方无需处理 PendingRollbackError；
        # close 仍在 finally 中执行，保证连接一定归还连接池。
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def platform_session(session_factory: SessionFactory | None = None) -> Iterator[Session]:
    """创建平台管理、认证预检或迁移专用的显式非租户会话。"""

    factory = session_factory or _default_platform_session_factory()
    db = _open_session(factory)
    db.info[_SESSION_MODE_KEY] = _PLATFORM_MODE
    db.info.pop(_SESSION_TENANT_ID_KEY, None)
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
