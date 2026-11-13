"""真实页面提交好友和群同步，检查跳转、反馈与窄屏布局。"""

import os
import json
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn

expect = pytest.importorskip("playwright.sync_api").expect

from test_directory_sync import sync_context  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import browser  # noqa: F401
from app.models.entities import Chatroom, TaskJob, WorkWechatContact
from app.core.time import now_shanghai
from sqlalchemy import select

@pytest.fixture()
def directory_server(sync_context):
    client, _ids = sync_context
    # 真实监听隔离测试应用，验证浏览器原生表单提交及 303 跳转。
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", log_config=None, access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "浏览器测试服务未启动"
            yield base_url
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "浏览器测试服务未退出"


@pytest.mark.parametrize("path,label", [("/contacts", "同步好友"), ("/chatrooms", "同步群聊")])
def test_directory_sync_preserves_selected_account_and_mobile_layout(sync_context, directory_server, browser, path, label):
    client, ids = sync_context
    _login(client, "operator")
    account_id = ids["sync_channels"][1]
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    context.add_cookies([{"name": key, "value": value, "url": directory_server} for key, value in client.cookies.items()])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{directory_server}{path}?channel_id={account_id}")
        form = page.locator("[data-directory-sync]")
        assert form.locator('select[name="channel_id"]').input_value() == str(account_id)
        # 展开同步账号菜单时不挤动其他控件，页面内增强后的下拉保持原生提交值。
        trigger = form.locator(".ui-select-trigger")
        trigger.click()
        expect(form.locator(".ui-select-panel")).to_be_visible()
        page.keyboard.press("Escape")
        with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == path + "/sync") as submitted:
            form.get_by_role("button", name=label, exact=True).click()
        task_id = submitted.value.json()["jobs"][0]["id"]
        monitor = page.locator('[data-directory-sync-monitor]')
        expect(monitor).to_contain_text("等待手机领取")
        expect(form.get_by_role("button", name="同步中…", exact=True)).to_be_disabled()
        assert form.locator('select').evaluate('node => getComputedStyle(node).opacity') == '0'
        with ids["factory"]() as db:
            job = db.get(TaskJob, task_id)
            job.status = "running"
            job.locked_at = now_shanghai()
            db.commit()
        expect(monitor).to_contain_text("手机正在同步")
        expect(monitor).to_have_attribute("data-state", "running")
        expect(page.locator('[data-directory-sync-counter]')).to_have_text("已完成 0 / 1 个账号")
        assert monitor.evaluate('node => getComputedStyle(node).backgroundColor') == 'rgb(255, 255, 255)'
        assert page.locator('[data-directory-sync-progress]').bounding_box()['height'] == 4
        for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if os.environ.get("QA_SCREENSHOT_DIR"):
                directory = Path(os.environ["QA_SCREENSHOT_DIR"])
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / f"{path[1:]}-sync-running-{name}.png"), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        with page.expect_navigation():
            with ids["factory"]() as db:
                job = db.get(TaskJob, task_id)
                job.status = "completed"
                job.result_json = json.dumps({"webridge": {"ok": True, "result": {"items": [{"account_id": "synced_friend" if path == "/contacts" else "synced_room@chatroom"}]}}})
                if path == "/contacts":
                    db.add(WorkWechatContact(tenant_id=ids["alpha"], channel_id=account_id, contact_kind="friend", contact_external_id="synced_friend", name="自动更新的好友", display_name="自动更新的好友"))
                else:
                    db.add(Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=account_id, external_id="synced_room@chatroom", name="自动更新的群聊", management_status="enabled", is_active=True))
                db.commit()
        assert urlsplit(page.url).path == path
        expect(monitor).to_contain_text("同步完成")
        expect(monitor).to_have_attribute("data-state", "success")
        expect(page.locator(".work-surface").first).to_contain_text("自动更新的好友" if path == "/contacts" else "自动更新的群聊")
        expect(form.get_by_role("button", name=label, exact=True)).to_be_enabled()
        assert ids["notified"] == [account_id]
        assert page.locator('[data-directory-sync] select[name="channel_id"]').input_value() == str(account_id)
        select_box = form.locator(".ui-select-trigger").bounding_box()
        button_box = form.get_by_role("button", name=label, exact=True).bounding_box()
        assert abs(select_box["y"] + select_box["height"] / 2 - button_box["y"] - button_box["height"] / 2) < 2
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f"{path[1:]}-sync-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            page.screenshot(path=str(directory / f"{path[1:]}-sync-mobile.png"), full_page=True)
        assert not errors
    finally:
        context.close()


def test_pending_monitor_recovers_after_reload_without_resubmitting(sync_context, directory_server, browser):
    client, ids = sync_context
    _login(client, "operator")
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    context.add_cookies([{"name": key, "value": value, "url": directory_server} for key, value in client.cookies.items()])
    page = context.new_page()
    try:
        page.goto(f"{directory_server}/contacts")
        with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/contacts/sync") as submitted:
            page.locator('[data-directory-sync] button[type="submit"]').click()
        task_ids = [job["id"] for job in submitted.value.json()["jobs"]]
        expect(page.locator('[data-directory-sync-monitor]')).to_contain_text("等待手机领取")
        page.reload()
        expect(page.locator('[data-directory-sync-monitor]')).to_contain_text("等待手机领取")
        with ids["factory"]() as db:
            jobs = db.scalars(select(TaskJob)).all()
            assert {job.id for job in jobs} == set(task_ids)
            for job in jobs:
                job.status = "failed"
                job.error_message = "手机未连接"
            db.commit()
        expect(page.locator('[data-directory-sync-monitor]')).to_contain_text("同步未完成")
        expect(page.locator('[data-directory-sync-monitor]')).to_have_attribute("data-state", "error")
        assert page.locator('.directory-sync-track.is-indeterminate').count() == 0
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / 'contacts-sync-failed-mobile.png'), full_page=True)
        expect(page.locator('[data-directory-sync] button[type="submit"]')).to_be_enabled()
        assert ids["notified"] == ids["sync_channels"][:2]
        assert page.evaluate("JSON.parse(sessionStorage.getItem('fulcrum:directory-sync:/contacts')).ids") == task_ids
        page.get_by_role("button", name="收起", exact=True).click()
        expect(page.locator('[data-directory-sync-monitor]')).to_be_hidden()
        assert page.evaluate("sessionStorage.getItem('fulcrum:directory-sync:/contacts')") is None
    finally:
        context.close()
