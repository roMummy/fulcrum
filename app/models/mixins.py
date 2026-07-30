from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import now_shanghai


class TimestampMixin:
    """为业务模型统一提供上海时区语义下的创建和更新时间。"""

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai)


class TenantOwnedMixin:
    """标记必须由租户感知 SQLAlchemy 会话强制隔离的业务模型。"""

    # tenant_id 由认证后的 tenant_scope 提供。普通创建流程可以不显式赋值，
    # before_flush 会自动补齐；若客户端伪造其他租户编号则在发出 SQL 前拒绝。
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )


# Task 7 的需求使用 TenantScopedMixin 名称，而既有实施计划和后续 Task 8
# 使用 TenantOwnedMixin。两者导出同一个类，避免后续模型迁移出现两套标记体系。
TenantScopedMixin = TenantOwnedMixin
