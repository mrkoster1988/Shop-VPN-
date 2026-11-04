import asyncio, time, uuid
import pytest
from test_shop import client, register, order, confirm
from app import config as c, service, providers
from app.db import one, execute, init_db

@pytest.fixture
def issued(client,monkeypatch):
    calls=[]
    async def provision(sub):
        calls.append(dict(sub)); return 'private-config-'+sub['id']
    async def disable(sub): calls.append({**sub,'disabled':True})
    monkeypatch.setattr(providers,'provision',provision)
    monkeypatch.setattr(providers,'disable',disable)
    register(client)
    def issue(protocol='xui'):
        purchase=order(client,protocol);confirm(client,purchase['id']);asyncio.run(service.fulfill(purchase['id']))
        return one('SELECT * FROM subscriptions WHERE protocol=?',(protocol,))
    return client,issue,calls

def add(client,sid,name='Телефон',request_id=None):
    return client.post('/api/subscriptions/'+sid+'/devices',json={'name':name,'request_id':request_id or str(uuid.uuid4())})

@pytest.mark.parametrize('protocol',['xui','wg'])
def test_separate_device_profiles_and_primary_survive(issued,protocol):
    client,issue,calls=issued;sub=issue(protocol);primary=sub['secret']
    a=add(client,sub['id']).json();b=add(client,sub['id'],'Ноутбук').json()
    assert a['id']!=b['id']!=sub['id'] and a['state']=='pending'
    assert client.get('/api/devices/'+a['id']+'/config').status_code==404
    asyncio.run(service.process_devices())
    for device in (a,b):
        result=client.get('/api/devices/'+device['id']+'/config')
        assert result.json()['config']=='private-config-'+device['id']
        assert client.get('/api/devices/'+device['id']+'/qr').headers['content-type']=='image/png'
        download=client.get('/api/devices/'+device['id']+'/download')
        assert ('conf' if protocol=='wg' else 'txt') in download.headers['content-disposition']
        assert one('SELECT secret FROM devices WHERE id=?',(device['id'],))['secret']!=result.json()['config']
    assert one('SELECT secret FROM subscriptions')['secret']==primary
    listed=client.get('/api/subscriptions/'+sub['id']+'/devices')
    assert 'secret' not in listed.text and 'server_id' not in listed.text and 'private-config' not in listed.text
    assert {call['server_id'] for call in calls}=={sub['server_id']}

def test_create_is_idempotent_and_limits_include_pending_revocations(issued):
    client,issue,_=issued;sub=issue();rid=str(uuid.uuid4())
    first=add(client,sub['id'],request_id=rid)
    assert first.status_code==201
    assert add(client,sub['id'],request_id=rid).json()['id']==first.json()['id']
    assert add(client,sub['id'],'Other',rid).status_code==409
    for i in range(3): assert add(client,sub['id'],str(i)).status_code==201
    assert add(client,sub['id']).status_code==409
    client.delete('/api/devices/'+first.json()['id'])
    assert add(client,sub['id']).status_code==409
    asyncio.run(service.process_devices())
    assert add(client,sub['id']).status_code==201
    # Replaying a revoked request never reactivates it.
    assert add(client,sub['id'],request_id=rid).json()['state']=='revoked'

@pytest.mark.parametrize('protocol',['xui','wg'])
def test_renewal_extends_same_device_ids_and_revocation_is_independent(issued,protocol):
    client,issue,calls=issued;sub=issue(protocol);a=add(client,sub['id']).json();b=add(client,sub['id'],'Other').json()
    asyncio.run(service.process_devices());expires=one('SELECT expires FROM devices WHERE id=?',(a['id'],))['expires']
    issue(protocol)
    assert one('SELECT expires FROM devices WHERE id=?',(a['id'],))['expires']==expires+30*86400
    assert client.delete('/api/devices/'+a['id']).status_code==200
    assert client.delete('/api/devices/'+a['id']).status_code==200
    for suffix in ('config','qr','download'):assert client.get('/api/devices/'+a['id']+'/'+suffix).status_code==404
    asyncio.run(service.process_devices())
    assert one('SELECT state,secret FROM devices WHERE id=?',(a['id'],))=={'state':'revoked','secret':None}
    assert calls[-1]['id']==a['id'] and calls[-1]['disabled']
    assert client.get('/api/devices/'+b['id']+'/config').status_code==200
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200

def test_device_retry_persists_identity_and_sanitizes_errors(issued,monkeypatch):
    client,issue,calls=issued;sub=issue();d=add(client,sub['id']).json();attempts=[]
    async def fail(remote): attempts.append(dict(remote));raise TimeoutError('private-credential')
    monkeypatch.setattr(providers,'provision',fail);asyncio.run(service.process_devices())
    row=one('SELECT * FROM devices');assert row['state']=='pending' and row['attempts']==1 and row['error']=='TimeoutError'
    assert 'private-credential' not in client.get('/api/subscriptions/'+sub['id']+'/devices').text
    init_db();execute('UPDATE devices SET next_attempt=0')
    async def good(remote): attempts.append(dict(remote));return 'device-secret'
    monkeypatch.setattr(providers,'provision',good);asyncio.run(service.process_devices())
    assert len(attempts)==2 and attempts[0]['id']==attempts[1]['id']==d['id']
    assert attempts[0]['server_id']==attempts[1]['server_id']==sub['server_id']

