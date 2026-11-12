"""Контракт S-UI: slim/full чтение, идемпотентная запись и отзыв QUIC-сессий."""
import asyncio
import copy
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import config as c, providers, servers, service, sui
from app.db import execute, one, init_db
from app.panel import PanelError
from test_shop import client, register, order, confirm


OWNER = {'Authorization':'Bearer ' + 'a' * 48}
PASSWORD = 'p' * 43
IDENTITY = '8ebcd9cb-49bd-4e17-ac7c-5863d6523638'


def profile(**changes):
    return servers.validated('hysteria2', {'api':'sui','url':'https://panel.example/private',
        'token':'t' * 48, 'inbound_id':7, 'address':'vpn.example','port':443,
        'sni':'vpn.example', **changes})


@pytest.fixture
def remote():
    state = {'records':{}, 'writes':[], 'closed':[], 'sessions':[], 'running':True,
             'drop':False, 'ignore':False, 'close_ignore':False, 'fault':None,
             'duplicates':False, 'refuse':False,
             'inbound':{'id':7,'type':'hysteria2','enable':True,'tls_id':2,'listen_port':443},
             'tls':{'id':2,'server':{'enabled':True,'server_name':'vpn.example'},
                    'client':{'enabled':True,'server_name':'vpn.example','insecure':False}}}

    def handler(request):
        assert request.headers['token'] == 't' * 48
        assert request.url.path.startswith('/private/apiv2/')
        path = request.url.path.rsplit('/',1)[1]
        if state['refuse']:
            return httpx.Response(200,json={'success':False,'msg':'upstream-secret','obj':None})
        if request.method == 'GET' and path == 'status':
            obj = {'sbd':{'running':state['running'],'maintenance':False}}
        elif request.method == 'GET' and path == 'inbounds':
            assert request.url.params['id'] == '7'
            obj = {'inbounds':[state['inbound']]}
        elif request.method == 'GET' and path == 'tls':
            obj = {'tls':[state['tls']]}
        elif request.method == 'GET' and path == 'clients':
            if 'id' in request.url.params:
                rows = [copy.deepcopy(row) for row in state['records'].values()
                        if str(row['id']) == request.url.params['id']]
            else:
                # Список панели не содержит config: перед правкой нужен запрос по id.
                rows = [{'id':r['id'],'name':r['name']} for r in state['records'].values()]
                if state['duplicates'] and rows:
                    rows.append(rows[0])
            obj = {'clients':rows}
        elif request.method == 'POST' and path == 'save':
            body = parse_qs(request.content.decode())
            assert body['object'] == ['clients']
            payload = json.loads(body['data'][0]); name = payload['name']
            existing = state['records'].get(name)
            assert body['action'] == (['edit'] if existing else ['new'])
            assert not existing or existing['id'] == payload['id']
            state['writes'].append(copy.deepcopy(payload))
            if not state['ignore']:
                saved = copy.deepcopy(payload)
                if existing:
                    assert payload['up'] or payload['down'], 'Случайный сброс счётчиков панели'
                    saved.update(up=existing['up'],down=existing['down'])
                else:
                    saved['id'] = len(state['records']) + 41
                state['records'][name] = saved
                if state['fault']:
                    state['fault'](state, saved)
            if state['drop']:
                state['drop'] = False
                raise httpx.ReadTimeout('upstream-secret',request=request)
            obj = {}
        elif request.method == 'POST' and path == 'closeSessions':
            name = parse_qs(request.content.decode())['u'][0]
            state['closed'].append(name)
            if not state['close_ignore']:
                state['sessions'] = [row for row in state['sessions'] if row['user'] != name]
            obj = None
        elif request.method == 'GET' and path == 'sessions':
            assert request.url.params['resource'] == 'user'
            obj = [row for row in state['sessions'] if row['user'] == request.url.params['tag']]
        else:
            raise AssertionError((request.method,path))
        return httpx.Response(200,json={'success':True,'obj':copy.deepcopy(obj)})
    state['handler'] = handler
    return state


def call(remote, enabled=True, expires=2000000000, settings=None, password=PASSWORD):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote['handler'])) as http:
            return await sui.SUIHysteria2(settings or profile()).apply(http,
                {'id':IDENTITY,'expires':expires}, password, enabled)
    return asyncio.run(run())


