"""页面导航与图标约定:返回按钮统一在页头左侧、只用图标;模板里用到的图标都必须存在。

背景:返回按钮原来各页各写各的,放在右侧操作区、文字也不统一("返回列表/返回订单/返回规则列表……"),
导航流程混乱。现在统一由 base.html 的 {% block back %} 渲染在标题左边。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="page-nav-logs-"))

from tests.test_provider_node_pages import _login, provider_page_context  # noqa: F401  (含 fixture)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
ICONS_CSS = (ROOT / "app" / "static" / "css" / "icons.css").read_text(encoding="utf-8")


def test_back_links_are_icon_buttons_in_the_header_not_text_buttons() -> None:
    offenders = []
    for path in TEMPLATES.glob("*.html"):
        if path.name == "auth_base.html":  # 登录页的"返回首页"是营销站入口,不属于后台二级页面
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"<a\b[^>]*>\s*(?:←\s*)?返回[^<]*</a>", text):
            offenders.append(path.name)
    assert offenders == [], f"这些模板仍把返回写成文字按钮,请改用 {{% block back %}}: {offenders}"


def test_every_icon_class_used_in_templates_is_defined() -> None:
    defined = set(re.findall(r"\.i-([a-z0-9-]+)\s*\{", ICONS_CSS))
    used = set()
    for path in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r'class="[^"]*\bi i-([a-z0-9-]+)', path.read_text(encoding="utf-8")))
    assert used - defined == set(), f"模板用了没定义的图标: {sorted(used - defined)}"
    assert {"arrow-left", "refresh", "filter", "x"} <= defined


def test_the_back_button_renders_before_the_page_title(provider_page_context) -> None:
    client, _ids = provider_page_context
    _login(client, "admin")
    html = client.get("/plugins/runtime").text
    back = html.index('class="page-back"')
    assert back < html.index("<h1") and 'href="/plugins"' in html[back: back + 200] and "i-arrow-left" in html[back: back + 300]
    assert "/static/css/icons.css" in html
    # 右侧操作区不再有"返回xx"文字按钮
    actions = html[html.index('class="page-header-actions"'): html.index("</section>", html.index('class="page-header-actions"'))]
    assert "返回" not in actions
