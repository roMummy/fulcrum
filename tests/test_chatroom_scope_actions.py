"""群级插件开关的路由层回归：路由用的是租户身份 TenantPrincipal，操作人字段必须是 user_id。"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="scope-actions-test-logs-"))

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_chatroom_scope_routes_use_principal_user_id() -> None:
    """TenantPrincipal 只有 user_id，没有 id。

    线上停用群插件时报 AttributeError: 'TenantPrincipal' object has no attribute 'id'，
    原因是 21 处 operator_id=user.id 是旧管理员对象时代的残留。
    这里直接检查源码：群级开关路由里不能再出现 user.id。
    """

    source = MAIN.read_text(encoding="utf-8")
    assert not re.search(r"\buser\.id\b", source), "main.py 中不应再有 user.id（租户身份字段是 user_id）"
    assert source.count("operator_id=user.user_id") >= 21


def test_principal_has_no_id_attribute() -> None:
    """锁定前提：身份对象的主键字段叫 user_id；如果有人给它加了 id，上面的检查需要重新评估。"""

    from app.services.saas_auth import TenantPrincipal

    assert "user_id" in TenantPrincipal.__dataclass_fields__
    assert "id" not in TenantPrincipal.__dataclass_fields__
