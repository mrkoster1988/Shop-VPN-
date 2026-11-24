"""Durable starter access: idempotency, quota, ownership and backend retries."""
import asyncio
import json
import time

from app import config as c, flow_auto_provision as auto, providers, servers, service
from app.db import db, one, execute
from test_shop import client, register


CONFIG = """[Interface]
PrivateKey = AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=
Address = 10.77.0.2/32
[Peer]
PublicKey = AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=
Endpoint = 203.0.113.10:51820
AllowedIPs = 0.0.0.0/0, ::/0
"""


def _node():
    profile = servers.validated('wg', {
        'api': 'wgdashboard', 'url': 'https://wg.example',
        'token': 'z' * 40, 'configuration': 'wg0',
        'panel_timezone': 'UTC', 'schedule_expiry': True,
    })
    execute('UPDATE servers SET settings=?,state=?,health=? WHERE id=?',
            (c.encrypt(json.dumps(profile)), 'active', 'ok', 'test-wg'))


def _policy(client, monkeypatch, *, enabled=True, days=1, max_users=3):
    # Only the owner can enable this. The test's HTTP client runs on testserver,
    # while production requires PUBLIC_URL=https.
    monkeypatch.setattr(c, 'SECURE_COOKIE', True)
    before = client.get('/api/admin/flow/wireguard/auto',
                        headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN})
    assert before.status_code == 200
    response = client.put('/api/admin/flow/wireguard/auto',
                          json={'enabled': enabled, 'days': days,
                                'max_users': max_users, 'version': before.json()['version']},
                          headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN})
    monkeypatch.setattr(c, 'SECURE_COOKIE', False)
    return response


def test_default_off_no_grant_after_registration_or_login(client):
    assert auto.policy()['enabled'] is False
    assert auto.policy()['days'] == 30
    assert client.get('/api/admin/flow/wireguard/auto').status_code == 401
    assert client.get('/api/admin/flow/wireguard/auto',
                      headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN}).json()['enabled'] is False
    register(client)
    assert client.get('/api/flow/wireguard/connections').json() == {'subscriptions': []}
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login', json={
        'email': 'user@example.com', 'password': 'correct-horse-123'}).status_code == 200
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 0


def test_policy_requires_https_and_expiring_wgdashboard(client, monkeypatch):
    assert _policy(client, monkeypatch).status_code == 409
    _node()
    # The authenticated caller must not be able to activate a grant on HTTP.
    before = auto.policy()
    assert before['enabled'] is False
    assert _policy(client, monkeypatch, days=0).status_code == 422
    assert _policy(client, monkeypatch).status_code == 200
    stale = client.put('/api/admin/flow/wireguard/auto', json={
        'enabled': False, 'days': 1, 'max_users': 3, 'version': 1},
        headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN})
    assert stale.status_code == 409


def test_existing_user_and_new_account_are_issued_only_once(client, monkeypatch):
    register(client)
    _node()
    assert _policy(client, monkeypatch, max_users=2).status_code == 200
    old = client.get('/api/flow/wireguard/connections').json()['subscriptions']
    assert len(old) == 1 and old[0]['state'] == 'pending'
    original_id = old[0]['subscription_id']
    assert auto.enqueue(1) == 'pending'
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login', json={
        'email': 'user@example.com', 'password': 'correct-horse-123'}).status_code == 200
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 1

    calls = []
    async def provision(sub):
        calls.append((sub['id'], sub['expires']))
        return CONFIG
    monkeypatch.setattr(providers, 'provision', provision)
    assert asyncio.run(auto.process_pending()) == 1
    assert asyncio.run(auto.process_pending()) == 0
    assert calls == [(original_id, calls[0][1])]
    assert calls[0][1] > time.time()
    assert one('SELECT state,access_source,device_limit FROM subscriptions')['state'] == 'active'
    assert one('SELECT access_source FROM subscriptions')['access_source'] == 'trial'
    active = client.get('/api/flow/wireguard/subscriptions/' + original_id)
    assert active.status_code == 200 and active.json()['schema'] == 'pushok-flow-subscription'
    assert active.json()['profiles'][0]['config'] == CONFIG
    assert client.get('/api/flow/wireguard/connections').json()['subscriptions'][0]['ready'] is True

    client.post('/api/auth/logout')
    register(client, 'new@example.com')
    second = one('SELECT subscription_id FROM flow_auto_grants WHERE user_id=2')
    assert second and second['subscription_id'] != original_id
    assert asyncio.run(auto.process_pending()) == 1
    assert len(calls) == 2 and calls[0][0] != calls[1][0]
    assert client.get('/api/flow/wireguard/subscriptions/' + original_id).status_code == 404
    client.post('/api/auth/logout')
    register(client, 'third@example.com')
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 2
    assert client.get('/api/flow/wireguard/connections').json() == {'subscriptions': []}


