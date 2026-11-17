"""群级插件开关的路由层回归：路由用的是租户身份 TenantPrincipal，操作人字段必须是 user_id。"""

from __future__ import annotations

import os
import ast
import re
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="scope-actions-test-logs-"))

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_chatroom_scope_routes_use_principal_user_id() -> None:
    """TenantPrincipal 只有 user_id，没有 id。

    线上停用群插件曾读取旧身份的 id；检查当前路由中每个实际写入点，
    不能依赖历史接口数量，否则删除旧接口也会误报回归。
    """

    source = MAIN.read_text(encoding="utf-8")
    assert not re.search(r"\buser\.id\b", source), "main.py 中不应再有 user.id（租户身份字段是 user_id）"
    tree = ast.parse(source)
    checked_routes = set()
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        operators = [keyword.value for call in ast.walk(node) if isinstance(call, ast.Call)
            for keyword in call.keywords if keyword.arg == "operator_id"]
        for operator in operators:
            assert ast.unparse(operator) == "user.user_id", f"{node.name} 的操作人必须使用可信租户身份 user_id"
        if operators:
            checked_routes.add(node.name)
    assert {
        "enable_wechat_ipad_chatroom_scope_action", "disable_wechat_ipad_chatroom_scope_action",
        "reset_wechat_ipad_chatroom_scope_action", "set_wechat_ipad_chatroom_scope_mode_action",
    } <= checked_routes


def test_principal_has_no_id_attribute() -> None:
    """锁定前提：身份对象的主键字段叫 user_id；如果有人给它加了 id，上面的检查需要重新评估。"""

    from app.services.saas_auth import TenantPrincipal

    assert "user_id" in TenantPrincipal.__dataclass_fields__
    assert "id" not in TenantPrincipal.__dataclass_fields__
