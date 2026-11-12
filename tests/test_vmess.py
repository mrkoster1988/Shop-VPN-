"""VMess AEAD: явный контракт панели и полный путь оплаченного доступа."""
import asyncio
import base64
import copy
import json
import uuid
from urllib.parse import unquote

import httpx
import pytest

from app import config as c, panel, providers, servers, service
from app.db import execute, one
from test_shop import client, register, order, confirm


OWNER = {'Authorization':'Bearer ' + 'a' * 48}


def profile(**changes):
    return servers.validated('vmess', {
        'api':'clients', 'url':'https://panel.example/private', 'token':'t' * 48,
        'inbound_id':7, 'address':'vpn.example', 'port':443,
        'network':'tcp', 'sni':'vpn.example', **changes})


@pytest.fixture
def remote():
    state = {'record':None, 'records':{}, 'writes':[], 'drop':False, 'ignore':False, 'corrupt':None,
             'bindings':[7], 'inbound':{'id':7, 'protocol':'vmess', 'enable':True,
             'streamSettings':{'network':'tcp', 'security':'tls',
                               'tcpSettings':{'header':{'type':'none'}},
                               'tlsSettings':{'serverName':'vpn.example'}}}}

    def handler(request):
        assert request.headers['authorization'] == 'Bearer ' + 't' * 48
        assert request.url.path.startswith('/private/panel/api/')
        path = request.url.path
        if path.endswith('/inbounds/get/7'):
            obj = copy.deepcopy(state['inbound'])
        elif path.endswith('/clients/list/paged'):
            search = request.url.params.get('search', '')
            rows = [{'email':row['email']} for row in state['records'].values()
                    if not search or search in row['email']]
            obj = {'items':rows, 'filtered':len(rows)}
        elif '/clients/get/' in path:
            row = state['records'].get(unquote(path.rsplit('/', 1)[1]))
            if not row:
                return httpx.Response(404)
            obj = {'client':copy.deepcopy(row), 'inboundIds':state['bindings']}
        elif path.endswith('/clients/add') or '/clients/update/' in path:
            body = json.loads(request.content)
            payload = body['client'] if 'client' in body else body
            if 'client' in body:
                assert payload['email'] not in state['records'], 'Повтор создал второй UUID'
                assert body['inboundIds'] == [7]
            state['writes'].append(copy.deepcopy(payload))
            if not state['ignore']:
                state['record'] = {**payload, 'uuid':payload['id'], 'id':31}
                state['records'][payload['email']] = state['record']
                if state['corrupt']:
                    state['corrupt'](state)
            if state['drop']:
                state['drop'] = False
                raise httpx.ReadTimeout('upstream secret', request=request)
            obj = None
        else:
            raise AssertionError('Неожиданный вызов панели: ' + path)
        return httpx.Response(200, json={'success':True, 'obj':obj})

    state['handler'] = handler
    return state


def call(remote, sub, enabled=True, settings=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote['handler'])) as http:
            return await panel.adapter(settings or profile(), 'vmess').apply(http, sub, enabled)
    return asyncio.run(run())


def decoded(link):
    assert link.startswith('vmess://')
    return json.loads(base64.b64decode(link[8:], validate=True))


def subscription():
    return {'id':'8ebcd9cb-49bd-4e17-ac7c-5863d6523638', 'expires':2000000000}


def test_create_renew_revoke_and_export_identity(remote):
    sub = subscription()
    first = call(remote, sub)
    data = decoded(first)
    assert data['id'] == sub['id'] and data['aid'] == '0' and data['scy'] == 'auto'
    assert data['tls'] == 'tls' and data['insecure'] == '0'
    assert data['add'] == 'vpn.example' and data['port'] == '443'
    assert 't' * 48 not in json.dumps(data) and 'panel.example' not in json.dumps(data)
    assert 'flow' not in remote['writes'][0]
    remote['record'].update(totalGB=54321, limitIp=3, comment='keep')
    sub['expires'] += 86400
    assert call(remote, sub) == first
    assert remote['record']['expiryTime'] == sub['expires'] * 1000
    assert remote['record']['totalGB'] == 54321 and remote['record']['comment'] == 'keep'
    assert call(remote, sub, False) == ''
    assert remote['record']['enable'] is False
    assert {r['id'] for r in remote['writes']} == {sub['id']}