def test_create_retry_renew_and_close_sessions_preserve_password_limits_and_usage(remote):
    remote['drop'] = True
    with pytest.raises(httpx.ReadTimeout): call(remote)
    link = call(remote)
    parsed = urlsplit(link)
    assert parsed.scheme == 'hysteria2' and parsed.username == PASSWORD
    assert parsed.hostname == 'vpn.example' and parsed.port == 443
    assert parse_qs(parsed.query) == {'sni':['vpn.example'],'insecure':['0']}
    row = remote['records']['pushok_' + IDENTITY]
    row.update(volume=1000,up=20,down=30,desc='Keep me')
    assert call(remote,expires=2000086400) == link
    assert row['config']['hysteria2']['password'] == PASSWORD
    row = remote['records']['pushok_' + IDENTITY]
    assert (row['volume'],row['up'],row['down'],row['desc']) == (1000,20,30,'Keep me')
    remote['sessions'] = [{'user':row['name'],'id':'open-quic'}]
    assert call(remote,False,expires=2000086400) == ''
    assert remote['records'][row['name']]['enable'] is False and not remote['sessions']
    assert len(remote['records']) == 1 and remote['closed'] == [row['name']]


def test_salamander_and_ipv6_are_encoded_without_panel_credentials(remote):
    secret = 'salamander:/?&@#password'
    remote['inbound']['obfs'] = {'type':'salamander','password':secret}
    link = call(remote,settings=profile(address='2001:db8::1',obfs_password=secret))
    parsed = urlsplit(link)
    assert parsed.hostname == '2001:db8::1' and parsed.port == 443
    assert parse_qs(parsed.query)['obfs-password'] == [secret]
    assert 't' * 48 not in link and 'panel.example' not in link


@pytest.mark.parametrize('fault', ['type','id','disabled','tls','sni','insecure','ech','obfs','core'])
def test_wrong_inbound_or_stopped_core_never_creates_access(remote, fault):
    if fault == 'type': remote['inbound']['type'] = 'hysteria'
    elif fault == 'id': remote['inbound']['id'] = 7.0
    elif fault == 'disabled': remote['inbound']['enable'] = False
    elif fault == 'tls': remote['tls']['server']['enabled'] = False
    elif fault == 'sni': remote['tls']['client']['server_name'] = 'other.example'
    elif fault == 'insecure': remote['tls']['client']['insecure'] = True
    elif fault == 'ech': remote['tls']['server']['ech'] = {'enabled':True}
    elif fault == 'obfs': remote['inbound']['obfs'] = {'type':'gecko','password':'other'}
    else: remote['running'] = False
    with pytest.raises(PanelError): call(remote)
    assert not remote['writes']


@pytest.mark.parametrize('fault', ['password','auth_name','bindings','float_binding','expiry','bool_expiry',
                                 'enable','delayStart','autoReset','quota','core','tls','duplicate'])
def test_success_response_without_confirmed_access_is_not_issued(remote, fault):
    def corrupt(state, row):
        if fault == 'password': row['config']['hysteria2']['password'] = 'wrong'
        elif fault == 'auth_name': row['config']['hysteria2']['name'] = 'someone_else'
        elif fault == 'bindings': row['inbounds'] = [7,8]
        elif fault == 'float_binding': row['inbounds'] = [7.0]
        elif fault == 'expiry': row['expiry'] -= 1
        elif fault == 'bool_expiry': row['expiry'] = True
        elif fault == 'enable': row['enable'] = 1
        elif fault in ('delayStart','autoReset'): row[fault] = True
        elif fault == 'quota': row.update(volume=10,up=5,down=5)
        elif fault == 'core': state['running'] = False
        elif fault == 'tls': state['tls']['client']['insecure'] = True
        else: state['duplicates'] = True
    remote['fault'] = corrupt
    with pytest.raises(PanelError): call(remote)


def test_revoke_persists_disabled_before_core_check_and_retries_session_close(remote):
    call(remote); name = 'pushok_' + IDENTITY
    remote['inbound']['enable'] = False
    remote['tls']['client']['insecure'] = True
    remote['running'] = False
    with pytest.raises(PanelError,match='SUICoreUnavailable'): call(remote,False)
    assert remote['records'][name]['enable'] is False
    remote['running'] = True; remote['close_ignore'] = True
    remote['sessions'] = [{'user':name}]
    with pytest.raises(PanelError,match='SUISessionCloseNotConfirmed'): call(remote,False)
    remote['close_ignore'] = False
    assert call(remote,False) == '' and not remote['sessions']


