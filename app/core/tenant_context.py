from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class TenantScopeError(RuntimeError):
    """租户上下文缺失、不可信或与当前数据库会话不一致。"""


# ContextVar 会按线程和异步任务分别保存值，适合在 FastAPI 请求以及后台任务中
# 传递认证后得到的 tenant_id。这里不提供任何从请求参数直接赋值的快捷入口，
# 调用方必须先完成租户会话、设备密钥或渠道密钥校验，再建立可信作用域。
_CURRENT_TENANT_ID: ContextVar[int | None] = ContextVar(
    "multi_bot_saas_current_tenant_id",
    default=None,
)


def _validate_tenant_id(tenant_id: int) -> int:
    """只接受正整数主键，显式拒绝 Python 中也属于 int 的布尔值。"""

    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
        raise TenantScopeError("tenant_id 必须是经过认证的正整数主键")
    return tenant_id


def get_current_tenant_id() -> int | None:
    """返回当前可信租户编号；未建立作用域时返回 None。"""

    return _CURRENT_TENANT_ID.get()


def require_tenant_id() -> int:
    """取得当前可信租户编号，缺少上下文时默认拒绝继续访问数据库。"""

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise TenantScopeError("当前操作缺少可信租户上下文")
    return _validate_tenant_id(tenant_id)


@contextmanager
def tenant_scope(tenant_id: int) -> Iterator[int]:
    """在当前请求或任务内临时绑定可信租户，并在退出时精确恢复外层值。"""

    trusted_tenant_id = _validate_tenant_id(tenant_id)
    token = _CURRENT_TENANT_ID.set(trusted_tenant_id)
    try:
        yield trusted_tenant_id
    finally:
        # 使用 ContextVar.reset(token) 而不是简单写回 None，才能正确支持嵌套
        # 作用域，并确保异常退出时也不会污染随后复用的线程或异步任务。
        _CURRENT_TENANT_ID.reset(token)
