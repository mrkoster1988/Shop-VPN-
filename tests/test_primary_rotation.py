import asyncio,time,uuid
import pytest
from test_devices import issued,add
from test_shop import client,register,order,confirm
from app import providers,service,config as c
from app.db import one,execute,init_db


def rotate(client,sid,rid=None):
    return client.post('/api/subscriptions/'+sid+'/rotate',json={'request_id':rid or str(uuid.uuid4())})


def prepared(issued,protocol='xui'):
    client,issue,calls=issued;sub=issue(protocol);device=add(client,sub['id']).json();asyncio.run(service.process_devices());calls.clear()
    return client,issue,calls,sub,device

@pytest.mark.parametrize('protocol',['xui','wg'])
def test_primary_rotation_keeps_subscription_and_extra_devices(issued,protocol):
    client,issue,calls,sub,device=prepared(issued,protocol)
    extra=client.get('/api/devices/'+device['id']+'/config').json();rid=str(uuid.uuid4())
    assert rotate(client,sub['id'],rid).status_code==201
    assert rotate(client,sub['id'],rid).status_code==200 and rotate(client,sub['id']).status_code==409
    for suffix in ('config','qr','download'):assert client.get('/api/subscriptions/'+sub['id']+'/'+suffix).status_code==404
    assert client.get('/api/devices/'+device['id']+'/config').json()==extra
    asyncio.run(service.process_devices());row=one('SELECT * FROM subscriptions');job=one('SELECT * FROM primary_rotations')
    identity=c.decrypt(row['credential_id'])
    assert [x['id'] for x in calls]==[sub['id'],identity] and calls[0]['disabled']
    assert identity!=sub['id'] and identity not in str(row) and identity not in str(job)
    assert row['id']==sub['id'] and row['expires']==sub['expires'] and row['device_limit']==sub['device_limit']
    assert job['state']=='completed' and row['primary_key_state']=='active'
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()['config']=='private-config-'+identity
    assert client.get('/api/devices/'+device['id']+'/config').json()==extra
    assert rotate(client,sub['id'],rid).json()['rotation']['state']=='completed'
    calls.clear();issue(protocol)
    assert {x['id'] for x in calls}=={identity,device['id']}
    execute('UPDATE subscriptions SET expires=1');calls.clear();asyncio.run(service.tick())
    assert {x['id'] for x in calls if x.get('disabled')}=={identity,device['id']}


def test_old_primary_revocation_failure_does_not_issue_new_key(issued,monkeypatch):
    client,_,calls,sub,_=prepared(issued)
    async def fail(remote):calls.append(dict(remote));raise TimeoutError('private')
    monkeypatch.setattr(providers,'disable',fail);rotate(client,sub['id']);asyncio.run(service.process_devices())
    job=one('SELECT * FROM primary_rotations');assert job['state']=='revoking' and job['attempts']==1
    assert len(calls)==1 and calls[0]['id']==sub['id']
    me=client.get('/api/me');assert me.json()['subscriptions'][0]['primary_retrying'] and 'private' not in me.text


def test_lost_issue_response_uses_saved_identity_after_restart(issued,monkeypatch):
    client,_,calls,sub,_=prepared(issued);attempts=[]
    async def lost(remote):attempts.append(remote['id']);raise TimeoutError()
    monkeypatch.setattr(providers,'provision',lost);rotate(client,sub['id']);asyncio.run(service.process_devices())
    job=one('SELECT * FROM primary_rotations');assert job['state']=='issuing';init_db();execute('UPDATE primary_rotations SET next_attempt=0')
    async def good(remote):attempts.append(remote['id']);return 'new'
    monkeypatch.setattr(providers,'provision',good);asyncio.run(service.process_devices())
    assert attempts==[c.decrypt(job['new_credential_id'])]*2 and len(calls)==1


def test_paid_renewal_waits_without_freezing_target_and_resumes_on_new_key(issued):
    client,_,calls,sub,device=prepared(issued);rotate(client,sub['id'])
    purchase=order(client);confirm(client,purchase['id']);asyncio.run(service.fulfill(purchase['id']))
    row=one('SELECT * FROM orders WHERE id=?',(purchase['id'],));assert row['status']=='paid' and row['target_expires'] is None
    assert calls==[]
    asyncio.run(service.process_devices());identity=service.primary_identity(one('SELECT * FROM subscriptions'))
    assert one('SELECT next_attempt FROM orders WHERE id=?',(purchase['id'],))['next_attempt']==0
    calls.clear();asyncio.run(service.fulfill(purchase['id']))
    assert {x['id'] for x in calls}=={identity,device['id']} and sub['id'] not in {x['id'] for x in calls}
    assert one('SELECT expires FROM subscriptions')['expires']==sub['expires']+30*86400