def test_loss_of_write_response_reuses_same_client(remote):
    sub = subscription(); remote['drop'] = True
    with pytest.raises(httpx.ReadTimeout):
        call(remote, sub)
    assert remote['record']['uuid'] == sub['id']
    assert decoded(call(remote, sub))['id'] == sub['id']
    assert len(remote['writes']) == 2


@pytest.mark.parametrize('fault', ['uuid', 'email', 'subId', 'binding', 'float_binding', 'expiry', 'bool_expiry', 'enable', 'security', 'alterId'])
def test_acknowledgement_without_matching_state_never_returns_profile(remote, fault):
    def corrupt(state):
        row = state['record']
        if fault == 'binding': state['bindings'] = [7, 8]
        elif fault == 'float_binding': state['bindings'] = [7.0]
        elif fault == 'expiry': row['expiryTime'] -= 1
        elif fault == 'bool_expiry': row['expiryTime'] = True
        elif fault == 'enable': row['enable'] = 1
        elif fault == 'alterId': row['alterId'] = 64
        else: row[fault] = 'unexpected'
    remote['corrupt'] = corrupt
    with pytest.raises(panel.PanelError): call(remote, subscription())


@pytest.mark.parametrize('fault', ['protocol', 'id', 'disabled', 'tls', 'network', 'header', 'sni', 'schema'])
def test_wrong_inbound_is_rejected_before_write(remote, fault):
    inbound = remote['inbound']; stream = inbound['streamSettings']
    if fault == 'protocol': inbound['protocol'] = 'vless'
    elif fault == 'id': inbound['id'] = 8
    elif fault == 'disabled': inbound['enable'] = False
    elif fault == 'tls': stream['security'] = 'none'
    elif fault == 'network': stream['network'] = 'grpc'
    elif fault == 'header': stream['tcpSettings']['header']['type'] = 'http'
    elif fault == 'sni': stream['tlsSettings']['serverName'] = 'other.example'
    else: inbound['streamSettings'] = '{broken'
    with pytest.raises(panel.PanelError): call(remote, subscription())
    assert not remote['writes']


def test_websocket_exports_verified_path_and_host(remote):
    remote['inbound']['streamSettings'].update(network='ws', wsSettings={'path':'/vpn','headers':{'Host':'vpn.example'}})
    settings = profile(network='ws', path='/vpn', ws_host='vpn.example')
    data = decoded(call(remote, subscription(), settings=settings))
    assert data['net'] == 'ws' and data['path'] == '/vpn' and data['host'] == 'vpn.example'
    remote['inbound']['streamSettings']['wsSettings']['path'] = '/changed'
    with pytest.raises(panel.PanelError, match='WebSocketMismatch'):
        call(remote, subscription(), settings=settings)


def test_transport_change_after_write_withholds_profile(remote):
    remote['corrupt'] = lambda state: state['inbound']['streamSettings'].update(network='grpc')
    with pytest.raises(panel.PanelError, match='TransportMismatch'): call(remote, subscription())


def test_revoke_survives_transport_or_security_drift_and_absent_is_noop(remote):
    sub = subscription()
    assert call(remote, sub, False) == '' and not remote['writes']
    call(remote, sub)
    remote['inbound']['enable'] = False
    remote['inbound']['streamSettings'] = None
    remote['record']['security'] = 'changed'
    remote['record']['alterId'] = 64
    assert call(remote, sub, False) == '' and remote['record']['enable'] is False


@pytest.mark.parametrize('changes', [
    {'api':'legacy'}, {'api':'remnawave'}, {'network':'grpc'}, {'port':True}, {'port':65536},
    {'address':'https://vpn.example'}, {'address':'vpn.example:443'}, {'sni':'bad name'},
    {'address':'host/path'}, {'address':'user@host'}, {'address':'fe80::1%eth0'},
    {'network':'tcp','path':'/vpn'}, {'network':'ws','path':''}, {'network':'ws','path':'/v?ed=2048'},
    {'token':'t' * 40 + '\n'}, {'flow':'xtls-rprx-vision'}, {'insecure':True},
])
def test_invalid_or_unsupported_settings_are_rejected(changes):
    with pytest.raises(ValueError): profile(**changes)


