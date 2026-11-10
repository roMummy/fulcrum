"""群详情里各标签的操作做完后,必须回到原来的标签,不能落到默认标签。"""

from __future__ import annotations

import inspect
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
    "update_wechat_ipad_chatroom_scope_config_action",
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