def test_failed_renewal_does_not_mark_order_ready_and_retries_same_target(issued,monkeypatch):
    client,issue,calls=issued;sub=issue();d=add(client,sub['id']).json();asyncio.run(service.process_devices());old=sub['expires']
    async def fail(remote):
        if remote['id']==d['id']:raise TimeoutError('secret')
        return 'primary'
    monkeypatch.setattr(providers,'provision',fail)
    purchase=order(client);confirm(client,purchase['id']);asyncio.run(service.fulfill(purchase['id']))
    assert one('SELECT status FROM orders WHERE id=?',(purchase['id'],))['status']=='paid'
    assert one('SELECT expires FROM subscriptions')['expires']==old
    targets=[]
    async def good(remote):targets.append(dict(remote));return 'secret'
    monkeypatch.setattr(providers,'provision',good);execute('UPDATE orders SET next_attempt=0')
    asyncio.run(service.fulfill(purchase['id']))
    assert {x['expires'] for x in targets}=={old+30*86400}
    assert {x['id'] for x in targets}=={sub['id'],d['id']}

def test_expiry_revokes_primary_and_additional_profiles(issued):
    client,issue,calls=issued;sub=issue();d=add(client,sub['id']).json();asyncio.run(service.process_devices())
    execute('UPDATE subscriptions SET expires=?',(int(time.time())-1,));asyncio.run(service.tick())
    disabled={x['id'] for x in calls if x.get('disabled')}
    assert disabled=={sub['id'],d['id']}
    assert client.get('/api/devices/'+d['id']+'/config').status_code==404
    issue();assert client.get('/api/devices/'+d['id']+'/config').status_code==200

def test_ownership_expiry_origin_and_validation(issued):
    client,issue,_=issued;sub=issue();d=add(client,sub['id']).json();asyncio.run(service.process_devices())
    for body in ({'name':' ','request_id':str(uuid.uuid4())},{'name':'ok','request_id':'bad'},{'name':'x\n','request_id':str(uuid.uuid4()),'extra':True}):
        assert client.post('/api/subscriptions/'+sub['id']+'/devices',json=body).status_code==422
    assert client.delete('/api/devices/'+d['id'],headers={'Origin':'https://evil.example'}).status_code==403
    execute('UPDATE subscriptions SET expires=1');assert add(client,sub['id']).status_code==409
    client.post('/api/auth/logout');register(client,'other@example.com')
    assert client.get('/api/subscriptions/'+sub['id']+'/devices').status_code==404
    assert add(client,sub['id']).status_code==404
    assert client.delete('/api/devices/'+d['id']).status_code==404
    for suffix in ('config','download','qr'):assert client.get('/api/devices/'+d['id']+'/'+suffix).status_code==404

def test_revoke_retries_and_expired_pending_device_never_creates_access(issued,monkeypatch):
    client,issue,calls=issued;sub=issue();d=add(client,sub['id']).json();execute('UPDATE subscriptions SET expires=1')
    async def fail(remote):raise TimeoutError('private')
    monkeypatch.setattr(providers,'disable',fail);asyncio.run(service.process_devices())
    row=one('SELECT * FROM devices');assert row['state']=='revoking' and row['attempts']==1
    assert len(calls)==1  # Only the original subscription was provisioned.
    async def disable(remote):calls.append({**remote,'disabled':True})
    monkeypatch.setattr(providers,'disable',disable);execute('UPDATE devices SET next_attempt=0');asyncio.run(service.process_devices())
    assert one('SELECT state FROM devices')['state']=='revoked' and calls[-1]['id']==d['id']

def test_devices_under_shop_prefix(issued,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    client,issue,_=issued;sub=issue()
    monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'COOKIE_PATH','/shop/');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        shop.cookies.update(client.cookies)
        body={'name':'Laptop','request_id':str(uuid.uuid4())}
        response=shop.post('/shop/api/subscriptions/'+sub['id']+'/devices',json=body,headers={'Origin':'http://testserver'})
        assert response.status_code==201
        did=response.json()['id'];asyncio.run(service.process_devices())
        assert shop.get('/shop/api/devices/'+did+'/config').status_code==200
        assert shop.get('/shop/api/subscriptions/'+sub['id']+'/devices').json()['devices'][0]['ready']
        assert shop.delete('/shop/api/devices/'+did,headers={'Origin':'http://testserver'}).status_code==200
