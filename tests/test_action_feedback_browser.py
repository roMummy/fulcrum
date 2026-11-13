"""用真实共享模板和资源验证自动跟进、结果恢复及外部提交按钮。"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

from test_select_layout import _serve_page, browser  # noqa: F401

expect = pytest.importorskip("playwright.sync_api").expect
ROOT = Path(__file__).resolve().parents[1]


def _page(browser, width, *, failed=False):
    feedback = Environment(loader=FileSystemLoader(ROOT / "app/templates")).get_template("_action_feedback.html").render(notice="", notice_tone="info")
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="qa-token"><link rel="stylesheet" href="/static/css/style.css"><link rel="stylesheet" href="/static/css/directory-sync.css"></head><body class="app-shell"><main class="workspace"><section class="page-header"><h1>操作反馈验证</h1><div class="hero-action-cluster"><form action="/submit" method="post" data-action-feedback data-action-label="检查连接"><input type="hidden" name="csrf_token" value="qa-token"><button type="submit" id="inline-submit" class="btn">检查连接</button></form><form id="plugin-action" action="/submit" method="post" data-action-feedback data-action-label="刷新插件状态" hidden><input type="hidden" name="csrf_token" value="qa-token"></form><button type="submit" form="plugin-action" id="external-submit" class="btn btn-secondary">刷新插件状态</button></div></section>""" + feedback + """<section class="work-surface">这里是自动更新后的页面内容。</section></main><script src="/static/js/action-feedback.js" defer></script></body></html>"""
    state = {"done": failed, "polls": 0, "submits": 0, "navigations": 0}
    page = browser.new_page(viewport={"width": width, "height": 850}, reduced_motion="reduce")

    def serve(route):
        path = urlsplit(route.request.url).path
        if route.request.is_navigation_request():
            state["navigations"] += 1
        if path == "/submit":
            state["submits"] += 1
            assert route.request.headers["x-csrf-token"] == "qa-token"
            route.fulfill(json={"ok": True, "done": False, "summary": "已提交，等待执行", "jobs": [{"id": 5}], "status_url": "/channels/3/operations/status?job_ids=5"})
        elif path == "/channels/3/operations/status":
            state["polls"] += 1
            done = state["done"]
            route.fulfill(json={"ok": True, "done": done, "successful": int(done and not failed), "failed": int(done and failed), "summary": "手机离线，操作未完成" if failed else "插件状态已更新" if done else "手机正在执行", "jobs": [{"id": 5, "channel_name": "验证账号", "state": "failed" if failed else "completed" if done else "running", "message": "手机离线" if failed else "已完成" if done else "等待手机回报", "elapsed_seconds": 2}]})
        else:
            _serve_page(route, html)

    page.route("http://fulcrum.test/**", serve)
    page.goto("http://fulcrum.test/channels/3?tab=plugins")
    return page, state


@pytest.mark.parametrize("width", [1440, 390])
def test_external_button_tracks_task_and_refreshes_once(browser, width):
    page, state = _page(browser, width)
    try:
        page.locator("#external-submit").click()
        expect(page.locator("[data-action-monitor]")).to_have_attribute("data-state", "running")
        expect(page.locator("#external-submit")).to_have_text("处理中…")
        expect(page.locator("#external-submit")).to_be_disabled()
        expect(page.locator("#inline-submit")).to_be_disabled()
        assert state["submits"] == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_navigation(wait_until="domcontentloaded"):
            state["done"] = True
        expect(page.locator("[data-action-summary]")).to_have_text("插件状态已更新")
        expect(page.locator("[data-action-monitor]")).to_have_attribute("data-state", "success")
        expect(page.locator("#external-submit")).to_be_enabled()
        expect(page.locator("#external-submit")).to_have_text("刷新插件状态")
        page.wait_for_timeout(250)
        assert state["navigations"] == 2
        assert state["submits"] == 1
        assert not urlsplit(page.url).query.endswith("action_refreshed=1")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f"action-feedback-{width}.png"), full_page=True)
    finally:
        page.close()


def test_failed_task_shows_result_without_refresh(browser):
    page, state = _page(browser, 390, failed=True)
    try:
        page.locator("#inline-submit").click()
        expect(page.locator("[data-action-summary]")).to_have_text("手机离线，操作未完成")
        expect(page.locator("[data-action-monitor]")).to_have_attribute("data-state", "error")
        expect(page.locator("#inline-submit")).to_be_enabled()
        assert state["navigations"] == 1
        assert state["submits"] == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()
