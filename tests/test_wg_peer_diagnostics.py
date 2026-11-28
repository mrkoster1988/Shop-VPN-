"""Developer diagnostics for WireGuard peers: read only, pinned and secret free."""
import base64
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


def config_text(private_key, **changes):
    values = {
        'Address': '10.0.0.2/32, 2001:db8::2/128',
        'DNS': '1.1.1.1', 'MTU': '1420', 'ListenPort': '0',
        'PublicKey': base64.b64encode(bytes([2]) * 32).decode(),
        'PresharedKey': base64.b64encode(bytes([3]) * 32).decode(),
        'AllowedIPs': '0.0.0.0/0, ::/0',
        'Endpoint': 'vpn.example:51820', 'PersistentKeepalive': '25',
        **changes,
    }
    interface = '\n'.join(f'{name} = {values.pop(name)}'
                          for name in ('Address', 'DNS', 'MTU', 'ListenPort'))
    remote = '\n'.join(f'{name} = {value}' for name, value in values.items()
                       if value is not None)
    return f'[Interface]\nPrivateKey = {private_key}\n{interface}\n[Peer]\n{remote}\n'


def panel_reader(monkeypatch, private_key, exported, *, configuration='wg0', error=None):
    public_key = wgdashboard.WGDashboard.identity(private_key)
    calls = []

    def handler(request):
        assert request.method == 'GET', 'Diagnostics/refresh must not rewrite a panel peer'
        calls.append(request.url.path)
        route = request.url.path.split('/api/')[1]
        if route == 'getWireguardConfigurations':
            data = [{'Name': configuration, 'Protocol': 'wg', 'Status': True}]
        elif route == 'getWireguardConfigurationInfo':
            assert request.url.params['configurationName'] == configuration
            data = {'configurationInfo': {'Name': configuration, 'Protocol': 'wg'},
                    'configurationPeers': [{'id': public_key}],
                    'configurationRestrictedPeers': []}
        elif route == f'downloadPeer/{configuration}':
            assert request.url.params['id'] == public_key
            if error:
                raise httpx.ReadTimeout('PRIVATE-ENDPOINT-KEY-TOKEN', request=request)
            data = {'file': exported}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={'status': True, 'data': data})

    monkeypatch.setattr(peer, '_new_client', lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=12,
        follow_redirects=False, trust_env=False))
    return calls


@pytest.mark.parametrize('field,value,expected', [
    ('PublicKey', base64.b64encode(bytes([4]) * 32).decode(), 'server_public_key'),
    ('PresharedKey', base64.b64encode(bytes([5]) * 32).decode(), 'preshared_key'),
    ('PresharedKey', None, 'preshared_key'),
    ('Address', '10.0.0.3/32', 'address'),
    ('AllowedIPs', '10.0.0.0/24', 'allowed_ips'),
    ('Endpoint', 'old-vpn.example:65353', 'endpoint'),
])
def test_peer_found_does_not_hide_stale_flow_profile(client, monkeypatch, field, value, expected):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    fresh = config_text(private)
    stale = config_text(private, **{field: value})
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(stale), SID))
    before = dict(one('SELECT * FROM subscriptions WHERE id=?', (SID,)))
    key_before = dict(one('SELECT * FROM wgdashboard_keys WHERE credential_id=?', (SID,)))
    calls = panel_reader(monkeypatch, private, fresh)

    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200, response.text
    report = response.json()
    assert report['status'] == 'peer_found' and report['local_ready'] is True
    assert report['profile_status'] == 'differs_from_panel'
    assert report['profile_differences'] == [expected]
    assert report['can_restore'] is True
    assert f'/api/downloadPeer/wg0' in calls
    for secret in (private, wgdashboard.WGDashboard.identity(private),
                   'AgICAgICAg', 'AwMDAwMDAw', 'BAQEBAQEBA', 'BQUFBQUFBQ',
                   'old-vpn.example', 'vpn.example', 'wg.example', '10.0.0.', 'do-not-leak'):
        assert secret not in response.text
    assert dict(one('SELECT * FROM subscriptions WHERE id=?', (SID,))) == before
    assert dict(one('SELECT * FROM wgdashboard_keys WHERE credential_id=?', (SID,))) == key_before


