"""TUIC v5: UUID + пароль, проверка транспорта и жизненного цикла доступа."""
import asyncio
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import config as c, providers, servers, service, sui
from app.db import execute, one
from app.panel import PanelError
from test_shop import client, register, order, confirm
from test_hysteria2 import remote as base_remote, OWNER, PASSWORD, IDENTITY


def profile(**changes):
    return servers.validated('tuic',{'api':'sui','url':'https://panel.example/private',
        'token':'t' * 48,'inbound_id':7,'address':'vpn.example','port':443,'sni':'vpn.example',**changes})


@pytest.fixture
def remote(base_remote):
    base_remote['inbound'].update(type='tuic',congestion_control='cubic',zero_rtt_handshake=False)
    for side in ('server','client'): base_remote['tls'][side]['alpn'] = ['h3']
    return base_remote


def call(remote,enabled=True,expires=2000000000,settings=None,password=PASSWORD):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote['handler'])) as http:
            return await sui.SUITUIC(settings or profile()).apply(http,
                {'id':IDENTITY,'expires':expires},password,enabled)
    return asyncio.run(run())


@pytest.mark.parametrize('algorithm',['cubic','new_reno','bbr'])
@pytest.mark.parametrize('udp',['native','quic'])
def test_tuic_link_and_panel_credentials_agree(remote,algorithm,udp):
    remote['inbound']['congestion_control'] = algorithm
    link = call(remote,settings=profile(address='2001:db8::1',congestion_control=algorithm,udp_relay_mode=udp))
    parsed = urlsplit(link)
    assert parsed.scheme == 'tuic' and parsed.hostname == '2001:db8::1' and parsed.port == 443
    assert parsed.username == IDENTITY and parsed.password == PASSWORD
    assert parse_qs(parsed.query) == {'security':['tls'],'sni':['vpn.example'],'alpn':['h3'],
        'insecure':['0'],'congestion_control':[algorithm],'udp_relay_mode':[udp]}
    auth = remote['records']['pushok_'+IDENTITY]['config']
    assert auth == {'tuic':{'name':'pushok_'+IDENTITY,'uuid':IDENTITY,'password':PASSWORD}}
    assert 'panel.example' not in link and 't' * 48 not in link


@pytest.mark.parametrize('fault',['wrong_protocol','alpn_missing','alpn_mismatch','insecure','rtt','rtt_number','congestion','obfs','udp_over_stream'])
def test_unsupported_transport_is_rejected_before_write(remote,fault):
    if fault == 'wrong_protocol': remote['inbound']['type'] = 'hysteria2'
    elif fault == 'alpn_missing': del remote['tls']['server']['alpn']
    elif fault == 'alpn_mismatch': remote['tls']['client']['alpn'] = ['h2']
    elif fault == 'insecure': remote['tls']['client']['insecure'] = True
    elif fault == 'rtt': remote['inbound']['zero_rtt_handshake'] = True
    elif fault == 'rtt_number': remote['inbound']['zero_rtt_handshake'] = 0
    elif fault == 'congestion': remote['inbound']['congestion_control'] = 'bbr'
    elif fault == 'obfs': remote['inbound']['obfs'] = {'type':'salamander'}
    else: remote['inbound']['udp_over_stream'] = True
    with pytest.raises(PanelError): call(remote)
    assert not remote['writes']


@pytest.mark.parametrize('fault',['uuid','password','name','binding','expiry','enable','protocol_config'])
def test_changed_identity_or_unconfirmed_write_does_not_issue_link(remote,fault):
    def corrupt(state,row):
        if fault in ('uuid','password','name'): row['config']['tuic'][fault] = 'foreign'
        elif fault == 'binding': row['inbounds'] = [7,8]
        elif fault == 'expiry': row['expiry'] -= 1
        elif fault == 'enable': row['enable'] = False
        else: row['config']['hysteria2'] = {'password':'foreign'}
    remote['fault'] = corrupt
    with pytest.raises(PanelError): call(remote)


def test_retry_renew_and_revoke_preserve_credentials_and_close_sessions(remote):
    remote['drop'] = True
    with pytest.raises(httpx.ReadTimeout): call(remote)
    link = call(remote)
    row = remote['records']['pushok_'+IDENTITY]; row.update(volume=1000,up=50,down=20,desc='keep')
    assert call(remote,expires=2000086400) == link
    row = remote['records']['pushok_'+IDENTITY]
    assert (row['expiry'],row['volume'],row['up'],row['down'],row['desc']) == (2000086400,1000,50,20,'keep')
    remote['inbound']['zero_rtt_handshake'] = True
    remote['sessions'] = [{'user':row['name']}];remote['close_ignore'] = True
    with pytest.raises(PanelError,match='SessionCloseNotConfirmed'): call(remote,False)
    assert remote['records'][row['name']]['enable'] is False
    remote['close_ignore'] = False
    assert call(remote,False) == '' and not remote['sessions'] and len(remote['records']) == 1


@pytest.mark.parametrize('changes',[{'api':'clients'},{'port':True},{'inbound_id':0},
    {'address':'https://vpn.example'},{'sni':'bad name'},{'congestion_control':'unknown'},
    {'udp_relay_mode':'stream'},{'zero_rtt_handshake':True},{'insecure':True},{'obfs_password':'secret'}])
def test_unsupported_profile_is_rejected(changes):
    with pytest.raises(ValueError): profile(**changes)