def test_missing_client_revoke_closes_old_sessions_without_creating_record(remote):
    remote['sessions'] = [{'user':'pushok_' + IDENTITY}]
    assert call(remote,False,password=None) == ''
    assert not remote['records'] and not remote['writes'] and not remote['sessions']


def test_foreign_credentials_are_not_overwritten_or_revoked(remote):
    call(remote)
    before = len(remote['writes'])
    remote['records']['pushok_' + IDENTITY]['config']['hysteria2']['password'] = 'foreign'
    for enabled in (True,False):
        with pytest.raises(PanelError,match='IdentityMismatch'): call(remote,enabled)
    assert len(remote['writes']) == before and not remote['closed']


def test_api_failure_is_fixed_local_error_without_secret(remote):
    remote['refuse'] = True
    with pytest.raises(PanelError,match='^SUIOperationFailed$'): call(remote)


@pytest.mark.parametrize('changes', [
    {'api':'clients'}, {'port':True}, {'port':65536}, {'inbound_id':True},
    {'address':'https://vpn.example'}, {'address':'user@host'}, {'address':'fe80::1%eth0'},
    {'sni':'bad name'}, {'token':'t' * 40 + '\n'}, {'obfs_password':'short'},
    {'obfs_password':'password with space'}, {'insecure':True}, {'network':'tcp'},
])
def test_invalid_profiles_rejected(changes):
    with pytest.raises(ValueError): profile(**changes)


def setup_shop(client, remote, monkeypatch):
    cls = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw: cls(transport=httpx.MockTransport(remote['handler']),**kw))
    monkeypatch.setattr(providers,'available',servers.available)
    response = client.post('/api/admin/servers',headers=OWNER,
                          json={'name':'Hysteria test','protocol':'hysteria2','settings':profile()})
    assert response.status_code == 201, response.text
    server = response.json()
    assert server['profile'] == 'sui_hysteria2' and server['state'] == 'disabled'
    assert 'token' not in response.text and 'vpn.example' not in response.text
    assert client.post('/api/admin/servers/' + server['id'] + '/check',headers=OWNER).status_code == 200
    response = client.put('/api/admin/servers/' + server['id'],headers=OWNER,
                         json={'name':server['name'],'state':'active','version':server['version']})
    assert response.status_code == 200, response.text
    register(client)
    return server['id']


def test_http_purchase_retry_renew_devices_rotation_and_expiry(client, remote, monkeypatch):
    from test_devices import add
    sid = setup_shop(client,remote,monkeypatch)
    paid = order(client,'hysteria2'); confirm(client,paid['id'])
    remote['drop'] = True; asyncio.run(service.tick())
    sub = one("SELECT * FROM subscriptions WHERE protocol='hysteria2'")
    assert sub['state'] == 'pending' and sub['secret'] is None
    password = sui.stored_password(sid,sub['id'])
    assert password != sub['id'] and len(password) == 43
    init_db()
    assert sui.stored_password(sid,sub['id']) == password
    execute('UPDATE orders SET next_attempt=0 WHERE id=?',(paid['id'],)); asyncio.run(service.tick())
    sub = one('SELECT * FROM subscriptions WHERE id=?',(sub['id'],))
    link = client.get('/api/subscriptions/' + sub['id'] + '/config').json()['config']
    assert urlsplit(link).username == password and sub['secret'] != link
    assert client.get('/api/subscriptions/' + sub['id'] + '/download').text == link
    assert client.get('/api/subscriptions/' + sub['id'] + '/qr').headers['content-type'] == 'image/png'
    assert password not in client.get('/api/me').text
    encrypted = one('SELECT password FROM hysteria2_credentials')['password']
    assert encrypted != password and c.decrypt(encrypted) == password
    extra = add(client,sub['id']).json(); asyncio.run(service.process_devices())
    extra_link = client.get('/api/devices/' + extra['id'] + '/config').json()['config']
    assert urlsplit(extra_link).username != password
    assert client.post('/api/subscriptions/' + sub['id'] + '/rotate',json={'request_id':str(uuid.uuid4())}).status_code == 201
    asyncio.run(service.process_devices())
    rotated = client.get('/api/subscriptions/' + sub['id'] + '/config').json()['config']
    assert urlsplit(rotated).username not in (password,urlsplit(extra_link).username)
    assert remote['records']['pushok_' + sub['id']]['enable'] is False
    renewal = order(client,'hysteria2'); confirm(client,renewal['id']); asyncio.run(service.tick())
    assert one('SELECT expires FROM subscriptions WHERE id=?',(sub['id'],))['expires'] > sub['expires']
    response = client.put('/api/admin/servers/' + sid,headers=OWNER,json={
        'name':'Changed','state':'disabled','version':2,'settings':profile(address='other.example')})
    assert response.status_code == 409
    execute('UPDATE subscriptions SET expires=1 WHERE id=?',(sub['id'],))
    active = [r['name'] for r in remote['records'].values() if r['enable']]
    remote['sessions'] = [{'user':name} for name in active];remote['close_ignore'] = True
    asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions WHERE id=?',(sub['id'],))['state'] == 'active'
    remote['close_ignore'] = False; asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions WHERE id=?',(sub['id'],))['state'] == 'expired'
    assert not remote['sessions'] and all(r['enable'] is False for r in remote['records'].values())
    assert client.get('/api/subscriptions/' + sub['id'] + '/config').status_code == 404


