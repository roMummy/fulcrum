"""用真实浏览器验证好友逐个检测、动态归类、停止和网络重试。"""

import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

from test_friend_check_pages import friend_check_context  # noqa: F401
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
        headers={name: value for name, value in route.request.headers.items() if name in {"content-type", "x-requested-with", "x-csrf-token"}},
        follow_redirects=False,
    )
    route.fulfill(status=response.status_code, body=response.content, headers={name: value for name, value in response.headers.items() if name in {"content-type", "location"}})


def _open_detector(browser, client, channel_id, *, transient_error=False, unsafe_name=False, seedable=False):
    """只模拟手机查询的结果，页面、账号选择及静态资源使用真实应用。"""
    contacts = [
        {"id": index + 1, "external_id": f"wxid_browser_friend_{index}", "name": name, "remark": f"备注 {index + 1}",
         "avatar_url": ["/avatar/friend.svg", "", "/avatar/missing.svg"][index]}
        for index, name in enumerate(["<img src=x onerror=window.contactInjected=1>" if unsafe_name else "陈小明", "李晓雨", "王小海"])
    ]
    state = {"contacts": contacts, "starts": [], "polls": 0, "hold": True, "transient_error": transient_error,
             "active": 0, "max_active": 0, "actions": [], "action_jobs": {}, "action_hold": False,
             "recheck_category": None, "poll_failures": 0, "action_poll_failures": 0}
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")

    def serve(route):
        path = urlsplit(route.request.url).path
        if seedable and path == "/static/js/contact-check.js":
            script = (ROOT / "app/static/js/contact-check.js").read_text(encoding="utf-8")
            # 仅测试注入大数据，继续使用生产分页/选择/操作函数，避免几千次模拟手机请求。
            script = script.replace("  resetResults();\n})();", """  resetResults();
  window.seedContactResults = (results) => {
    results.forEach((item) => { tables[item.category].rows.push(item); tables[item.category].lookup.set(String(item.contact.id), item); });
    categories.forEach((category) => renderCategory(category));
  };
})();""")
            route.fulfill(content_type="application/javascript", body=script)
            return
        if path == "/avatar/friend.svg":
            route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" fill="#8fbcbb"/><circle cx="24" cy="18" r="9" fill="#fff"/><path d="M8 48V40a16 16 0 0 1 32 0v8" fill="#fff"/></svg>')
            return
        if path == "/avatar/missing.svg":
            route.fulfill(status=404, body="missing avatar")
            return
        if path == f"/contacts/check/{channel_id}/friends":
            route.fulfill(json={"contacts": contacts, "total": len(contacts)})
            return
        action_match = re.fullmatch(rf"/contacts/check/{channel_id}/friends/(\d+)/(remark|delete)", path)
        if action_match:
            assert route.request.method == "POST" and route.request.headers.get("x-csrf-token")
            import json
            payload = json.loads(route.request.post_data)
            contact_id, action = int(action_match.group(1)), action_match.group(2)
            job_id = 1000 + len(state["actions"])
            state["actions"].append((contact_id, action, payload))
            state["action_jobs"][job_id] = (contact_id, action, payload)
            route.fulfill(json={"job_id": job_id})
            return
        action_poll = re.fullmatch(rf"/contacts/check/{channel_id}/actions/(\d+)/poll", path)
        if action_poll:
            assert route.request.method == "POST" and route.request.headers.get("x-csrf-token")
            if state["action_poll_failures"]:
                state["action_poll_failures"] -= 1
                route.abort("connectionreset")
                return
            if state["action_hold"]:
                route.fulfill(json={"state": "running", "result": None})
                return
            contact_id, action, payload = state["action_jobs"][int(action_poll.group(1))]
            contact = next((item.copy() for item in contacts if item["id"] == contact_id), {"id": contact_id, "name": f"好友{contact_id}", "remark": f"备注{contact_id}"})
            if action == "remark":
                contact["remark"] = (contact.get("remark") or contact.get("name")) + payload["suffix"]
            route.fulfill(json={"state": "completed", "result": {"ok": True, "action": action, "contact": contact, "message": "操作成功"}})
            return
        start_match = re.fullmatch(rf"/contacts/check/{channel_id}/friends/(\d+)", path)
        if start_match:
            assert route.request.method == "POST" and route.request.headers.get("x-csrf-token")
            contact_id = int(start_match.group(1))
            state["starts"].append(contact_id)
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            route.fulfill(json={"job_id": 100 + len(state["starts"])})
            return
        poll_match = re.fullmatch(rf"/contacts/check/{channel_id}/jobs/(\d+)/poll", path)
        if poll_match:
            assert route.request.method == "POST" and route.request.headers.get("x-csrf-token")
            state["polls"] += 1
            if state["poll_failures"]:
                state["poll_failures"] -= 1
                route.abort("connectionreset")
                return
            if state["transient_error"]:
                state["transient_error"] = False
                route.abort("connectionreset")
                return
            if state["hold"]:
                route.fulfill(json={"state": "running", "result": None})
                return
            contact_id = state["starts"][int(poll_match.group(1)) - 101]
            category = state["recheck_category"] or ["friend", "deleted", "unknown"][(contact_id - 1) % 3]
            contact = next((item for item in contacts if item["id"] == contact_id), {"id": contact_id, "name": f"好友{contact_id}", "remark": f"备注{contact_id}", "external_id": f"wxid_{contact_id}"})
            state["active"] -= 1
            route.fulfill(json={"state": "completed", "result": {
                "contact": contact, "category": category,
                "pay_name_last_char": "明" if category == "friend" else "",
                "reason": {"friend": "查询成功", "deleted": "未查询到姓名末字", "unknown": "查询失败"}[category],
            }})
            return
        _serve_application(route, client)

    context.route("http://fulcrum.test/**", serve)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"http://fulcrum.test/contacts/check?channel_id={channel_id}")
    page.locator("#contact-check-interval").fill("0")
    return context, page, state, errors