def test_profile_comparison_ignores_format_and_uses_pinned_guest_interface(client, monkeypatch):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    fresh = config_text(private, PresharedKey=None)
    local = config_text(private, Address='2001:0db8:0:0:0:0:0:2/128, 10.0.0.2/32',
                        AllowedIPs='::/0, 0.0.0.0/0', Endpoint='VPN.EXAMPLE:51820',
                        PresharedKey=base64.b64encode(bytes(32)).decode())
    local = local.replace('[Interface]\n', '[Interface]\n# Saved profile\n\n').replace(' = ', '=')
    execute("UPDATE subscriptions SET secret=?,wg_configuration='wg-guest' WHERE id=?",
            (c.encrypt(local), SID))
    calls = panel_reader(monkeypatch, private, fresh, configuration='wg-guest')
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200, response.text
    assert response.json()['profile_status'] == 'matches_panel'
    assert response.json()['profile_differences'] == []
    assert '/api/downloadPeer/wg-guest' in calls
    assert '/api/downloadPeer/wg0' not in calls


@pytest.mark.parametrize('bad_local,expected', [
    ('not a configuration', 'local_invalid'),
    ('wrong-key', 'local_key_mismatch'),
    ('bad-endpoint', 'local_invalid'),
    ('bad-psk', 'local_invalid'),
])
def test_profile_comparison_never_matches_invalid_local_profile(client, monkeypatch, bad_local, expected):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    local = {
        'wrong-key': config_text(base64.b64encode(bytes([9]) * 32).decode()),
        'bad-endpoint': config_text(private, Endpoint='vpn.example:51820:51820'),
        'bad-psk': config_text(private, PresharedKey='not-a-key'),
    }.get(bad_local, bad_local)
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(local), SID))
    calls = panel_reader(monkeypatch, private, config_text(private))
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200
    assert response.json()['status'] == 'peer_found'
    assert response.json()['profile_status'] == expected
    assert response.json()['profile_differences'] == []
    assert not any('/downloadPeer/' in call for call in calls)


@pytest.mark.parametrize('export_kind', ['timeout', 'malformed', 'wrong-key', 'bad-psk'])
def test_failed_export_never_claims_matching_profile(client, monkeypatch, export_kind):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    local = config_text(private)
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(local), SID))
    exported = {
        'malformed': 'PRIVATE-ENDPOINT-KEY-TOKEN',
        'wrong-key': config_text(base64.b64encode(bytes([9]) * 32).decode()),
        'bad-psk': config_text(private, PresharedKey='PRIVATE-ENDPOINT-KEY-TOKEN'),
    }.get(export_kind, local)
    panel_reader(monkeypatch, private, exported, error=export_kind == 'timeout')
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200
    assert response.json()['status'] == 'peer_found'
    assert response.json()['profile_status'] == 'unavailable'
    assert response.json()['profile_differences'] == []
    assert 'PRIVATE-ENDPOINT-KEY-TOKEN' not in response.text
    assert c.decrypt(one('SELECT secret FROM subscriptions WHERE id=?', (SID,))['secret']) == local


@pytest.mark.parametrize('field,value', [
    ('MTU', '-1'), ('MTU', '2147483648'),
    ('ListenPort', '-1'), ('ListenPort', '65536'),
    ('PersistentKeepalive', '-1'), ('PersistentKeepalive', '65536'),
])
@pytest.mark.parametrize('invalid_local', [True, False], ids=['both-invalid', 'invalid-export'])
def test_out_of_range_numbers_never_report_matching_profiles(client, monkeypatch, field, value, invalid_local):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    invalid = config_text(private, **{field: value})
    local = invalid if invalid_local else config_text(private)
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(local), SID))
    calls = panel_reader(monkeypatch, private, invalid)
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200
    report = response.json()
    assert report['status'] == 'peer_found'
    assert report['profile_status'] == ('local_invalid' if invalid_local else 'unavailable')
    assert report['profile_differences'] == []
    assert any('/downloadPeer/' in call for call in calls) is not invalid_local
    assert c.decrypt(one('SELECT secret FROM subscriptions WHERE id=?', (SID,))['secret']) == local


