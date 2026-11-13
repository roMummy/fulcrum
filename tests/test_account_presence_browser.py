"""验证账号在线标识在桌面和手机宽度下可见且不溢出。"""

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.time import now_shanghai
from app.models.entities import Channel
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_select_layout import _serve_page, browser  # noqa: F401


@pytest.mark.parametrize("width", [1440, 390])
def test_presence_badges_and_activity_fit_account_rows(provider_page_context, browser, width):
    client, ids = provider_page_context
    now = now_shanghai()
    with ids["factory"]() as db:
        db.add_all([
            Channel(
                tenant_id=ids["alpha"], bot_id=ids["bot"], name=name,
                channel_type="telegram" if status == "degraded" else "wechat_webridge", is_enabled=enabled, status=status,
                runtime_info_json=json.dumps({"webridge": {"online": True, "last_seen_at": (now - timedelta(seconds=age)).isoformat()}}),
            )
            for name, enabled, status, age in (
                ("客服微信", True, "online", 10),
                ("售后微信", True, "online", 300),
                ("暂停使用的微信账号", False, "online", 10),
                ("连接异常的微信账号", True, "degraded", 10),
            )
        ])
        db.commit()
    _login(client, "readonly")
    response = client.get("/channels")
    assert response.status_code == 200
    page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    page.route("http://fulcrum.test/**", lambda route: _serve_page(route, response.text))
    try:
        page.goto("http://fulcrum.test/channels")
        for state in ("online", "offline", "disabled", "error", "unknown"):
            badge = page.locator(f'[data-presence="{state}"] .account-presence-badge')
            assert badge.is_visible()
            assert badge.locator(".account-presence-dot").evaluate("node => getComputedStyle(node).backgroundColor") != "rgba(0, 0, 0, 0)"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".account-presence").evaluate_all("nodes => nodes.every(node => {const box = node.getBoundingClientRect(), row = node.closest('.record-row').getBoundingClientRect(); return box.left >= row.left && box.right <= row.right && box.bottom <= row.bottom;})")
        if os.environ.get("QA_SCREENSHOT_DIR"):
            directory = Path(os.environ["QA_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f"account-presence-{width}.png"), full_page=True)
    finally:
        page.close()