def setup_shop(client,remote,monkeypatch):
    cls = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw: cls(transport=httpx.MockTransport(remote['handler']),**kw))
    monkeypatch.setattr(providers,'available',servers.available)
    response = client.post('/api/admin/servers',headers=OWNER,json={'name':'TUIC test','protocol':'tuic','settings':profile()})
    assert response.status_code == 201,response.text
    server = response.json();assert server['state'] == 'disabled' and server['profile'] == 'sui_tuic'
    assert 'token' not in response.text and 'vpn.example' not in response.text
    assert client.post('/api/admin/servers/'+server['id']+'/check',headers=OWNER).json()['health'] == 'ok'
    response = client.put('/api/admin/servers/'+server['id'],headers=OWNER,
                         json={'name':'TUIC test','state':'active','version':server['version']})
    assert response.status_code == 200,response.text
    register(client)
    return server['id']


def test_http_purchase_retry_devices_rotation_renewal_and_expiry(client,remote,monkeypatch):
    from test_devices import add
    sid = setup_shop(client,remote,monkeypatch)
    paid = order(client,'tuic');confirm(client,paid['id']);remote['drop'] = True
    asyncio.run(service.tick());sub = one("SELECT * FROM subscriptions WHERE protocol='tuic'")
    assert sub['state'] == 'pending' and sub['secret'] is None
    password = sui.stored_password(sid,sub['id'],protocol='tuic')
    execute('UPDATE orders SET next_attempt=0 WHERE id=?',(paid['id'],));asyncio.run(service.tick())
    link = client.get('/api/subscriptions/'+sub['id']+'/config').json()['config']
    assert (urlsplit(link).username,urlsplit(link).password) == (sub['id'],password)
    assert client.get('/api/subscriptions/'+sub['id']+'/download').text == link
    assert client.get('/api/subscriptions/'+sub['id']+'/qr').headers['content-type'] == 'image/png'
    assert password not in client.get('/api/me').text
    extra = add(client,sub['id']).json();asyncio.run(service.process_devices())
    extra_link = client.get('/api/devices/'+extra['id']+'/config').json()['config']
    assert urlsplit(extra_link).password != password
    assert client.post('/api/subscriptions/'+sub['id']+'/rotate',json={'request_id':str(uuid.uuid4())}).status_code == 201
    asyncio.run(service.process_devices())
    rotated = client.get('/api/subscriptions/'+sub['id']+'/config').json()['config']
    assert urlsplit(rotated).password not in (password,urlsplit(extra_link).password)
    assert remote['records']['pushok_'+sub['id']]['enable'] is False
    target = one('SELECT expires FROM subscriptions WHERE id=?',(sub['id'],))['expires']
    renewal = order(client,'tuic');confirm(client,renewal['id']);asyncio.run(service.tick())
    assert one('SELECT expires FROM subscriptions WHERE id=?',(sub['id'],))['expires'] > target
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()['config'] == rotated
    response = client.put('/api/admin/servers/'+sid,headers=OWNER,json={
        'name':'TUIC test','state':'disabled','version':2,'settings':profile(udp_relay_mode='quic')})
    assert response.status_code == 409
    execute('UPDATE subscriptions SET expires=1 WHERE id=?',(sub['id'],))
    remote['sessions'] = [{'user':r['name']} for r in remote['records'].values() if r['enable']]
    asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions WHERE id=?',(sub['id'],))['state'] == 'expired'
    assert not remote['sessions'] and all(not r['enable'] for r in remote['records'].values())


def test_tuic_trial_coupon_and_unconfigured_server(client,remote,monkeypatch):
    from test_trials import telegram,policy,claim
    from test_promos import coupon,quote,purchase
    monkeypatch.setattr(providers,'available',servers.available)
    assert not next(p for p in client.get('/api/catalog').json()['protocols'] if p['id']=='tuic')['available']
    setup_shop(client,remote,monkeypatch);coupon(client,protocol='tuic')
    assert quote(client,protocol='xui').status_code == 409
    pricing = quote(client,protocol='tuic').json();paid = purchase(client,pricing).json()
    confirm(client,paid['id']);asyncio.run(service.fulfill(paid['id']))
    assert paid['amount'] == pricing['base_amount'] - pricing['discount']
    client.post('/api/auth/logout');monkeypatch.setattr(c,'BOT_TOKEN','123:test-token');monkeypatch.setattr(c,'BOT_USERNAME','test_bot')
    async def quiet(*args,**kw): return None
    monkeypatch.setattr(service,'send',quiet);execute('UPDATE store_settings SET terms_text=? WHERE id=1',('Trial terms',))
    telegram(client);assert policy(client,enabled=True).status_code == 200
    trial = claim(client,'tuic').json();assert trial['amount'] == 0 and trial['method'] == 'trial'
    asyncio.run(service.fulfill(trial['id']));assert one('SELECT status FROM orders WHERE id=?',(trial['id'],))['status'] == 'active'


def test_tuic_password_backup_and_protocol_isolation(client,remote,monkeypatch,tmp_path):
    import os,sqlite3
    from test_backup import b
    sid = setup_shop(client,remote,monkeypatch)
    password = sui.stored_password(sid,IDENTITY,protocol='tuic')
    assert sui.stored_password(sid,IDENTITY,protocol='tuic') == password
    assert one('SELECT count(*) AS n FROM hysteria2_credentials')['n'] == 0
    with pytest.raises(PanelError,match='ServerRequired'): sui.stored_password(sid,IDENTITY)
    assert sui.stored_password(sid,str(uuid.uuid4()),protocol='tuic',create=False) is None
    archive = tmp_path/'tuic.pvb'
    b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'test-backup-password-long')
    b.restore(archive,'test-backup-password-long',tmp_path/'restored')
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        row = conn.execute('SELECT password FROM tuic_credentials').fetchone()
    assert row[0] != password and c.decrypt(row[0]) == password