@pytest.mark.parametrize('changes,valid_changes', [
    ({'MTU': '14_20'}, {}),
    ({'ListenPort': '65_535'}, {'ListenPort': '65535'}),
    ({'PersistentKeepalive': '2_5'}, {}),
    ({'PresharedKey': ''}, {'PresharedKey': None}),
    # Low padding bits differ, while Python's decoder returns the same 32 bytes.
    ({'PublicKey': base64.b64encode(bytes([2]) * 32).decode()[:-2] + 'J='}, {}),
    ({'PresharedKey': base64.b64encode(bytes([3]) * 32).decode()[:-2] + 'N='}, {}),
], ids=['mtu-separator', 'port-separator', 'keepalive-separator', 'empty-psk',
        'public-key-padding', 'psk-padding'])
@pytest.mark.parametrize('invalid_local', [True, False], ids=['both-invalid', 'invalid-export'])
def test_native_parser_rejected_aliases_never_match(client, monkeypatch, changes, valid_changes, invalid_local):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    invalid = config_text(private, **changes)
    local = invalid if invalid_local else config_text(private, **valid_changes)
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(local), SID))
    panel_reader(monkeypatch, private, invalid)
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200
    assert response.json()['status'] == 'peer_found'
    assert response.json()['profile_status'] == ('local_invalid' if invalid_local else 'unavailable')
    assert response.json()['profile_differences'] == []
    assert private not in response.text
    assert c.decrypt(one('SELECT secret FROM subscriptions WHERE id=?', (SID,))['secret']) == local


@pytest.mark.parametrize('mtu,port,keepalive', [
    ('0', '0', '0'), ('2147483647', '65535', '65535'), ('+0001420', '+65535', '00025'),
])
def test_native_parser_numeric_boundaries_remain_valid(client, monkeypatch, mtu, port, keepalive):
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    local = config_text(private, MTU=mtu, ListenPort=port, PersistentKeepalive=keepalive)
    # Native Config.parse treats a zero MTU as automatic, like an omitted MTU.
    exported = config_text(private, MTU=str(int(mtu)), ListenPort=str(int(port)),
                           PersistentKeepalive=str(int(keepalive)))
    if mtu == '0':
        exported = exported.replace('MTU = 0\n', '')
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(local), SID))
    panel_reader(monkeypatch, private, exported)
    response = client.get('/api/admin/users/1/wireguard/peer', headers=owner())
    assert response.status_code == 200
    assert response.json()['profile_status'] == 'matches_panel'
    assert response.json()['profile_differences'] == []


def test_explicit_refresh_repairs_feed_with_same_peer_key_and_entitlement(client, monkeypatch):
    from app import providers
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    fresh = config_text(private)
    stale = config_text(private, PublicKey=base64.b64encode(bytes([8]) * 32).decode())
    execute('UPDATE subscriptions SET secret=? WHERE id=?', (c.encrypt(stale), SID))
    before = dict(one('SELECT * FROM subscriptions WHERE id=?', (SID,)))
    key_before = dict(one('SELECT * FROM wgdashboard_keys WHERE credential_id=?', (SID,)))
    panel_reader(monkeypatch, private, fresh)

    async def forbid(_sub):
        raise AssertionError('An existing peer must not be reprovisioned')

    monkeypatch.setattr(providers, 'provision', forbid)
    url = f'/api/flow/wireguard/subscriptions/{SID}'
    old_feed = client.get(url).json()
    assert old_feed['profiles'][0]['config'] == stale
    report = client.get('/api/admin/users/1/wireguard/peer', headers=owner()).json()
    assert report['profile_status'] == 'differs_from_panel'
    assert client.get(url).json() == old_feed, 'Read-only diagnosis must preserve the feed'

    restored = client.post('/api/admin/users/1/wireguard/restore', headers=owner())
    assert restored.status_code == 200, restored.text
    assert restored.json()['status'] == 'refreshed'
    new_feed = client.get(url).json()
    assert new_feed['profiles'][0]['config'] == fresh
    assert new_feed['revision'] == old_feed['revision'] + 1
    after = dict(one('SELECT * FROM subscriptions WHERE id=?', (SID,)))
    assert {k: v for k, v in after.items() if k != 'secret'} == {
        k: v for k, v in before.items() if k != 'secret'}
    assert dict(one('SELECT * FROM wgdashboard_keys WHERE credential_id=?', (SID,))) == key_before
    checked = client.get('/api/admin/users/1/wireguard/peer', headers=owner()).json()
    assert checked['profile_status'] == 'matches_panel'


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
    ('unavailable', 'panel_connection_failed'),
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
    assert client.post('/api/admin/users/1/wireguard/restore').status_code == 401
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
    assert client.post('/api/admin/users/1/wireguard/restore').status_code == 403


