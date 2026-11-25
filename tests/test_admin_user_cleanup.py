"""Deleting unused Shop customers must not erase business or identity history."""
from app import config as c, service
from app.db import execute, one
from app.security import digest
from test_shop import client, register


def owner():
    return {'Authorization': 'Bearer ' + c.ADMIN_TOKEN}


def preview(client, uid=1):
    response = client.get(f'/api/admin/users/{uid}/delete/preview', headers=owner())
    assert response.status_code == 200, response.text
    return response.json()


def delete(client, plan, uid=1):
    return client.post(f'/api/admin/users/{uid}/delete', headers=owner(), json={
        'revision': plan['revision'], 'confirm': 'DELETE_UNUSED_CUSTOMER'})


def test_unused_account_removed_with_credentials_and_id_never_reused(client):
    register(client)
    old = client.cookies.get('session')
    execute('INSERT INTO tickets VALUES (?,?,?)', (digest('private-ticket'), 1, 9999999999))
    plan = preview(client)
    assert plan['can_delete'] and plan['auth_records']['sessions'] == 1
    assert plan['auth_records']['tickets'] == 1
    assert old not in str(plan) and 'private-ticket' not in str(plan)
    result = delete(client, plan)
    assert result.status_code == 200, result.text
    assert result.json() == {'ok': True, 'status': 'deleted', 'user_id': 1}
    assert one('SELECT id FROM users WHERE id=1') is None
    assert one('SELECT token FROM sessions WHERE user_id=1') is None
    assert one('SELECT token FROM tickets WHERE user_id=1') is None
    assert client.get('/api/me').status_code == 401
    assert client.get('/api/admin/users', headers=owner()).json()['total'] == 0
    register(client)
    assert client.get('/api/me').json()['id'] == 2
    assert client.get('/api/admin/users/1', headers=owner()).status_code == 404
    assert service.telegram_account(123456789)['id'] == 3


def test_stale_plan_and_financial_history_prevent_deletion(client):
    register(client)
    plan = preview(client)
    execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status) "
            "VALUES ('preserve-order',1,'wg','month',30,199,1,'active')")
    assert delete(client, plan).status_code == 409
    blocked = preview(client)
    assert not blocked['can_delete']
    assert {'label': 'Заказы', 'count': 1} in blocked['blockers']
    assert delete(client, blocked).status_code == 409
    assert one("SELECT amount FROM orders WHERE id='preserve-order'")['amount'] == 199
    assert one('SELECT id FROM users WHERE id=1') is not None


def test_partner_history_and_linked_identity_are_protected(client):
    uid = service.telegram_account(987654321)['id']
    execute("INSERT INTO flow_bridge_identities(mtproxy_uuid,telegram_id,shop_user_id,created) "
            "VALUES ('link-fixture',987654321,?,1)", (uid,))
    p = preview(client, uid)
    assert not p['can_delete']
    assert {'label':'Связь с аккаунтом Flow','count':1} in p['blockers']
    assert delete(client, p, uid).status_code == 409
    assert one('SELECT shop_user_id FROM flow_bridge_identities')['shop_user_id'] == uid


def test_delete_requires_developer_and_explicit_confirmation(client):
    register(client)
    assert client.get('/api/admin/users/1/delete/preview').status_code == 401
    plan = preview(client)
    assert client.post('/api/admin/users/1/delete', json={
        'revision': plan['revision'], 'confirm': 'DELETE_UNUSED_CUSTOMER'}).status_code == 401
    assert client.post('/api/admin/users/1/delete', headers=owner(), json={
        'revision': plan['revision'], 'confirm': 'WRONG'}).status_code == 422
    assert one('SELECT id FROM users WHERE id=1') is not None
    assert client.post('/api/admin/auth/bootstrap', headers=owner(), json={
        'email':'developer@example.org','password':'long-developer-password'}).status_code==200
    assert client.post('/api/admin/staff', json={
        'email':'admin@example.org','password':'long-admin-password','role':'administrator'}).status_code==201
    client.post('/api/admin/auth/logout')
    assert client.post('/api/admin/auth/login', json={
        'email':'admin@example.org','password':'long-admin-password'}).status_code==200
    assert client.get('/api/admin/users/1/delete/preview').status_code==403
    assert client.post('/api/admin/users/1/delete', json={
        'revision':plan['revision'],'confirm':'DELETE_UNUSED_CUSTOMER'}).status_code==403