def test_unconfigured_hysteria2_is_not_sold(client,monkeypatch):
    monkeypatch.setattr(providers,'available',servers.available); register(client)
    assert client.post('/api/orders',json={'plan':'month','protocol':'hysteria2'}).status_code == 503
    assert not next(p for p in client.get('/api/catalog').json()['protocols'] if p['id']=='hysteria2')['available']


def test_trial_and_scoped_coupon_use_hysteria2_adapter(client,remote,monkeypatch):
    from test_trials import telegram,policy,claim
    from test_promos import coupon,quote,purchase
    setup_shop(client,remote,monkeypatch)
    coupon(client,protocol='hysteria2')
    assert quote(client,protocol='xui').status_code == 409
    pricing = quote(client,protocol='hysteria2').json()
    paid = purchase(client,pricing).json();confirm(client,paid['id']);asyncio.run(service.fulfill(paid['id']))
    assert paid['amount'] == pricing['base_amount'] - pricing['discount']
    client.post('/api/auth/logout')
    monkeypatch.setattr(c,'BOT_TOKEN','123:test-token');monkeypatch.setattr(c,'BOT_USERNAME','test_bot')
    async def quiet(*args,**kw): return None
    monkeypatch.setattr(service,'send',quiet)
    execute('UPDATE store_settings SET terms_text=? WHERE id=1',('Trial terms',))
    telegram(client);assert policy(client,enabled=True).status_code == 200
    trial = claim(client,'hysteria2').json()
    assert trial['method'] == 'trial' and trial['amount'] == 0 and not trial['payment_url']
    asyncio.run(service.fulfill(trial['id']))
    assert one('SELECT status FROM orders WHERE id=?',(trial['id'],))['status'] == 'active'
    assert claim(client,'hysteria2').json()['id'] == trial['id']
    assert len(remote['records']) == 2


def test_credentials_are_atomic_and_missing_key_is_not_regenerated_during_revoke(client,remote,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    sid = setup_shop(client,remote,monkeypatch)
    with ThreadPoolExecutor(max_workers=6) as pool:
        passwords = list(pool.map(lambda _: sui.stored_password(sid,IDENTITY), range(12)))
    assert len(set(passwords)) == 1
    assert one('SELECT count(*) AS n FROM hysteria2_credentials')['n'] == 1
    assert sui.stored_password(sid,str(uuid.uuid4()),create=False) is None
    assert one('SELECT count(*) AS n FROM hysteria2_credentials')['n'] == 1
    with pytest.raises(PanelError,match='ServerRequired'): sui.stored_password('test-wg',IDENTITY)
    with pytest.raises(PanelError,match='InvalidSubscriptionIdentity'): sui.stored_password(sid,'../other')


def test_password_survives_real_encrypted_backup_restore(client,remote,monkeypatch,tmp_path):
    import os,sqlite3
    from test_backup import b
    sid = setup_shop(client,remote,monkeypatch)
    password = sui.stored_password(sid,IDENTITY)
    archive = tmp_path/'hy.pvb'
    b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'test-backup-password-long')
    b.restore(archive,'test-backup-password-long',tmp_path/'restored-hy')
    with sqlite3.connect(tmp_path/'restored-hy/shop.db') as conn:
        row = conn.execute('SELECT server_id,credential_id,password FROM hysteria2_credentials').fetchone()
    assert row[:2] == (sid,IDENTITY) and row[2] != password and c.decrypt(row[2]) == password