def test_missing_wg_peer_is_restored_on_pinned_server_without_new_key(client, monkeypatch):
    from app import providers
    setup_user(client)
    private = wgdashboard.stored_private_key('test-wg', SID)
    original=one('SELECT * FROM subscriptions WHERE id=?', (SID,))
    encrypted=one('SELECT private_key FROM wgdashboard_keys WHERE credential_id=?',(SID,))['private_key']
    state={'peer':'peer_missing'}
    async def check(profile, public_key):
        assert profile['url']=='https://wg.example'
        assert public_key==wgdashboard.WGDashboard.identity(private)
        return state['peer']
    writes=[]
    async def provision(sub):
        writes.append(dict(sub))
        state['peer']='peer_found'
        return '[Interface]\nPrivateKey = '+private+'\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = '+wgdashboard.WGDashboard.identity(private)+'\nEndpoint = 193.238.155.39:51820\nAllowedIPs = 0.0.0.0/0\n'
    async def download(profile, private_key):
        assert private_key == private
        return c.decrypt(one('SELECT secret FROM subscriptions WHERE id=?',(SID,))['secret'])
    monkeypatch.setattr(peer,'_remote_peer',check)
    monkeypatch.setattr(peer,'_download_existing_config',download)
    monkeypatch.setattr(providers,'provision',provision)
    result=client.post('/api/admin/users/1/wireguard/restore',headers=owner())
    assert result.status_code==200,result.text
    assert result.json()=={'ok':True,'status':'restored'}
    restored=one('SELECT * FROM subscriptions WHERE id=?', (SID,))
    assert restored['expires']==original['expires']
    assert restored['server_id']=='test-wg'
    assert restored['user_id']==original['user_id']
    assert restored['secret']!=original['secret']
    assert 'Endpoint = 193.238.155.39:51820' in c.decrypt(restored['secret'])
    assert writes and len(writes)==1 and writes[0]['id']==SID
    assert one('SELECT private_key FROM wgdashboard_keys WHERE credential_id=?',(SID,))['private_key']==encrypted
    again=client.post('/api/admin/users/1/wireguard/restore',headers=owner())
    assert again.status_code==200 and again.json()['status']=='already_present'
    assert len(writes)==1


def test_failed_restore_retains_old_subscription_and_peer_key(client, monkeypatch):
    from app import providers
    setup_user(client)
    wgdashboard.stored_private_key('test-wg', SID)
    old=one('SELECT secret,expires FROM subscriptions WHERE id=?',(SID,))
    key=one('SELECT private_key FROM wgdashboard_keys WHERE credential_id=?',(SID,))['private_key']
    async def check(profile, pub):return 'peer_missing'
    async def failure(sub):raise TimeoutError('NEVER-SHOW-API-TOKEN-PRIVATEKEY')
    monkeypatch.setattr(peer,'_remote_peer',check)
    monkeypatch.setattr(providers,'provision',failure)
    result=client.post('/api/admin/users/1/wireguard/restore',headers=owner())
    assert result.status_code==502,result.text
    assert 'NEVER-SHOW' not in result.text
    after=one('SELECT secret,expires FROM subscriptions WHERE id=?',(SID,))
    assert after['secret']==old['secret'] and after['expires']==old['expires']
    assert one('SELECT private_key FROM wgdashboard_keys WHERE credential_id=?',(SID,))['private_key']==key


def test_restore_does_not_create_keys_or_migrate_old_server(client, monkeypatch):
    from app import providers
    setup_user(client)
    async def forbid(*args):raise AssertionError('No remote write without saved key')
    monkeypatch.setattr(providers,'provision',forbid)
    assert client.post('/api/admin/users/1/wireguard/restore',headers=owner()).status_code==409
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n']==0
    execute("UPDATE servers SET state='disabled' WHERE id='test-wg'")
    assert client.post('/api/admin/users/1/wireguard/restore',headers=owner()).status_code==409