def test_retry_keeps_identity_and_does_not_claim_ready_before_peer(client, monkeypatch):
    _node()
    assert _policy(client, monkeypatch).status_code == 200
    register(client)
    sid = one('SELECT subscription_id FROM flow_auto_grants')['subscription_id']
    attempted = []
    async def flaky(sub):
        attempted.append(sub['id'])
        if len(attempted) == 1:
            raise RuntimeError('PRIVATE-KEY-AND-TOKEN-DO-NOT-LOG')
        return CONFIG
    monkeypatch.setattr(providers, 'provision', flaky)
    assert asyncio.run(auto.process_pending()) == 0
    assert one('SELECT state FROM subscriptions')['state'] == 'pending'
    assert client.get('/api/flow/wireguard/subscriptions/' + sid).status_code == 409
    assert one('SELECT attempts FROM flow_auto_grants')['attempts'] == 1
    execute('UPDATE flow_auto_grants SET next_attempt=0')
    assert asyncio.run(auto.process_pending()) == 1
    assert attempted == [sid, sid]
    assert one('SELECT state FROM flow_auto_grants')['state'] == 'active'


def test_existing_wireguard_is_not_overwritten_even_if_expired(client, monkeypatch):
    register(client)
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,access_source) "
            "VALUES ('old',1,'wg',1,'expired','purchase')")
    _node()
    assert _policy(client, monkeypatch).status_code == 200
    assert auto.enqueue(1) == 'existing'
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 0
    assert one('SELECT id,state FROM subscriptions WHERE protocol=?', ('wg',)) == {
        'id': 'old', 'state': 'expired'}


def test_policy_off_does_not_revoke_existing_grant(client, monkeypatch):
    _node()
    assert _policy(client, monkeypatch).status_code == 200
    register(client)
    sid = one('SELECT subscription_id FROM flow_auto_grants')['subscription_id']
    assert _policy(client, monkeypatch, enabled=False).status_code == 200
    async def forbidden(sub):
        raise AssertionError('No remote grant while the owner disabled the policy')
    monkeypatch.setattr(providers, 'provision', forbidden)
    assert asyncio.run(auto.process_pending()) == 0
    assert one('SELECT state FROM flow_auto_grants')['state'] == 'pending'
    assert one('SELECT state FROM subscriptions WHERE id=?', (sid,))['state'] == 'pending'


def test_mtproxy_only_pilot_excludes_public_shop_signups(client, monkeypatch):
    monkeypatch.setenv('FLOW_TRIAL_MTPROXY_ONLY', 'true')
    _node()
    assert _policy(client, monkeypatch, enabled=True, days=30, max_users=2).status_code == 200
    register(client)
    assert auto.enqueue(1) == 'not_mtproxy'
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 0

    # The bridge binds a verified MTProxy identity before requesting a grant.
    from test_flow_identity_bridge import _enabled, _call
    _enabled(monkeypatch)
    reply = _call(client)
    assert reply.status_code == 200, reply.text
    assert reply.json() == {'status': 'pending', 'subscription_url': None}
    grant = one('SELECT user_id,target_expires FROM flow_auto_grants')
    assert grant['user_id'] == 2
    assert grant['target_expires'] > int(time.time()) + 29 * 86400

    # Another login by the original Shop email account still cannot claim it.
    assert auto.enqueue(1) == 'not_mtproxy'
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 1


def test_owner_issuance_diagnostics_do_not_expose_credentials(client, monkeypatch):
    status=client.get('/api/admin/flow/wireguard/auto',
        headers={'Authorization':'Bearer '+c.ADMIN_TOKEN})
    assert status.status_code==200
    initial=status.json()['diagnostics']
    assert initial['state']=='policy_disabled'
    assert initial['pending']==0 and initial['active']==0
    assert 'token' not in json.dumps(initial).lower()
    _node()
    assert _policy(client, monkeypatch).status_code==200
    register(client)
    status=client.get('/api/admin/flow/wireguard/auto',
        headers={'Authorization':'Bearer '+c.ADMIN_TOKEN})
    data=status.json()['diagnostics']
    assert data['state']=='issuance_pending'
    assert data['ready_node'] is True
    assert data['pending']==1 and data['retrying']==0
    assert data['existing_wg_subscriptions']==1
    assert 'z'*40 not in json.dumps(data)