def test_empty_tables_sequential_results_restart_and_responsive_layout(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"])
    try:
        expect(page.locator("[data-result-body] tr")).to_have_count(0)
        assert page.locator("[data-result-count]").all_text_contents() == ["0", "0", "0"]
        with page.expect_response(lambda response: response.url.endswith("/poll")):
            page.get_by_role("button", name="开始检测", exact=True).click()
        assert state["starts"] == [1]
        expect(page.locator("[data-result-body] tr")).to_have_count(0)
        assert page.locator("#contact-check-start").is_disabled()
        assert page.locator("#contact-check-channel").is_disabled()
        state["hold"] = False
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        for category in ("friend", "deleted", "unknown"):
            expect(page.locator(f'[data-result-body="{category}"] tr')).to_have_count(1)
            expect(page.locator(f'[data-result-count="{category}"]')).to_have_text("1")
        assert state["starts"] == [1, 2, 3] and state["max_active"] == 1
        avatar = page.locator('[data-result-body="friend"] .identity-avatar img')
        expect(avatar).to_have_count(1)
        expect(avatar).to_have_attribute("referrerpolicy", "no-referrer")
        assert avatar.evaluate("node => node.complete && node.naturalWidth > 0")
        expect(page.locator('[data-result-body="friend"] .identity-name')).to_have_text("陈小明")
        expect(page.locator('[data-result-body="deleted"] .identity-avatar')).to_have_text("李")
        expect(page.locator('[data-result-body="unknown"] .identity-avatar')).to_have_text("王")
        assert page.locator('[data-result-body="unknown"] .identity-avatar img').count() == 0
        assert page.evaluate("window.contactInjected") is None
        assert page.locator("[data-row-action]").evaluate_all("buttons => buttons.every(button => button.getBoundingClientRect().height <= 40)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "friend-check-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            page.screenshot(path=str(directory / "friend-check-mobile.png"), full_page=True)
        state["hold"] = True
        with page.expect_response(lambda response: response.url.endswith("/poll")):
            page.get_by_role("button", name="重新开始", exact=True).click()
            page.get_by_role("button", name="清空并重新开始", exact=True).click()
        expect(page.locator("[data-result-body] tr")).to_have_count(0)
        assert page.locator("[data-result-count]").all_text_contents() == ["0", "0", "0"]
        assert state["starts"] == [1, 2, 3, 1]
        state["hold"] = False
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        assert state["starts"] == [1, 2, 3, 1, 2, 3] and state["max_active"] == 1
        assert not errors
    finally:
        context.close()


def test_contact_name_is_text_and_unsupported_avatar_url_uses_initial(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"], unsafe_name=True)
    state["contacts"][0]["avatar_url"] = "javascript:window.contactInjected=1"
    state["hold"] = False
    try:
        page.get_by_role("button", name="开始检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        expect(page.locator('[data-result-body="friend"] .identity-name')).to_have_text("<img src=x onerror=window.contactInjected=1>")
        assert page.locator('[data-result-body="friend"] img').count() == 0
        expect(page.locator('[data-result-body="friend"] .identity-avatar')).to_have_text("<")
        assert page.evaluate("window.contactInjected") is None
        assert not errors
    finally:
        context.close()


def test_stop_waits_for_current_friend_and_does_not_start_next(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"])
    try:
        with page.expect_response(lambda response: response.url.endswith("/poll")):
            page.get_by_role("button", name="开始检测", exact=True).click()
        account = page.locator("#contact-check-channel")
        expect(account).to_be_disabled()
        assert account.evaluate("node => getComputedStyle(node).opacity") == "0"
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "friend-check-running.png"))
        page.get_by_role("button", name="停止检测", exact=True).click()
        expect(page.locator("#contact-check-stop")).to_have_text("等待当前检测完成…")
        assert page.locator("#contact-check-start").is_disabled()
        assert state["starts"] == [1]
        state["hold"] = False
        expect(page.locator("#contact-check-status")).to_have_text("已暂停，保留 1 位结果，剩余 2 位未检测。")
        expect(page.locator("[data-result-body] tr")).to_have_count(1)
        assert state["starts"] == [1] and not errors
        elapsed = page.locator("#contact-check-elapsed").text_content()
        page.wait_for_timeout(1100)
        expect(page.locator("#contact-check-elapsed")).to_have_text(elapsed)
        page.get_by_role("button", name="继续检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        assert state["starts"] == [1, 2, 3]
        expect(page.locator("[data-result-body] tr")).to_have_count(3)
    finally:
        context.close()


def test_pause_during_interval_cancels_next_dispatch_and_resume_keeps_cursor(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"])
    state["hold"] = False
    try:
        page.locator("#contact-check-interval").fill("2")
        page.get_by_role("button", name="开始检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("已检测 1 / 3，等待下一位…")
        page.get_by_role("button", name="停止检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("已暂停，保留 1 位结果，剩余 2 位未检测。")
        page.wait_for_timeout(2200)
        assert state["starts"] == [1]
        page.locator("#contact-check-interval").fill("0")
        page.get_by_role("button", name="继续检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        assert state["starts"] == [1, 2, 3] and state["max_active"] == 1
        assert not errors
    finally:
        context.close()


def test_poll_exhaustion_resume_uses_existing_job(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"])
    state["poll_failures"] = 4
    try:
        page.get_by_role("button", name="开始检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_contain_text("检测已暂停", timeout=10000)
        assert state["starts"] == [1]
        assert page.locator("#contact-check-channel").is_disabled()
        assert page.locator("#contact-check-restart").is_disabled()
        state["hold"] = False
        page.get_by_role("button", name="继续检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        assert state["starts"] == [1, 2, 3] and state["max_active"] == 1
        assert not errors
    finally:
        context.close()


def _seed_results(page, count=2501):
    page.evaluate("""count => {
      const results = [];
      ['friend', 'deleted', 'unknown'].forEach((category, categoryIndex) => {
        for (let index = 0; index < count; index += 1) {
          const id = categoryIndex * 10000 + index + 1;
          results.push({category, contact: {id, name: `好友${id}`, remark: `备注${id}`, external_id: `wxid_${id}`},
            pay_name_last_char: category === 'friend' ? '明' : '', reason: category === 'friend' ? '已取得姓名末字' : '未取得姓名末字'});
        }
      });
      window.seedContactResults(results);
    }""", count)


def test_thousands_results_render_current_page_and_preserve_cross_page_selection(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"], seedable=True)
    try:
        _seed_results(page)
        expect(page.locator("[data-result-body] tr")).to_have_count(150)
        assert page.locator("[data-result-count]").all_text_contents() == ["2501"] * 3
        page.locator('[data-result-body="deleted"] [data-select-contact]').first.check()
        page.locator('[data-page-next="deleted"]').click()
        expect(page.locator('[data-page-label="deleted"]')).to_have_text("2 / 51")
        page.locator('[data-result-body="deleted"] [data-select-contact]').first.check()
        expect(page.locator('[data-selection-count="deleted"]')).to_have_text("已选 2 位")
        page.locator('[data-page-prev="deleted"]').click()
        expect(page.locator('[data-result-body="deleted"] [data-select-contact]').first).to_be_checked()
        expect(page.locator('[data-page-label="unknown"]')).to_have_text("1 / 51")
        page.locator('[data-page-size="friend"]').select_option("100")
        expect(page.locator('[data-result-body="friend"] tr')).to_have_count(100)
        expect(page.locator("[data-result-body] tr")).to_have_count(200)
        page.locator('[data-select-all="deleted"]').click()
        expect(page.locator('[data-selection-count="deleted"]')).to_have_text("已选 2501 位")
        page.locator('[data-clear-selection="deleted"]').click()
        expect(page.locator('[data-selection-count="deleted"]')).to_have_text("已选 0 位")
        page.locator('[data-select-page="unknown"]').check()
        expect(page.locator('[data-selection-count="unknown"]')).to_have_text("已选 50 位")
        assert not state["starts"] and not state["actions"] and not errors
    finally:
        context.close()


def test_recheck_stays_in_original_row_and_remark_uses_default_suffix(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"])
    state["hold"] = False
    try:
        page.get_by_role("button", name="开始检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("检测完成，共 3 位好友")
        deleted_row = page.locator('[data-result-body="deleted"] tr')
        state["recheck_category"] = "friend"
        deleted_row.locator('[data-row-action="recheck"]').click()
        expect(page.locator("#contact-check-action-status")).to_contain_text("状态变化 1 位（保留在原表）")
        expect(deleted_row).to_have_count(1)
        expect(deleted_row).to_contain_text("复查：互相是好友（状态变化）")
        expect(page.locator('[data-result-count="friend"]')).to_have_text("1")
        assert state["starts"] == [1, 2, 3, 2]
        deleted_row.locator('[data-row-action="remark"]').click()
        expect(page.locator("#contact-check-remark-suffix")).to_have_value("- 单删")
        expect(page.locator("#contact-check-remark-preview")).to_have_text("备注 2- 单删")
        page.get_by_role("button", name="确认添加", exact=True).click()
        expect(page.locator("#contact-check-action-status")).to_have_text("添加备注后缀完成：成功 1 位。")
        expect(deleted_row).to_contain_text("备注 2- 单删")
        assert state["actions"] == [(2, "remark", {"suffix": "- 单删"})]
        assert not errors
    finally:
        context.close()


def test_bulk_delete_requires_count_confirmation_and_keeps_rows(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"], seedable=True)
    try:
        _seed_results(page, 51)
        page.locator('[data-result-body="deleted"] [data-select-contact]').first.check()
        page.locator('[data-page-next="deleted"]').click()
        page.locator('[data-result-body="deleted"] [data-select-contact]').first.check()
        page.locator('[data-batch-action="delete"][data-category="deleted"]').click()
        expect(page.locator("#app-dialog-title")).to_have_text("删除 2 位微信好友？")
        expect(page.locator("#app-dialog-message")).to_contain_text("从微信通讯录实际删除所选的 2 位好友")
        assert not state["actions"]
        confirm = page.get_by_role("button", name="确认删除 2 位好友", exact=True)
        expect(confirm).to_be_disabled()
        page.locator('.app-dialog [data-role="cancel"]').click()
        assert not state["actions"]
        page.locator('[data-batch-action="delete"][data-category="deleted"]').click()
        page.locator('.app-dialog [data-role="match-input"]').fill("2")
        confirm.click()
        expect(page.locator("#contact-check-action-status")).to_have_text("删除好友完成：成功 2 位。")
        assert state["actions"] == [(10001, "delete", {"confirmed": True}), (10051, "delete", {"confirmed": True})]
        expect(page.locator('[data-result-count="deleted"]')).to_have_text("51")
        expect(page.locator('[data-result-body="deleted"] tr')).to_contain_text("已删除微信好友")
        expect(page.locator('[data-result-body="deleted"] [data-row-action="delete"]')).to_be_disabled()
        expect(page.locator('[data-selection-count="deleted"]')).to_have_text("已选 0 位")
        assert not errors
    finally:
        context.close()


def test_action_poll_failure_resumes_same_job_and_locks_conflicting_actions(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"], seedable=True)
    try:
        _seed_results(page, 1)
        state["action_poll_failures"] = 4
        page.locator('[data-result-body="deleted"] [data-row-action="remark"]').click()
        page.get_by_role("button", name="确认添加", exact=True).click()
        expect(page.locator("#contact-check-action-resume")).to_be_visible(timeout=10000)
        assert page.locator("#contact-check-start").is_disabled()
        assert page.locator('[data-result-body="unknown"] [data-row-action="recheck"]').is_disabled()
        assert len(state["actions"]) == 1
        page.get_by_role("button", name="继续查询操作结果", exact=True).click()
        expect(page.locator("#contact-check-action-status")).to_have_text("添加备注后缀完成：成功 1 位。")
        assert len(state["actions"]) == 1
        expect(page.locator("#contact-check-start")).to_be_enabled()
        assert not errors
    finally:
        context.close()


def test_cross_page_bulk_recheck_and_remark_are_sequential_and_keep_categories(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"], seedable=True)
    state["hold"] = False
    state["recheck_category"] = "friend"
    try:
        _seed_results(page, 51)
        page.locator('[data-result-body="unknown"] [data-select-contact]').first.check()
        page.locator('[data-page-next="unknown"]').click()
        page.locator('[data-result-body="unknown"] [data-select-contact]').first.check()
        page.locator('[data-batch-action="recheck"][data-category="unknown"]').click()
        expect(page.locator("#contact-check-action-status")).to_contain_text("状态变化 2 位（保留在原表）")
        assert state["starts"] == [20001, 20051] and state["max_active"] == 1
        assert page.locator("[data-result-count]").all_text_contents() == ["51"] * 3
        expect(page.locator('[data-result-body="unknown"] tr')).to_contain_text("复查：互相是好友（状态变化）")
        page.locator('[data-batch-action="remark"][data-category="unknown"]').click()
        expect(page.locator("#contact-check-remark-count")).to_contain_text("2 位好友")
        page.locator("#contact-check-remark-suffix").fill("- 待核实")
        page.get_by_role("button", name="确认添加", exact=True).click()
        expect(page.locator("#contact-check-action-status")).to_have_text("添加备注后缀完成：成功 2 位。")
        assert state["actions"] == [(20001, "remark", {"suffix": "- 待核实"}), (20051, "remark", {"suffix": "- 待核实"})]
        expect(page.locator('[data-result-body="unknown"] tr')).to_contain_text("备注20051- 待核实")
        assert not errors
    finally:
        context.close()


def test_transient_poll_failure_retries_same_job_without_new_command(friend_check_context, browser):
    client, ids = friend_check_context
    _login(client, "operator")
    context, page, state, errors = _open_detector(browser, client, ids["bridge_channel"], transient_error=True)
    try:
        page.get_by_role("button", name="开始检测", exact=True).click()
        expect(page.locator("#contact-check-status")).to_have_text("网络暂时中断，正在重试当前任务结果…")
        assert state["starts"] == [1]
        page.get_by_role("button", name="停止检测", exact=True).click()
        state["hold"] = False
        expect(page.locator("#contact-check-status")).to_have_text("已暂停，保留 1 位结果，剩余 2 位未检测。")
        assert state["starts"] == [1] and state["polls"] == 2
        expect(page.locator('[data-result-body="friend"] tr')).to_have_count(1)
        assert not errors
    finally:
        context.close()
