from __future__ import annotations

import pytest
from sqlalchemy import Integer, String, create_engine, insert, select, update
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from app.core.db import Base
from app.core.tenant_context import TenantScopeError, get_current_tenant_id, tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.mixins import TenantOwnedMixin, TenantScopedMixin
from app.models.saas import Tenant


class TenantScopedProbe(Base, TenantOwnedMixin):
    """仅用于验证会话隔离机制，不提前改动任何正式业务模型。"""

    __tablename__ = "tenant_scoped_probes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


@pytest.fixture()
def session_factory(tmp_path):
    """每个用例使用独立 SQLite 数据库，避免租户数据在测试之间串扰。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'tenant-scope.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def tenant_pair(session_factory):
    """通过显式非租户会话创建两个租户，作为隔离边界的基础数据。"""

    with platform_session(session_factory) as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        db.add_all([alpha, beta])
        db.commit()
        return alpha, beta


def _create_probe_unscoped(session_factory, tenant_id: int, name: str) -> TenantScopedProbe:
    """平台旁路只用于准备测试数据，租户业务代码不得照此绕过隔离会话。"""

    with platform_session(session_factory) as db:
        row = TenantScopedProbe(tenant_id=tenant_id, name=name)
        db.add(row)
        db.commit()
        return row


def test_tenant_scope_restores_outer_trusted_context() -> None:
    """嵌套作用域退出后必须恢复外层租户，避免异步请求之间遗留错误上下文。"""

    assert get_current_tenant_id() is None

    with tenant_scope(11):
        assert get_current_tenant_id() == 11
        with tenant_scope(22):
            assert get_current_tenant_id() == 22
        assert get_current_tenant_id() == 11

    assert get_current_tenant_id() is None


def test_tenant_session_requires_a_trusted_scope(session_factory) -> None:
    """没有认证链建立的可信租户上下文时，租户会话必须默认拒绝创建。"""

    with pytest.raises(TenantScopeError):
        with tenant_session(session_factory):
            pass


def test_scoped_session_cannot_load_other_tenant_row(session_factory, tenant_pair) -> None:
    """主键已知也不能读取其他租户记录，查询结果统一表现为不存在。"""

    alpha, beta = tenant_pair
    alpha_row = _create_probe_unscoped(session_factory, alpha.id, "alpha-row")
    beta_row = _create_probe_unscoped(session_factory, beta.id, "beta-row")

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        assert db.get(TenantScopedProbe, alpha_row.id) is not None
        assert db.get(TenantScopedProbe, beta_row.id) is None
        visible_rows = db.scalars(select(TenantScopedProbe)).all()
        assert [(row.id, row.name) for row in visible_rows] == [(alpha_row.id, "alpha-row")]


def test_scoped_session_fills_missing_tenant_on_insert(session_factory, tenant_pair) -> None:
    """新对象未显式提供归属时由可信上下文补齐，不能采信客户端 tenant_id。"""

    alpha, _beta = tenant_pair

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        row = TenantScopedProbe(name="auto-owned")
        db.add(row)
        db.commit()
        assert row.tenant_id == alpha.id

    with platform_session(session_factory) as db:
        stored = db.scalar(select(TenantScopedProbe).where(TenantScopedProbe.name == "auto-owned"))
        assert stored is not None
        assert stored.tenant_id == alpha.id


def test_scoped_session_rejects_cross_tenant_insert(session_factory, tenant_pair) -> None:
    """客户端显式伪造其他租户编号时，在 SQL 发出前拒绝新增。"""

    alpha, beta = tenant_pair

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        db.add(TenantScopedProbe(tenant_id=beta.id, name="wrong-owner"))
        with pytest.raises(TenantScopeError):
            db.flush()


def test_scoped_session_rejects_bulk_insert_that_bypasses_flush(session_factory, tenant_pair) -> None:
    """ORM 批量新增不经过 before_flush，租户会话必须明确拒绝该旁路。"""

    alpha, beta = tenant_pair

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        statement = insert(TenantScopedProbe).values(
            tenant_id=beta.id,
            name="bulk-wrong-owner",
        )
        with pytest.raises(TenantScopeError):
            db.execute(statement)


def test_scoped_session_rejects_cross_tenant_object_update(session_factory, tenant_pair) -> None:
    """即使其他租户对象由平台会话传入，租户会话也不能修改该对象。"""

    alpha, beta = tenant_pair
    beta_row = _create_probe_unscoped(session_factory, beta.id, "beta-row")

    # 先取得脱离平台会话的对象，模拟服务层误把跨租户实体传给当前请求。
    with platform_session(session_factory) as db:
        detached = db.get(TenantScopedProbe, beta_row.id)
        assert detached is not None
        db.expunge(detached)

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        detached.name = "forbidden-update"
        db.add(detached)
        with pytest.raises(TenantScopeError):
            db.flush()


def test_scoped_session_rejects_tenant_id_change(session_factory, tenant_pair) -> None:
    """对象一旦持久化，其 tenant_id 不允许通过普通业务更新转移归属。"""

    alpha, beta = tenant_pair
    alpha_row = _create_probe_unscoped(session_factory, alpha.id, "alpha-row")

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        row = db.get(TenantScopedProbe, alpha_row.id)
        assert row is not None
        row.tenant_id = beta.id
        with pytest.raises(TenantScopeError):
            db.flush()


def test_scoped_bulk_update_is_limited_to_current_tenant(session_factory, tenant_pair) -> None:
    """ORM 批量更新同样追加租户条件，不能因绕过对象加载而影响其他租户。"""

    alpha, beta = tenant_pair
    alpha_row = _create_probe_unscoped(session_factory, alpha.id, "before-alpha")
    beta_row = _create_probe_unscoped(session_factory, beta.id, "before-beta")

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        result = db.execute(update(TenantScopedProbe).values(name="updated"))
        db.commit()
        assert result.rowcount == 1

    with platform_session(session_factory) as db:
        assert db.get(TenantScopedProbe, alpha_row.id).name == "updated"
        assert db.get(TenantScopedProbe, beta_row.id).name == "before-beta"


def test_platform_session_is_an_explicit_unscoped_bypass(session_factory, tenant_pair) -> None:
    """平台管理和迁移旁路可见全部租户数据，但必须通过独立入口显式使用。"""

    alpha, beta = tenant_pair
    _create_probe_unscoped(session_factory, alpha.id, "alpha-row")
    _create_probe_unscoped(session_factory, beta.id, "beta-row")

    with platform_session(session_factory) as db:
        assert len(db.scalars(select(TenantScopedProbe)).all()) == 2

    # 两个名称作为同一混入的兼容导出，便于后续模型迁移按既有计划逐步接入。
    assert TenantScopedMixin is TenantOwnedMixin
