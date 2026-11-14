"""好友检测持久化接口的权限、CSRF、账号隔离和确认边界。"""

from __future__ import annotations

import asyncio

import pytest

import app.web.contact_check as check_web
from app.core.tenant_context import require_tenant_id
from app.models.entities import Channel, WorkWechatContact
from app.services.wechat_webridge import friend_check
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


GENERATION = "a" * 32


@pytest.fixture()
def check_context(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    monkeypatch.setattr(check_web, "TenantSessionLocal", ids["factory"])
    with ids["factory"]() as db:
        current = Channel(
            tenant_id=ids["alpha"], bot_id=ids["bot"], name="当前微桥", channel_type="wechat_webridge",
        )
        foreign = Channel(
            tenant_id=ids["beta"], bot_id=ids["bot"], name="其他租户微桥", channel_type="wechat_webridge",
        )
        db.add_all([current, foreign])
        db.flush()
        contact = WorkWechatContact(
            tenant_id=ids["alpha"], channel_id=current.id, contact_kind="friend", contact_external_id="test-friend",
            name="测试好友",
        )
        db.add(contact)
        db.commit()
        channel_id, foreign_id, contact_id = int(current.id), int(foreign.id), int(contact.id)

    calls = []
    snapshot = {"generation": GENERATION, "status": "paused", "contacts": [], "results": [], "pending": None}

    def operation(name):
        def run(db, channel, *args, **kwargs):
            assert require_tenant_id() == ids["alpha"]
            assert channel.id == channel_id
            # 数据库调用必须在同步路由的工作线程中执行，不能占用 ASGI 事件循环。
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            calls.append((name, args, kwargs))
            if name.startswith("start_") and name != "start_run":
                return {"job_id": 42, "progress": snapshot}
            if name.startswith("poll_"):
                return {"state": "running", "result": None, "progress": snapshot}
            return snapshot
        return run

    for name in ("get_state", "start_run", "pause_run", "clear_run", "start_item", "poll_item", "start_action", "poll_action"):
        monkeypatch.setattr(check_web.friend_check_runs, name, operation(name))
    csrf = _login(client, "admin")
    return client, ids, csrf, channel_id, foreign_id, contact_id, calls


def _headers(csrf):
    return {"X-CSRF-Token": csrf}


def test_page_and_saved_state_allow_readonly_without_dispatching_checks(check_context):
    client, _ids, _csrf, channel_id, _foreign, _contact, calls = check_context
    _login(client, "readonly")
    page = client.get(f"/contacts/check?channel_id={channel_id}")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert 'id="contact-check"' in page.text
    state = client.get(f"/contacts/check/{channel_id}/state")
    assert state.status_code == 200 and state.json()["generation"] == GENERATION
    assert state.headers["cache-control"] == "no-store"
    assert [name for name, _args, _kwargs in calls] == ["get_state"]
    friends = client.get(f"/contacts/check/{channel_id}/friends")
    assert friends.status_code == 200 and friends.json()["total"] == 1


@pytest.mark.parametrize("endpoint", ["run", "pause", "clear"])
def test_progress_changes_require_edit_permission_and_csrf(check_context, endpoint):
    client, _ids, csrf, channel_id, _foreign, _contact, calls = check_context
    data = {"generation": GENERATION, "confirmed": True}
    url = f"/contacts/check/{channel_id}/{endpoint}"
    assert client.post(url, json=data).status_code == 403
    readonly_csrf = _login(client, "readonly")
    assert client.post(url, json=data, headers=_headers(readonly_csrf)).status_code == 403
    assert not calls


def test_progress_changes_preserve_generation_interval_and_confirmation(check_context):
    client, _ids, csrf, channel_id, _foreign, _contact, calls = check_context
    for endpoint, data in (
        ("run", {"generation": GENERATION, "interval_seconds": 0.5}),
        ("pause", {"generation": GENERATION}),
        ("clear", {"generation": GENERATION, "confirmed": True}),
    ):
        response = client.post(f"/contacts/check/{channel_id}/{endpoint}", json=data, headers=_headers(csrf))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert calls == [
        ("start_run", (), {"generation": GENERATION, "interval_seconds": 0.5}),
        ("pause_run", (), {"generation": GENERATION}),
        ("clear_run", (), {"generation": GENERATION, "confirmed": True}),
    ]


def test_old_page_cannot_create_checks_without_saved_generation(check_context):
    client, _ids, csrf, channel_id, _foreign, contact_id, calls = check_context
    response = client.post(f"/contacts/check/{channel_id}/friends/{contact_id}", headers=_headers(csrf))
    assert response.status_code == 409
    assert "重新打开页面" in response.json()["detail"]
    assert not calls


def test_check_and_action_routes_pass_only_validated_parameters(check_context):
    client, _ids, csrf, channel_id, _foreign, contact_id, calls = check_context
    base = f"/contacts/check/{channel_id}"
    for endpoint, payload in (
        (f"friends/{contact_id}", {"generation": GENERATION, "scan": True}),
        (f"jobs/42/poll", {"generation": GENERATION}),
        (f"friends/{contact_id}/remark", {"generation": GENERATION, "suffix": "-复查"}),
        (f"friends/{contact_id}/delete", {"generation": GENERATION, "confirmed": True}),
        ("actions/42/poll", {"generation": GENERATION}),
    ):
        assert client.post(f"{base}/{endpoint}", json=payload, headers=_headers(csrf)).status_code == 200
    assert calls == [
        ("start_item", (contact_id,), {"generation": GENERATION, "scan": True}),
        ("poll_item", (42,), {"generation": GENERATION}),
        ("start_action", (contact_id,), {"generation": GENERATION, "action": "remark", "suffix": "-复查"}),
        ("start_action", (contact_id,), {"generation": GENERATION, "action": "delete", "suffix": "- 单删"}),
        ("poll_action", (42,), {"generation": GENERATION}),
    ]


def test_clear_delete_and_remark_reject_implicit_or_invalid_confirmation(check_context):
    client, _ids, csrf, channel_id, _foreign, contact_id, calls = check_context
    base = f"/contacts/check/{channel_id}"
    for endpoint, payload in (
        ("clear", {"generation": GENERATION}),
        ("clear", {"generation": GENERATION, "confirmed": "true"}),
        (f"friends/{contact_id}/delete", {"generation": GENERATION}),
        (f"friends/{contact_id}/remark", {"generation": GENERATION, "suffix": 123}),
        ("run", {"generation": GENERATION, "interval_seconds": True}),
        ("run", {"generation": GENERATION, "interval_seconds": 10 ** 100}),
    ):
        assert client.post(f"{base}/{endpoint}", json=payload, headers=_headers(csrf)).status_code == 400
    assert not calls


def test_saved_state_and_mutations_reject_other_tenant_channels(check_context):
    client, _ids, csrf, _channel_id, foreign_id, _contact, calls = check_context
    assert client.get(f"/contacts/check/{foreign_id}/state").status_code == 404
    assert client.post(
        f"/contacts/check/{foreign_id}/run", json={"generation": GENERATION}, headers=_headers(csrf),
    ).status_code == 404
    assert not calls


def test_service_conflicts_preserve_http_status_and_message(check_context, monkeypatch):
    client, _ids, csrf, channel_id, _foreign, _contact, _calls = check_context

    def stale_generation(*args, **kwargs):
        raise friend_check.FriendCheckError(409, "检测结果已被清空，请重新读取")

    monkeypatch.setattr(check_web.friend_check_runs, "pause_run", stale_generation)
    response = client.post(
        f"/contacts/check/{channel_id}/pause", json={"generation": GENERATION}, headers=_headers(csrf),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "检测结果已被清空，请重新读取"


@pytest.mark.parametrize("endpoint", ["jobs/42/poll", "actions/42/poll"])
def test_polling_requires_current_generation(check_context, endpoint):
    """旧页面缺少代次时直接拒绝，不能轮询并更新持久化结果。"""
    client, _ids, csrf, channel_id, _foreign, _contact, calls = check_context
    response = client.post(f"/contacts/check/{channel_id}/{endpoint}", headers=_headers(csrf))
    assert response.status_code == 409
    assert not calls
