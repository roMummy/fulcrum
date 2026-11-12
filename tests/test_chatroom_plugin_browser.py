"""通过真实页面验证跨页选择、表单提交及保存后的群状态。"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

from app.models.entities import Chatroom, GroupPluginScope
from test_chatroom_plugin_batch import batch_context  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _serve_application(route, client):
    url = urlsplit(route.request.url)
    if url.path.startswith("/static/"):
        asset = ROOT / "app" / url.path.lstrip("/")
        if asset.is_file():
            route.fulfill(path=str(asset))
            return
    response = client.request(
        route.request.method,
        url.path + ("?" + url.query if url.query else ""),
        content=route.request.post_data,
        headers={name: value for name, value in route.request.headers.items() if name in {"content-type", "x-requested-with"}},
        follow_redirects=False,
    )
    route.fulfill(status=response.status_code, body=response.content, headers={name: value for name, value in response.headers.items() if name in {"content-type", "location"}})


def test_selects_across_real_pages_and_saves_current_states(batch_context, browser):
    client, ids = batch_context
    _login(client, "operator")
    with ids["factory"]() as db:
        rooms = [Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id=f"browser-{i}@chatroom", name=f"分页测试 {i:02d}", management_status="enabled", is_active=True) for i in range(32)]
        db.add_all(rooms)
        db.flush()
        room_id = rooms[0].id
        db.add(GroupPluginScope(tenant_id=ids["alpha"], channel_id=ids["channel_id"], chatroom_id=room_id, bot_plugin_id=ids["bindings"][0], enabled=False, scope_status="overridden"))
        db.commit()
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve_application(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://fulcrum.test/chatrooms/plugin-scopes?plugin_id={ids['plugin']}")
        first = page.locator(f'input[data-scope-room][value="{room_id}"]')
        row = first.locator("xpath=ancestor::tr")
        assert "已禁用" in row.inner_text() and "单独设置" in row.inner_text()
        first.check()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 1 个群（本页 1 个）")
        page.get_by_role("link", name="下一页").click()
        second = page.locator("input[data-scope-room]:not(:disabled)").first
        second_id = second.input_value()
        assert second_id != str(room_id)
        expect(page.locator("[data-selection-count]")).to_have_text("已选 1 个群（本页 0 个）")
        second.check()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 2 个群（本页 1 个）")
        page.get_by_role("link", name="上一页").click()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 2 个群（本页 1 个）")
        assert first.is_checked()
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "group-plugin-management-desktop.png"), full_page=True)
        with page.expect_navigation():
            page.get_by_role("button", name="批量启用", exact=True).click()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 0 个群（本页 0 个）")
        assert "已启用" in row.inner_text()
        assert not first.is_checked()
        with ids["factory"]() as db:
            from sqlalchemy import select
            scopes = db.scalars(select(GroupPluginScope).where(GroupPluginScope.chatroom_id.in_([room_id, int(second_id)]), GroupPluginScope.bot_plugin_id == ids["bindings"][0])).all()
            assert len(scopes) == 2 and all(scope.enabled for scope in scopes)
        page.set_viewport_size({"width": 390, "height": 844})
        if os.environ.get("QA_SCREENSHOT_DIR"):
            page.screenshot(path=str(directory / "group-plugin-management-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("Array.from(document.querySelectorAll('body *')).filter(node=>{const box=node.getBoundingClientRect();return box.right>innerWidth+1 && getComputedStyle(node).position!=='absolute'}).slice(0,16).map(node=>({tag:node.tagName,class:node.className,width:node.getBoundingClientRect().width,right:node.getBoundingClientRect().right}))")
        assert not errors
    finally:
        context.close()


def test_selects_all_matching_rooms_and_excludes_one_across_pages(batch_context, browser):
    from sqlalchemy import select

    client, ids = batch_context
    _login(client, "operator")
    with ids["factory"]() as db:
        rooms = [Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id=f"all-browser-{i}@chatroom", name=f"全量测试{i:03d}", management_status="enabled", is_active=True) for i in range(353)]
        db.add_all(rooms)
        db.add(Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="all-browser-left@chatroom", name="全量测试退出", status="left"))
        db.commit()
        room_ids = [room.id for room in rooms]
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    context.route("http://fulcrum.test/**", lambda route: _serve_application(route, client))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://fulcrum.test/chatrooms/plugin-scopes?plugin_id={ids['plugin']}&q=全量测试")
        select_all = page.get_by_role("button", name="全选筛选结果（353 个群）", exact=True)
        select_all.click()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 353 个群（本页 30 个）")
        page.locator(f'input[data-scope-room][value="{room_ids[0]}"]').uncheck()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 352 个群（本页 29 个）")
        page.get_by_role("link", name="下一页").click()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 352 个群（本页 30 个）")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "select-all-matching-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(directory / "select-all-matching-mobile.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_navigation():
            page.get_by_role("button", name="批量禁用", exact=True).click()
        expect(page.locator("[data-selection-count]")).to_have_text("已选 0 个群（本页 0 个）")
        assert "已在 352 个群聊中禁用" in page.inner_text("body")
        assert "已禁用" in page.locator("tbody tr").first.inner_text()
        with ids["factory"]() as db:
            scopes = db.scalars(select(GroupPluginScope).where(GroupPluginScope.bot_plugin_id == ids["bindings"][0], GroupPluginScope.chatroom_id.in_(room_ids))).all()
            assert len(scopes) == 352 and all(not scope.enabled for scope in scopes)
            assert room_ids[0] not in {scope.chatroom_id for scope in scopes}
            assert db.get(GroupPluginScope, ids["scope"]).enabled is False
        assert not errors
    finally:
        context.close()
