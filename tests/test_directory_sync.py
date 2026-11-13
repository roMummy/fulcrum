"""好友与群管理同步入口复用微桥队列，验证账号选择、权限和返回地址。"""

from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app import main
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob
from app.services.task_queue import parse_job_payload
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture()
def sync_context(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    notified = []
    monkeypatch.setattr(main.webridge_wakeup, "notify", notified.append)
    with ids["factory"]() as db:
        channels = [
            Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="同步甲号", channel_type="wechat_webridge", is_enabled=True),
            Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="同步乙号", channel_type="wechat_webridge", is_enabled=True),
            Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="停用微桥", channel_type="wechat_webridge", is_enabled=False),
            Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="已删除微桥", channel_type="wechat_webridge", is_enabled=True, deleted_at=now_shanghai()),
            Channel(tenant_id=ids["beta"], bot_id=ids["bot"], name="其他企业微桥", channel_type="wechat_webridge", is_enabled=True),
        ]
        db.add_all(channels)
        db.commit()
        ids = {**ids, "sync_channels": [channel.id for channel in channels], "notified": notified}
    return client, ids


def _jobs(ids):
    with ids["factory"]() as db:
        return [(job.tenant_id, parse_job_payload(job)) for job in db.scalars(select(TaskJob)).all()]


@pytest.mark.parametrize("path,label", [("/contacts", "同步好友"), ("/chatrooms", "同步群聊")])
def test_pages_show_sync_account_selector_and_keep_current_filter(sync_context, path, label):
    client, ids = sync_context
    _login(client, "operator")
    selected_id = ids["sync_channels"][1]
    response = client.get(f"{path}?channel_id={selected_id}")
    assert response.status_code == 200
    assert f'action="{path}/sync"' in response.text and label in response.text
    assert "全部微桥账号" in response.text
    assert f'<option value="{selected_id}" selected>同步乙号</option>' in response.text
    # 其他列表筛选器仍可查看停用账号；这里只检查同步表单的可操作范围。
    form = response.text.split("data-directory-sync>", 1)[1].split("</form>", 1)[0]
    assert "停用微桥" not in form and "已删除微桥" not in form and "其他企业微桥" not in form


@pytest.mark.parametrize("path,command", [("/contacts", "friends_all"), ("/chatrooms", "groups_all")])
def test_selected_account_uses_existing_command_queue_and_returns_to_directory(sync_context, path, command):
    client, ids = sync_context
    csrf = _login(client, "operator")
    selected_id = ids["sync_channels"][1]
    response = client.post(path + "/sync", data={"channel_id": selected_id, "csrf_token": csrf, "next": f"{path}?q=demo&channel_id={selected_id}&page=2&notice=old"})
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == path and not location.netloc
    assert parse_qs(location.query)["channel_id"] == [str(selected_id)]
    assert parse_qs(location.query)["q"] == ["demo"]
    assert "正在同步 1 个账号" in parse_qs(location.query)["notice"][0]
    assert parse_qs(location.query)["sync_jobs"][0].isdigit()
    assert len(parse_qs(location.query)["notice"]) == 1
    jobs = _jobs(ids)
    assert len(jobs) == 1
    tenant_id, payload = jobs[0]
    assert tenant_id == ids["alpha"] and payload["channel_id"] == selected_id
    assert payload["command"]["type"] == command and payload["command"]["params"] == {}
    assert ids["notified"] == [selected_id]


@pytest.mark.parametrize("path,command", [("/contacts", "friends_all"), ("/chatrooms", "groups_all")])
def test_all_accounts_only_enqueues_enabled_webridge_accounts_in_current_tenant(sync_context, path, command):
    client, ids = sync_context
    csrf = _login(client, "admin")
    response = client.post(path + "/sync", data={"channel_id": 0, "csrf_token": csrf})
    assert response.status_code == 303
    jobs = _jobs(ids)
    assert len(jobs) == 2
    assert {payload["channel_id"] for _, payload in jobs} == set(ids["sync_channels"][:2])
    assert all(tenant == ids["alpha"] and payload["command"]["type"] == command for tenant, payload in jobs)
    assert ids["notified"] == ids["sync_channels"][:2]


@pytest.mark.parametrize("invalid", ["disabled", "deleted", "other_tenant", "other_protocol", "negative"])
def test_invalid_account_does_not_enqueue_any_command(sync_context, invalid):
    client, ids = sync_context
    csrf = _login(client, "operator")
    account_id = {"disabled": ids["sync_channels"][2], "deleted": ids["sync_channels"][3], "other_tenant": ids["sync_channels"][4], "other_protocol": ids["channel_id"], "negative": -1}[invalid]
    response = client.post("/contacts/sync", data={"channel_id": account_id, "csrf_token": csrf})
    assert response.status_code == 404 and not _jobs(ids) and not ids["notified"]


def test_readonly_and_csrf_protection_apply_to_both_pages(sync_context):
    client, ids = sync_context
    csrf = _login(client, "operator")
    for path in ("/contacts", "/chatrooms"):
        assert client.post(path + "/sync", data={"csrf_token": "forged"}).status_code == 403
    csrf = _login(client, "readonly")
    for path in ("/contacts", "/chatrooms"):
        assert "data-directory-sync" not in client.get(path).text
        assert client.post(path + "/sync", data={"csrf_token": csrf}).status_code == 403
    assert not _jobs(ids)


@pytest.mark.parametrize("next_url", ["https://example.test/contacts", "//example.test/contacts", "/chatrooms", "/channels/1", "https://["])
def test_sync_return_url_cannot_leave_current_directory(sync_context, next_url):
    client, ids = sync_context
    csrf = _login(client, "operator")
    response = client.post("/contacts/sync", data={"channel_id": ids["sync_channels"][0], "csrf_token": csrf, "next": next_url})
    target = urlsplit(response.headers["location"])
    assert target.path == "/contacts" and not target.netloc


def test_single_account_uses_direct_button_and_no_available_account_has_no_control(sync_context):
    client, ids = sync_context
    csrf = _login(client, "operator")
    with ids["factory"]() as db:
        db.get(Channel, ids["sync_channels"][1]).is_enabled = False
        db.commit()
    page = client.get("/contacts")
    form = page.text.split("data-directory-sync>", 1)[1].split("</form>", 1)[0]
    assert '<select name="channel_id"' not in form
    assert f'name="channel_id" value="{ids["sync_channels"][0]}"' in form
    with ids["factory"]() as db:
        db.get(Channel, ids["sync_channels"][0]).is_enabled = False
        db.commit()
    assert "data-directory-sync" not in client.get("/contacts").text
    assert client.post("/contacts/sync", data={"csrf_token": csrf}).status_code == 400
    assert not _jobs(ids)
