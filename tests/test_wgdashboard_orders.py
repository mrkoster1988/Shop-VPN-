import asyncio
import json
import pytest
from app import config as c, service, providers, wgdashboard, servers
from app.db import execute, one
from test_shop import client, register, order, confirm


def test_paid_order_renewal_and_expiry_use_same_peer(client, monkeypatch):
    profile = {'api':'wgdashboard','url':'https://wg.example','token':'t'*40,'configuration':'wg0'}
    execute('UPDATE servers SET settings=? WHERE id=?',(c.encrypt(json.dumps(profile)),'test-wg'))
    calls = []
    class Adapter:
        def __init__(self, settings):assert settings == profile
        async def ensure_peer(self, client, key, name):calls.append(('ensure',key))
        async def download_config(self, client, key):return '[Interface]\nPrivateKey = '+key
        def identity(self,key):return key
        async def revoke_peer(self,client,key):calls.append(('revoke',key))
    monkeypatch.setattr(wgdashboard,'WGDashboard',Adapter)
    # Хранилище также проверяет ключ, поэтому сохраняем настоящую функцию проверки.
    Adapter.identity = staticmethod(lambda key:key)
    register(client)
    purchase = order(client,'wg')
    assert confirm(client,purchase['id']).status_code == 200
    asyncio.run(service.fulfill(purchase['id']))
    first = one('SELECT * FROM subscriptions')
    assert first['state'] == 'active'
    assert one('SELECT status FROM orders WHERE id=?',(purchase['id'],))['status'] == 'active'
    renewal = order(client,'wg')
    confirm(client,renewal['id'])
    asyncio.run(service.fulfill(renewal['id']))
    assert one('SELECT expires FROM subscriptions')['expires'] == first['expires'] + 30*86400
    assert calls[0] == calls[1]
    execute('UPDATE subscriptions SET expires=1')
    asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions')['state'] == 'expired'
    assert calls[-1] == ('revoke',calls[0][1])


def test_unknown_wireguard_mode_is_rejected():
    with pytest.raises(ValueError):servers.validated('wg',{'url':'https://wg.example','token':'t'*40,'api':'typo'})


def test_wgdashboard_profile_is_explicit_and_public_response_has_no_credentials(client):
    profile = servers.validated('wg',{'url':'https://wg.example','token':'t'*40,'api':'wgdashboard','configuration':'wg0'})
    execute('UPDATE servers SET settings=? WHERE id=?',(c.encrypt(json.dumps(profile)),'test-wg'))
    public = servers.public(one('SELECT * FROM servers WHERE id=?',('test-wg',)))
    assert public['profile'] == 'wgdashboard'
    assert 't'*40 not in json.dumps(public) and 'wg.example' not in json.dumps(public)


def test_interface_cannot_change_after_subscription_is_bound(client):
    register(client)
    profile = {'api':'wgdashboard','url':'https://wg.example','token':'t'*40,'configuration':'wg0'}
    execute('UPDATE servers SET settings=? WHERE id=?',(c.encrypt(json.dumps(profile)),'test-wg'))
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id) VALUES ('bound',1,'wg',0,'pending','test-wg')")
    data = dict(version=1,settings=profile | {'configuration':'wg1'},state='disabled',name='WG',location='',capacity=0)
    # Проверяем настоящий HTTP-контракт, включая значения новых полей по умолчанию.
    response = client.put('/api/admin/servers/test-wg', json=data,
                          headers={'Authorization':'Bearer '+c.ADMIN_TOKEN})
    assert response.status_code == 409
    assert servers.decode(one('SELECT * FROM servers WHERE id=?', ('test-wg',))) == profile


def test_public_http_wgdashboard_rejected_with_actionable_message(client):
    from app import config as c
    payload = {'name':'New WGDashboard','location':'','protocol':'wg',
        'capacity':0,'settings':{'api':'wgdashboard',
          'url':'http://193.238.155.39:10086',
          'token':'t'*40,'configuration':'wg0'}}
    resp = client.post('/api/admin/servers',json=payload,
        headers={'Authorization':'Bearer '+c.ADMIN_TOKEN})
    assert resp.status_code==422,resp.text
    assert 'HTTPS' in resp.json()['detail']
    assert 't'*40 not in resp.text


def test_new_server_settings_are_encrypted_and_persist_after_reinit(client):
    from app.db import init_db
    profile = {
        'api': 'wgdashboard',
        'url': 'https://wg.example/private-panel',
        'token': 't'*40,
        'configuration': 'wg0',
    }
    created = client.post('/api/admin/servers', headers={
        'Authorization': 'Bearer ' + c.ADMIN_TOKEN,
    }, json={
        'name': 'New WG node', 'location': 'FI', 'protocol': 'wg',
        'capacity': 0, 'settings': profile,
    })
    assert created.status_code == 201, created.text
    item = created.json()
    assert item['profile'] == 'wgdashboard'
    assert item['state'] == 'disabled'
    assert 'wg.example' not in created.text
    assert profile['token'] not in created.text
    row = one('SELECT * FROM servers WHERE id=?', (item['id'],))
    assert row is not None
    assert profile['token'] not in row['settings']
    assert profile['url'] not in row['settings']
    assert servers.decode(row) == {
        **profile, 'panel_timezone': None, 'schedule_expiry': False,
    }
    init_db()
    fresh = one('SELECT * FROM servers WHERE id=?', (item['id'],))
    assert fresh is not None
    assert servers.decode(fresh) == servers.decode(row)
    listing = client.get('/api/admin/servers', headers={
        'Authorization': 'Bearer ' + c.ADMIN_TOKEN,
    })
    assert listing.status_code == 200
    assert any(server['id'] == item['id'] for server in listing.json()['servers'])
    assert profile['token'] not in listing.text
    assert profile['url'] not in listing.text
