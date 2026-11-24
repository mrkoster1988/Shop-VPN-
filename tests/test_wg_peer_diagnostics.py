"""Developer diagnostics for WireGuard peers: read only, pinned and secret free."""
import json
import time

import httpx
import pytest

from app import config as c, servers, wgdashboard, wg_peer_diagnostics as peer
from app.db import one, execute
from test_shop import client, register


SID = '2fe9c845-f87f-4704-8e04-f352f004cdaa'


def owner():
    return {'Authorization': 'Bearer ' + c.ADMIN_TOKEN}


def profile(*, schedule_expiry=False):
    return servers.validated('wg', {
        'api': 'wgdashboard', 'url': 'https://wg.example',
        'token': 'do-not-leak-' + 't' * 40, 'configuration': 'wg0',
        'schedule_expiry': schedule_expiry,
        **({'panel_timezone': 'UTC'} if schedule_expiry else {}),
    })


def setup_user(client, *, state='active', use_wgdashboard=True, secret=True):
    register(client)
    if use_wgdashboard:
        execute('UPDATE servers SET settings=?,state=?,health=? WHERE id=?',
                (c.encrypt(json.dumps(profile())), 'active', 'ok', 'test-wg'))
    execute("""INSERT INTO subscriptions(
        id,user_id,protocol,expires,state,server_id,secret,credential_id)
        VALUES (?,1,'wg',?,?, 'test-wg',?,?)""",
        (SID, int(time.time()) + 3600, state,
         c.encrypt('test-connection') if secret else None, c.encrypt(SID)))
    return SID


def test_no_wireguard_is_read_only_and_nothing_is_allocated(client):
    register(client)
    result = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'no_subscription'
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n'] == 0
    assert client.get('/api/admin/users/999/wireguard/peer', headers=owner()).status_code == 404


def test_existing_wg_agent_subscription_stays_pinned(client):
    setup_user(client, use_wgdashboard=False)
    result = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'other_backend'
    assert result.json()['server_name'] == 'Test wg'
    assert one('SELECT server_id FROM subscriptions WHERE id=?', (SID,))['server_id'] == 'test-wg'
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n'] == 0
    assert client.post('/api/admin/users/1/wireguard/retry', headers=owner()).status_code == 409


def test_missing_key_is_diagnosed_without_creation(client, monkeypatch):
    setup_user(client)
    async def forbid(*args):
        raise AssertionError('Read-only diagnostics cannot touch a panel without stored key')
    monkeypatch.setattr(peer, '_remote_peer', forbid)
    result = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'key_not_created'
    assert result.json()['local_ready'] is True
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n'] == 0
    assert 'do-not-leak' not in result.text
    assert 'test-connection' not in result.text


@pytest.mark.parametrize('remote,expected', [
    ('present', 'peer_found'),
    ('absent', 'peer_missing'),
    ('restricted', 'peer_restricted'),
    ('unavailable', 'panel_unavailable'),
])
def test_real_wgdashboard_read_contract_is_redacted(client, monkeypatch, remote, expected):
    setup_user(client)
    secret = wgdashboard.stored_private_key('test-wg', SID)
    public_key = wgdashboard.WGDashboard.identity(secret)
    seen = []

    def handler(request):
        assert request.headers['wg-dashboard-apikey'].startswith('do-not-leak-')
        assert request.method == 'GET'
        seen.append(request.url.path)
        if remote == 'unavailable':
            raise httpx.ConnectError('private server address must not leak', request=request)
        url = request.url.path.split('/api/')[1]
        if url == 'getWireguardConfigurations':
            data = [{'Name': 'wg0', 'Protocol': 'wg', 'Status': True}]
        elif url == 'getWireguardConfigurationInfo':
            data = {'configurationInfo': {'Name': 'wg0', 'Protocol': 'wg'},
                    'configurationPeers': [{'id': public_key, 'private_key': 'do-not-leak'}]
                        if remote == 'present' else [],
                    'configurationRestrictedPeers': [{'id': public_key}]
                        if remote == 'restricted' else []}
        else:
            raise AssertionError(url)
        return httpx.Response(200, json={'status': True, 'data': data})

    monkeypatch.setattr(peer, '_new_client',
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=12,
                                  follow_redirects=False, trust_env=False))
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200, response.text
    assert response.json()['status'] == expected
    assert public_key not in response.text and secret not in response.text
    assert 'do-not-leak' not in response.text and 'wg.example' not in response.text
    assert seen and all('/api/' in path for path in seen)
    assert one('SELECT state FROM subscriptions WHERE id=?',(SID,))['state'] == 'active'


def test_retry_only_wakes_eligible_pending_queue_with_original_identity(client, monkeypatch):
    register(client)
    execute('UPDATE servers SET settings=?,state=?,health=? WHERE id=?',
            (c.encrypt(json.dumps(profile(schedule_expiry=True))),
             'active', 'ok', 'test-wg'))
    execute('UPDATE flow_auto_policy SET enabled=1')
    now = int(time.time())
    execute("""INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id)
               VALUES (?,1,'wg',0,'pending','test-wg')""", (SID,))
    execute("""INSERT INTO flow_auto_grants(
       user_id,subscription_id,target_expires,policy_version,state,
       created,next_attempt,attempts)
       VALUES (1,?,?,1,'pending',?,?,3)""",
       (SID, now+30*86400, now, now+3600))

    report = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert report.status_code == 200, report.text
    assert report.json()['status'] == 'key_not_created'
    assert report.json()['grant']['attempts'] == 3
    assert report.json()['can_retry'] is True
    retry = client.post('/api/admin/users/1/wireguard/retry', headers=owner())
    assert retry.status_code == 200, retry.text
    assert retry.json() == {'status': 'queued', 'ok': True}
    assert one('SELECT next_attempt,attempts FROM flow_auto_grants')['next_attempt'] == 0
    assert one('SELECT attempts FROM flow_auto_grants')['attempts'] == 3
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n'] == 0
    assert one('SELECT server_id FROM subscriptions WHERE id=?',(SID,))['server_id'] == 'test-wg'
    execute('UPDATE flow_auto_grants SET target_expires=1,next_attempt=3000000000')
    assert client.post('/api/admin/users/1/wireguard/retry', headers=owner()).status_code == 409
    assert one('SELECT next_attempt FROM flow_auto_grants')['next_attempt'] == 3000000000


def test_panel_error_never_reassigns_old_subscription(client):
    setup_user(client)
    execute("UPDATE servers SET state='disabled' WHERE id='test-wg'")
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200
    assert response.json()['server_state'] == 'disabled'
    assert response.json()['status'] == 'key_not_created'
    assert client.post('/api/admin/users/1/wireguard/retry', headers=owner()).status_code == 409


def test_missing_login_and_wrong_role_cannot_probe_or_retry(client):
    assert client.get('/api/admin/users/1/wireguard/peer').status_code == 401
    assert client.post('/api/admin/users/1/wireguard/retry').status_code == 401
    response=client.post('/api/admin/auth/bootstrap', headers=owner(),json={
        'email':'developer@example.org','password':'long-developer-password'})
    assert response.status_code == 200
    made=client.post('/api/admin/staff',json={'email':'admin@example.org',
        'password':'long-admin-password','role':'administrator'})
    assert made.status_code == 201
    client.post('/api/admin/auth/logout')
    logged=client.post('/api/admin/auth/login',json={
        'email':'admin@example.org','password':'long-admin-password'})
    assert logged.status_code == 200
    assert client.get('/api/admin/users/1/wireguard/peer').status_code == 403
    assert client.post('/api/admin/users/1/wireguard/retry').status_code == 403
