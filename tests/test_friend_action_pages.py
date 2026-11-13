"""好友备注和删除入口的权限、确认及真实任务回报。"""

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, TaskJob, WorkWechatContact
from app.services.wechat_webridge import service
from test_friend_check_pages import IDENTITY, friend_check_context  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def _url(ids, action, *, contact=None, channel=None):
    return f'/contacts/check/{channel or ids["bridge_channel"]}/friends/{contact or ids["bridge_contacts"][0]}/{action}'


def _poll(client, ids, csrf, job_id, *, channel=None):
    return client.post(f'/contacts/check/{channel or ids["bridge_channel"]}/actions/{job_id}/poll', headers={"X-CSRF-Token": csrf})


@pytest.mark.parametrize('action,body', [('remark', {'suffix': '- 单删'}), ('delete', {'confirmed': True})])
def test_friend_operation_routes_obey_tenant_role_and_csrf(friend_check_context, action, body):
    client, ids = friend_check_context
    csrf = _login(client, 'operator')
    assert client.post(_url(ids, action), json=body, headers={'X-CSRF-Token': 'invalid'}).status_code == 403
    assert client.post(_url(ids, action, channel=ids['beta_bridge_channel']), json=body, headers={'X-CSRF-Token': csrf}).status_code == 404
    assert client.post(_url(ids, action, channel=ids['other_bridge_channel']), json=body, headers={'X-CSRF-Token': csrf}).status_code == 404
    csrf = _login(client, 'readonly')
    assert client.post(_url(ids, action), json=body, headers={'X-CSRF-Token': csrf}).status_code == 403
    assert _poll(client, ids, csrf, 999).status_code == 403
    with ids['factory']() as db:
        assert not db.scalars(select(TaskJob)).all()


@pytest.mark.parametrize('body', [{}, {'confirmed': False}, {'confirmed': 1}, {'confirmed': 'true'}, []])
def test_delete_requires_explicit_confirmation(friend_check_context, body):
    client, ids = friend_check_context
    csrf = _login(client, 'operator')
    response = client.post(_url(ids, 'delete'), json=body, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 400
    with ids['factory']() as db:
        assert not db.scalars(select(TaskJob)).all()


@pytest.mark.parametrize('body', [None, [], {'suffix': 1}, {'suffix': None}, {'suffix': ''}])
def test_remark_rejects_invalid_json_and_suffix(friend_check_context, body):
    client, ids = friend_check_context
    csrf = _login(client, 'operator')
    response = client.post(_url(ids, 'remark'), content='{' if body is None else None, json=body, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 400


@pytest.mark.parametrize('action,command,result', [
    ('remark', 'set_friend_remark', {'ok': True, 'id': 'wxid_check_0', 'status': 'applied_locally', 'remark': '备注0- 单删'}),
    ('delete', 'delete_friend', {'id': 'wxid_check_0'}),
])
def test_friend_operation_is_applied_after_confirmed_phone_result(friend_check_context, action, command, result):
    client, ids = friend_check_context
    csrf = _login(client, 'operator')
    response = client.post(_url(ids, action), json={'suffix': '- 单删', 'confirmed': True}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    job_id = response.json()['job_id']
    assert _poll(client, ids, csrf, job_id).json() == {'state': 'pending', 'result': None}
    assert _poll(client, ids, 'invalid', job_id).status_code == 403
    assert _poll(client, ids, csrf, job_id, channel=ids['other_bridge_channel']).status_code == 404
    with tenant_scope(ids['alpha']), tenant_session(ids['factory']) as db:
        channel = db.get(Channel, ids['bridge_channel'])
        commands = service.claim_commands(db, channel, IDENTITY)
        assert len(commands) == 1 and commands[0]['command_name'] == command
        assert commands[0]['args']['id'] == 'wxid_check_0'
        if action == 'remark':
            assert commands[0]['args']['remark'] == '备注0- 单删'
        service.accept_command_result(db, channel, {**IDENTITY, 'request_id': commands[0]['request_id'], 'command_name': command, 'ok': True, 'result': result})
    for _ in range(2):
        response = _poll(client, ids, csrf, job_id)
        assert response.status_code == 200
        outcome = response.json()
        assert outcome['state'] == 'completed' and outcome['result']['ok'] is True
        assert outcome['result']['action'] == action
    with ids['factory']() as db:
        contact = db.get(WorkWechatContact, ids['bridge_contacts'][0])
        if action == 'delete':
            assert contact.deleted_at is not None
            assert client.post(_url(ids, action), json={'confirmed': True}, headers={'X-CSRF-Token': csrf}).status_code == 404
        else:
            assert contact.display_name == '备注0- 单删'
    if action == 'delete':
        friends = client.get(f'/contacts/check/{ids["bridge_channel"]}/friends').json()['contacts']
        assert ids['bridge_contacts'][0] not in {contact['id'] for contact in friends}
