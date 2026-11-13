"""群详情里各标签的操作做完后,必须回到原来的标签,不能落到默认标签。"""

from __future__ import annotations

import inspect
from pathlib import Path
import os
import re
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="tab-redirect-logs-"))

import pytest

import app.main as main

# 「插件」标签里的群内插件开关:启用、停用、恢复继承、保存配置、切换模式。
PLUGIN_TAB_ACTIONS = [
    "enable_wechat_ipad_chatroom_scope_action",
    "disable_wechat_ipad_chatroom_scope_action",
    "reset_wechat_ipad_chatroom_scope_action",
    "set_wechat_ipad_chatroom_scope_mode_action",
]
DETAIL_URL = re.compile(r"chatrooms/\{chatroom_id\}\?([^\"']*)")


@pytest.mark.parametrize("name", PLUGIN_TAB_ACTIONS)
def test_plugin_scope_actions_stay_on_the_plugins_tab(name: str) -> None:
    source = inspect.getsource(getattr(main, name))
    redirects = DETAIL_URL.findall(source)
    assert redirects, f"{name} 没有跳转到群详情"
    for query in redirects:
        assert query.startswith("tab=plugins&"), f"{name} 的跳转没有停留在插件标签: ?{query[:40]}"


def test_group_plugin_tab_has_no_code_style_config_editor() -> None:
    """群详情的插件标签只保留"本群启用/停用 + 恢复默认":不再有 JSON 覆盖编辑器和技术信息,保存接口也已删除。"""

    template = (Path(__file__).resolve().parents[1] / "app" / "templates" / "chatroom_detail.html").read_text(encoding="utf-8")
    assert "config_override_json" not in template and "data-json-editor" not in template and "Binding #" not in template
    assert not hasattr(main, "update_wechat_ipad_chatroom_scope_config_action")
    assert not any(str(getattr(route, "path", "")).endswith("/scopes/{bot_plugin_id}/config") for route in main.app.routes)
