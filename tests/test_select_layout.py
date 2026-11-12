"""在真实浏览器中验证长弹窗展开下拉不会挤出或滚动其他输入框。"""

from pathlib import Path
import os
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app.models.entities import Plugin
from test_plugin_detail_page import pages  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def browser():
    executable = next((path for path in (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ) if path.exists()), None)
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True, **({"executable_path": str(executable)} if executable else {}))
        except playwright.Error as exc:
            pytest.skip(f"没有可用的浏览器：{exc}")
        yield instance
        instance.close()


def _serve_page(route, html):
    path = urlsplit(route.request.url).path
    if path.startswith("/static/"):
        asset = ROOT / "app" / path.lstrip("/")
        if asset.is_file():
            route.fulfill(path=str(asset))
            return
    route.fulfill(status=200, content_type="text/html", body=html)


@pytest.mark.parametrize("size", [(1440, 900), (1280, 600), (390, 844)])
def test_binding_dropdown_keeps_long_modal_layout_and_scroll_position(pages, browser, size):
    client, ids, _plugin_id, _binding_id = pages
    _login(client, "admin")
    with ids["factory"]() as db:
        plugin_id = db.scalar(select(Plugin.id).where(Plugin.code == "maibot_adapter"))
    response = client.get(f"/plugins/{plugin_id}")
    assert response.status_code == 200
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve_page(route, response.text))
    page = context.new_page()
    try:
        page.goto(f"http://fulcrum.test/plugins/{plugin_id}")
        page.get_by_role("button", name="绑定到机器人", exact=True).click()
        modal = page.locator("#create-plugin-binding-modal")
        card = modal.locator(".app-modal-card")
        before = card.evaluate("node => ({top:node.getBoundingClientRect().top,height:node.clientHeight,scrollTop:node.scrollTop,scrollHeight:node.scrollHeight})")
        positions = modal.locator("input:not([type=hidden]):not([type=checkbox])").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().top)")
        trigger = modal.locator('select[name="group_default_enabled"]').locator("..").locator(".ui-select-trigger")
        trigger.click()
        panel = modal.locator(".ui-select-open .ui-select-panel")
        assert panel.is_visible()
        after = card.evaluate("node => ({top:node.getBoundingClientRect().top,height:node.clientHeight,scrollTop:node.scrollTop,overflow:getComputedStyle(node).overflowY})")
        assert after["overflow"] == "auto"
        assert after["height"] <= size[1] - 48
        assert before["scrollHeight"] > before["height"]
        assert abs(before["top"] - after["top"]) < 1
        assert before["height"] == after["height"]
        assert before["scrollTop"] == after["scrollTop"]
        assert positions == modal.locator("input:not([type=hidden]):not([type=checkbox])").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().top)")
        panel_box, card_box = panel.bounding_box(), card.bounding_box()
        assert panel_box["y"] >= card_box["y"]
        assert panel_box["y"] + panel_box["height"] <= card_box["y"] + card_box["height"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f"binding-dropdown-{size[0]}x{size[1]}.png"))
        panel.get_by_text("否，稍后在群管理中选择群聊", exact=True).click()
        assert modal.locator('select[name="group_default_enabled"]').input_value() == "false"
        assert not panel.is_visible()
        trigger.click()
        card.evaluate("node => {node.scrollTop += 80}")
        panel.wait_for(state="hidden")
    finally:
        context.close()


def test_dropdown_near_modal_bottom_opens_up_and_only_scrolls_its_options(browser):
    css = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    script = (ROOT / "app/static/js/select.js").read_text(encoding="utf-8")
    options = "".join(f'<option value="{i}" {"selected" if i == 19 else ""}>选项 {i}</option>' for i in range(20))
    html = f'<style>{css}</style><dialog class="app-modal"><div class="app-modal-card"><div style="height:800px">长表单</div><label>底部下拉<select>{options}</select></label></div></dialog>'
    page = browser.new_page(viewport={"width": 1280, "height": 600}, reduced_motion="reduce")
    try:
        page.set_content(html)
        page.add_script_tag(content=script)
        page.locator("dialog").evaluate("node => node.showModal()")
        card = page.locator(".app-modal-card")
        card.evaluate("node => {node.scrollTop = node.scrollHeight}")
        page.locator(".ui-select-trigger").click()
        panel = page.locator(".ui-select-panel")
        assert "ui-select-up" in page.locator(".ui-select").get_attribute("class")
        before = card.evaluate("node => node.scrollTop")
        for _ in range(19):
            page.locator(".ui-select-trigger").press("ArrowUp")
        assert card.evaluate("node => node.scrollTop") == before
        assert panel.evaluate("node => node.scrollTop <= node.firstElementChild.offsetTop")
        page.locator(".ui-select-trigger").press("Escape")
        assert not panel.is_visible()
    finally:
        page.close()