def setup_shop(client, remote, monkeypatch):
    cls = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kw: cls(transport=httpx.MockTransport(remote['handler']), **kw))
    monkeypatch.setattr(providers, 'available', servers.available)
    response = client.post('/api/admin/servers', headers=OWNER,
                           json={'name':'VMess test','protocol':'vmess','settings':profile()})
    assert response.status_code == 201, response.text
    server = response.json()
    assert server['profile'] == 'clients_vmess' and server['state'] == 'disabled'
    assert 'token' not in response.text and 'vpn.example' not in response.text
    assert client.post('/api/admin/servers/' + server['id'] + '/check', headers=OWNER).status_code == 200
    updated = client.put('/api/admin/servers/' + server['id'], headers=OWNER,
        json={'name':server['name'],'state':'active','version':server['version']})
    assert updated.status_code == 200, updated.text
    register(client)
    return server['id']


def test_purchase_retry_renew_download_expire_and_bound_profile(client, remote, monkeypatch):
    sid = setup_shop(client, remote, monkeypatch)
    assert next(p for p in client.get('/api/catalog').json()['protocols'] if p['id']=='vmess')['available']
    purchase = order(client, 'vmess'); confirm(client, purchase['id'])
    remote['drop'] = True
    asyncio.run(service.tick())
    sub = one("SELECT * FROM subscriptions WHERE protocol='vmess'")
    assert sub['state'] == 'pending' and sub['secret'] is None
    assert client.get('/api/subscriptions/' + sub['id'] + '/config').status_code == 404
    target = one('SELECT target_expires FROM orders WHERE id=?',(purchase['id'],))['target_expires']
    execute('UPDATE orders SET next_attempt=0 WHERE id=?',(purchase['id'],))
    asyncio.run(service.tick())
    ready = one('SELECT * FROM subscriptions WHERE id=?',(sub['id'],))
    assert ready['state'] == 'active' and ready['expires'] == target and ready['server_id'] == sid
    config = client.get('/api/subscriptions/' + sub['id'] + '/config').json()['config']
    assert decoded(config)['id'] == remote['record']['uuid']
    assert ready['secret'] != config
    assert client.get('/api/subscriptions/' + sub['id'] + '/download').text == config
    assert client.get('/api/subscriptions/' + sub['id'] + '/qr').headers['content-type'] == 'image/png'
    renewal = order(client, 'vmess'); confirm(client, renewal['id']); asyncio.run(service.tick())
    renewed = one('SELECT * FROM subscriptions WHERE id=?',(sub['id'],))
    assert renewed['expires'] > target and decoded(c.decrypt(renewed['secret']))['id'] == decoded(config)['id']
    changed = client.put('/api/admin/servers/' + sid, headers=OWNER, json={
        'name':'VMess test','state':'disabled','version':2,'settings':profile(address='other.example')})
    assert changed.status_code == 409
    execute('UPDATE subscriptions SET expires=1 WHERE id=?',(sub['id'],))
    remote['ignore'] = True; asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions WHERE id=?',(sub['id'],))['state'] == 'active'
    remote['ignore'] = False; asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions WHERE id=?',(sub['id'],))['state'] == 'expired'
    assert remote['record']['enable'] is False
    assert client.get('/api/subscriptions/' + sub['id'] + '/config').status_code == 404


def test_unconfigured_vmess_cannot_be_purchased(client, monkeypatch):
    monkeypatch.setattr(providers, 'available', servers.available)
    register(client)
    assert client.post('/api/orders',json={'plan':'month','protocol':'vmess'}).status_code == 503
    assert not next(p for p in client.get('/api/catalog').json()['protocols'] if p['id']=='vmess')['available']


