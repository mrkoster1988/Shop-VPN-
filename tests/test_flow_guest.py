import asyncio
from concurrent.futures import ThreadPoolExecutor

from app import config, flow_auto_provision, flow_guests, providers
from app.db import one
from test_shop import client
from test_flow_auto_provision import _node, _policy, CONFIG


def test_guest_one_seven_day_grant_without_account_login(client, monkeypatch):
    device, key = 'a' * 64, 'b' * 64
    _node()
    assert _policy(client, monkeypatch, days=30).status_code == 200
    monkeypatch.setenv('FLOW_TRIAL_MTPROXY_ONLY', 'true')
    monkeypatch.setattr(config, 'SECURE_COOKIE', True)
    monkeypatch.setattr(config, 'PUBLIC_URL', 'https://pushok.shop/shop')
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: flow_guests.claim(device, 'Bearer '+key, 'fixture'), range(4)))
    assert all(item['status']=='pending' for item in replies)
    row = one('SELECT created, target_expires FROM flow_auto_grants')
    assert row['target_expires'] - row['created'] == 7 * 86400
    assert one('SELECT count(*) AS n FROM users')['n'] == 1
    assert client.get('/api/me').status_code == 401
    headers = {'Authorization': 'Bearer ' + key}
    assert client.post('/api/flow/guest', json={'device': device}).status_code == 401
    async def issue(sub):
        assert sub['expires'] == row['target_expires']
        return CONFIG
    monkeypatch.setattr(providers, 'provision', issue)
    assert asyncio.run(flow_auto_provision.process_pending()) == 1
    result = client.post('/api/flow/guest', json={'device': device}, headers=headers)
    assert result.json()['status'] == 'active'
    assert 'no-store' in result.headers['cache-control']
    link = result.json()['subscription_url']
    assert client.get('/' + link.split('/shop/', 1)[1]).json()['expires_at'] == row['target_expires']
    assert flow_guests.claim(device, 'Bearer '+'c'*64, 'fixture')['subscription_url'] is None
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 1


def test_browser_guest_requires_click_and_resumes_with_same_cookie(client, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _node()
    assert _policy(client, monkeypatch, days=30).status_code == 200
    monkeypatch.setattr(config, 'SECURE_COOKIE', True)
    monkeypatch.setattr(config, 'PUBLIC_URL', 'https://testserver')
    monkeypatch.setattr(config, 'COOKIE_PATH', '/')
    browser = TestClient(app, base_url='https://testserver')
    def request(activate=False, origin='https://testserver'):
        return browser.post('/api/guest/browser', json={'activate': activate}, headers={'Origin': origin})
    assert request(True, 'https://other.example').status_code == 403
    initial = request()
    assert initial.json()['status'] == 'available'
    assert 'HttpOnly' in initial.headers['set-cookie'] and 'Secure' in initial.headers['set-cookie']
    assert one('SELECT COUNT(*) AS n FROM flow_guest_trials')['n'] == 0
    pending = request(True).json()
    assert pending['status'] == 'pending' and pending['expires_at'] > 0
    assert request().json() == pending
    assert request(True).json() == pending
    assert one('SELECT COUNT(*) AS n FROM flow_guest_trials')['n'] == 1
    assert browser.get('/api/me').status_code == 401


def test_guest_uses_guest_interface_for_issuance_expiration_export_and_revoke(client, monkeypatch):
    from app import servers, wgdashboard
    from app.db import db
    _node()
    assert _policy(client, monkeypatch, days=30).status_code == 200
    monkeypatch.setattr(config, 'SECURE_COOKIE', True)
    monkeypatch.setattr(config, 'PUBLIC_URL', 'https://pushok.shop/shop')
    calls = []
    original_identity = staticmethod(wgdashboard.WGDashboard.identity)

    class Panel:
        identity = original_identity
        def __init__(self, profile):
            self.interface = profile['configuration']
        async def ensure_peer(self, client, private_key, name):
            calls.append((self.interface, 'peer'))
        async def ensure_expiration(self, client, private_key, expires, server_id, credential):
            calls.append((self.interface, 'expiry'))
        async def download_config(self, client, private_key):
            calls.append((self.interface, 'export'))
            return '[Interface]\nPrivateKey = '+private_key+'\n[Peer]\nEndpoint = wg.pushok.shop:65353\n'
        async def revoke_peer(self, client, public_key):
            calls.append((self.interface, 'revoke'))

    monkeypatch.setattr(wgdashboard, 'WGDashboard', Panel)
    assert flow_guests.claim('a'*64,'Bearer '+'b'*64,'fixture')['status'] == 'pending'
    assert asyncio.run(flow_auto_provision.process_pending()) == 1
    sub = one('SELECT * FROM subscriptions')
    assert sub['wg_configuration'] == 'wg-guest'
    assert 'wg.pushok.shop:65353' in config.decrypt(sub['secret'])
    with db() as conn:
        assert servers.decode(servers.select(conn, 'wg'))['configuration'] == 'wg0'
    asyncio.run(providers.disable(sub))
    assert calls == [('wg-guest', step) for step in ('peer','expiry','export','revoke')]


def test_guest_panel_failure_keeps_grant_pending_without_using_wg0(client, monkeypatch):
    from app import wgdashboard
    _node()
    assert _policy(client, monkeypatch).status_code == 200
    monkeypatch.setattr(config, 'SECURE_COOKIE', True)
    seen = []
    async def offline(self, *args):
        seen.append(self.profile['configuration'])
        raise wgdashboard.PanelError('WGDashboardInterfaceUnavailable')
    monkeypatch.setattr(wgdashboard.WGDashboard, 'ensure_peer', offline)
    assert flow_guests.claim('c'*64,'Bearer '+'d'*64,'fixture')['status'] == 'pending'
    assert asyncio.run(flow_auto_provision.process_pending()) == 0
    assert seen == ['wg-guest']
    assert one('SELECT state,error FROM flow_auto_grants') == {
        'state':'pending','error':'WGDashboardInterfaceUnavailable'}


def test_dedicated_panel_is_reserved_for_guests(client):
    import json
    from app import servers
    from app.db import db
    _node()
    with db() as conn:
        regular = servers.select(conn, 'wg')
        profile = {**servers.decode(regular), 'configuration': 'wg-guest'}
        conn.execute("""INSERT INTO servers(id,name,protocol,settings,state,health,created)
                        VALUES ('guest','Guests','wg',?,'active','ok',0)""",
                     (config.encrypt(json.dumps(profile)),))
        assert flow_auto_provision._node(conn, guest=True)['id'] == 'guest'
        assert flow_auto_provision._node(conn)['id'] == regular['id']
        assert servers.select(conn, 'wg')['id'] == regular['id']