def test_expired_incomplete_rotation_retires_both_and_renewal_never_revives_old(issued,monkeypatch):
    client,issue,calls,sub,_=prepared(issued)
    async def lost(_):raise TimeoutError()
    monkeypatch.setattr(providers,'provision',lost);rotate(client,sub['id']);asyncio.run(service.process_devices());job=one('SELECT * FROM primary_rotations');calls.clear()
    execute('UPDATE subscriptions SET expires=1');execute('UPDATE primary_rotations SET next_attempt=0');asyncio.run(service.tick())
    assert {c.decrypt(job['old_credential_id']),c.decrypt(job['new_credential_id'])}<={x['id'] for x in calls if x.get('disabled')}
    assert one('SELECT state FROM primary_rotations')['state']=='canceled'
    assert one('SELECT primary_key_state FROM subscriptions')['primary_key_state']=='revoked'
    calls.clear()
    async def good(remote):calls.append(dict(remote));return 'renewed'
    monkeypatch.setattr(providers,'provision',good);issue()
    assert sub['id'] not in {x['id'] for x in calls} and c.decrypt(job['new_credential_id']) in {x['id'] for x in calls}
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200


def test_second_rotation_uses_current_primary_key(issued):
    client,_,calls,sub,_=prepared(issued);rotate(client,sub['id']);asyncio.run(service.process_devices())
    current=service.primary_identity(one('SELECT * FROM subscriptions'));calls.clear()
    rotate(client,sub['id']);asyncio.run(service.process_devices())
    assert calls[0]['id']==current and calls[0]['disabled'] and calls[1]['id']!=current


def test_primary_authorization_validation_and_expiry(issued):
    client,_,_,sub,_=prepared(issued)
    assert client.post('/api/subscriptions/'+sub['id']+'/rotate',json={'request_id':'bad'}).status_code==422
    assert client.post('/api/subscriptions/'+sub['id']+'/rotate',json={'request_id':str(uuid.uuid4())},headers={'Origin':'https://evil.example'}).status_code==403
    execute('UPDATE subscriptions SET expires=1');assert rotate(client,sub['id']).status_code==409
    client.post('/api/auth/logout');register(client,'other@example.com');assert rotate(client,sub['id']).status_code==404


def test_primary_rotation_under_shop_prefix(issued,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    client,_,_,sub,_=prepared(issued)
    monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'COOKIE_PATH','/shop/');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        shop.cookies.update(client.cookies)
        result=shop.post('/shop/api/subscriptions/'+sub['id']+'/rotate',json={'request_id':str(uuid.uuid4())},headers={'Origin':'http://testserver'})
        assert result.status_code==201;asyncio.run(service.process_devices())
        assert shop.get('/shop/api/subscriptions/'+sub['id']+'/config').status_code==200


def test_legacy_primary_migration_keeps_config_and_identity(issued):
    from app.db import db
    client,_,_,sub,_=prepared(issued);original=client.get('/api/subscriptions/'+sub['id']+'/config').json()
    with db() as conn:
        for name in ('credential_id','primary_key_state','rotation_error'):conn.execute('ALTER TABLE subscriptions DROP COLUMN '+name)
    init_db();init_db();row=one('SELECT * FROM subscriptions')
    assert row['primary_key_state']=='active' and service.primary_identity(row)==sub['id']
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()==original

def test_failed_expiry_cleanup_blocks_renewal_until_both_keys_retired(issued,monkeypatch):
    client,_,calls,sub,_=prepared(issued);rotate(client,sub['id'])
    async def lost(_):raise TimeoutError()
    monkeypatch.setattr(providers,'provision',lost);asyncio.run(service.process_devices());job=one('SELECT * FROM primary_rotations')
    execute('UPDATE subscriptions SET expires=1');execute('UPDATE primary_rotations SET next_attempt=0')
    async def partial(remote):
        if remote['id']==c.decrypt(job['new_credential_id']):raise ConnectionError()
    monkeypatch.setattr(providers,'disable',partial);asyncio.run(service.process_devices())
    assert one('SELECT state FROM primary_rotations')['state']=='canceling'
    purchase=order(client);confirm(client,purchase['id']);calls.clear();asyncio.run(service.fulfill(purchase['id']))
    assert one('SELECT status FROM orders WHERE id=?',(purchase['id'],))['status']=='paid' and calls==[]