def test_additional_device_rotation_and_expiry_use_separate_vmess_keys(client, remote, monkeypatch):
    from test_devices import add
    setup_shop(client, remote, monkeypatch)
    purchase = order(client, 'vmess'); confirm(client, purchase['id']); asyncio.run(service.tick())
    sub = one("SELECT * FROM subscriptions WHERE protocol='vmess'")
    device = add(client, sub['id']).json(); asyncio.run(service.process_devices())
    extra = client.get('/api/devices/' + device['id'] + '/config').json()['config']
    extra_id = decoded(extra)['id']
    original_id = decoded(c.decrypt(sub['secret']))['id']
    assert extra_id != original_id and len(remote['records']) == 2
    assert client.post('/api/subscriptions/' + sub['id'] + '/rotate',
                       json={'request_id':str(uuid.uuid4())}).status_code == 201
    asyncio.run(service.process_devices())
    rotated = client.get('/api/subscriptions/' + sub['id'] + '/config').json()['config']
    assert decoded(rotated)['id'] not in (original_id, extra_id)
    assert remote['records']['pushok_' + original_id]['enable'] is False
    assert client.get('/api/devices/' + device['id'] + '/config').json()['config'] == extra
    renewal = order(client, 'vmess'); confirm(client, renewal['id']); asyncio.run(service.tick())
    assert remote['records']['pushok_' + extra_id]['expiryTime'] > sub['expires'] * 1000
    execute('UPDATE subscriptions SET expires=1 WHERE id=?', (sub['id'],))
    asyncio.run(service.tick())
    assert all(record['enable'] is False for record in remote['records'].values())
    assert client.get('/api/devices/' + device['id'] + '/config').status_code == 404


def test_vmess_trial_is_issued_once_without_payment(client, remote, monkeypatch):
    from test_trials import telegram, policy, claim
    setup_shop(client, remote, monkeypatch)
    client.post('/api/auth/logout')
    monkeypatch.setattr(c, 'BOT_TOKEN', '123:trial-test-token')
    monkeypatch.setattr(c, 'BOT_USERNAME', 'trial_test_bot')
    async def no_outgoing_message(*args, **kwargs):
        return None
    monkeypatch.setattr(service, 'send', no_outgoing_message)
    execute('UPDATE store_settings SET terms_text=? WHERE id=1', ('Trial terms',))
    telegram(client)
    assert policy(client, enabled=True).status_code == 200
    response = claim(client, 'vmess')
    assert response.status_code == 200, response.text
    trial = response.json()
    assert trial['method'] == 'trial' and trial['amount'] == 0 and not trial['payment_url']
    asyncio.run(service.fulfill(trial['id']))
    sub = one("SELECT * FROM subscriptions WHERE protocol='vmess'")
    assert sub['state'] == 'active' and sub['access_source'] == 'trial'
    assert decoded(client.get('/api/subscriptions/' + sub['id'] + '/config').json()['config'])['id'] == sub['id']
    assert claim(client, 'vmess').json()['id'] == trial['id']
    asyncio.run(service.fulfill(trial['id']))
    assert len(remote['records']) == 1 and len(remote['writes']) == 1


def test_vmess_coupon_scope_and_discount_reach_issued_order(client, remote, monkeypatch):
    from test_promos import coupon, quote, purchase
    setup_shop(client, remote, monkeypatch)
    coupon(client, protocol='vmess')
    assert quote(client, protocol='xui').status_code == 409
    response = quote(client, protocol='vmess')
    assert response.status_code == 200, response.text
    pricing = response.json()
    assert pricing['discount'] > 0 and pricing['amount'] == pricing['base_amount'] - pricing['discount']
    response = purchase(client, pricing)
    assert response.status_code == 200, response.text
    paid = response.json()
    assert paid['amount'] == pricing['amount']
    confirm(client, paid['id']); asyncio.run(service.fulfill(paid['id']))
    assert one('SELECT status FROM orders WHERE id=?', (paid['id'],))['status'] == 'active'
    assert one('SELECT count(*) AS n FROM coupon_uses')['n'] == 1
    assert len(remote['records']) == 1