def test_restore_rejects_pending_checkout_and_restricted_remote_peer(client,monkeypatch):
    from app import providers
    setup_user(client)
    wgdashboard.stored_private_key('test-wg', SID)
    async def restricted(profile,pub):return 'peer_restricted'
    async def forbid(*args):raise AssertionError('No remote write for restricted peer')
    monkeypatch.setattr(peer,'_remote_peer',restricted)
    monkeypatch.setattr(providers,'provision',forbid)
    assert client.post('/api/admin/users/1/wireguard/restore',headers=owner()).status_code==409
    execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status) "
            "VALUES ('pending-restoration',1,'wg','month',30,199,1,'pending')")
    assert client.post('/api/admin/users/1/wireguard/restore',headers=owner()).status_code==409



def test_remote_peer_found_can_refresh_stale_config_without_recreating_peer(client, monkeypatch):
    from app import providers
    setup_user(client)
    key=wgdashboard.stored_private_key('test-wg', SID)
    initial=one('SELECT expires,secret,server_id FROM subscriptions WHERE id=?',(SID,))
    async def present(profile, public):return 'peer_found'
    fresh='[Interface]\nPrivateKey = '+key+'\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = '+wgdashboard.WGDashboard.identity(key)+'\nEndpoint = 193.238.155.39:51820\nAllowedIPs = 0.0.0.0/0\n'
    async def downloaded(profile, private):
        assert private==key
        return fresh
    async def forbid(sub):
        raise AssertionError('Existing peer must never be re-created')
    monkeypatch.setattr(peer,'_remote_peer',present)
    monkeypatch.setattr(peer,'_download_existing_config',downloaded)
    monkeypatch.setattr(providers,'provision',forbid)
    result=client.post('/api/admin/users/1/wireguard/restore',headers=owner())
    assert result.status_code==200,result.text
    assert result.json()['status']=='refreshed'
    updated=one('SELECT expires,secret,server_id FROM subscriptions WHERE id=?',(SID,))
    assert updated['expires']==initial['expires'] and updated['server_id']==initial['server_id']
    assert c.decrypt(updated['secret'])==fresh
    again=client.post('/api/admin/users/1/wireguard/restore',headers=owner())
    assert again.status_code==200 and again.json()['status']=='already_present'


def test_background_username_refresh_never_rewrites_live_peer(client, monkeypatch):
    import asyncio
    from app import flow_presence, wg_activity

    setup_user(client)
    execute('UPDATE users SET telegram_id=? WHERE id=1', (123456789,))
    private = wgdashboard.stored_private_key('test-wg', SID)
    public = wgdashboard.WGDashboard.identity(private)
    execute('INSERT OR REPLACE INTO wg_peer_owners VALUES (?,?,?)',
            (1, '@new_username', int(time.time())))

    async def refresh_names(_client):
        # Nickname metadata remains available for newly created peers.
        pass

    monkeypatch.setattr(wg_activity, 'refresh_names', refresh_names)
    monkeypatch.setattr(flow_presence, '_until', 0)
    calls = []
    peer_row = {'id': public, 'name': 'Pushok · old username',
                'private_key': private, 'preshared_key': '', 'DNS': '1.1.1.1',
                'allowed_ip': '10.0.0.2/32', 'endpoint_allowed_ip': '0.0.0.0/0',
                'mtu': 1420, 'keepalive': 25, 'notes': '',
                'latest_handshake': '0:00:12'}

    def panel_request(request):
        calls.append((request.method, request.url.path))
        assert request.method == 'GET', 'Maintenance must not modify peer settings'
        assert request.url.path.endswith('/getWireguardConfigurationInfo')
        return httpx.Response(200, json={'status': True, 'data': {
            'configurationInfo': {'Name': 'wg0', 'Protocol': 'wg'},
            'configurationPeers': [peer_row], 'configurationRestrictedPeers': []}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(flow_presence.httpx, 'AsyncClient', lambda **kwargs:
        real_client(transport=httpx.MockTransport(panel_request), **kwargs))
    report = asyncio.run(flow_presence.snapshot(maintenance=True))
    assert calls == [('GET', '/api/getWireguardConfigurationInfo')]
    assert report['unlinked_activity']['123456789']['online'] is True
    assert one('SELECT label FROM wg_peer_owners WHERE user_id=1')['label'] == '@new_username'
    assert one('SELECT state FROM subscriptions WHERE id=?', (SID,))['state'] == 'active'
