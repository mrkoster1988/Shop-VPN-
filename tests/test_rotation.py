import asyncio,time,uuid
import pytest
from test_devices import issued, add
from test_shop import client, register
from app import providers,service,config as c
from app.db import one,execute,init_db


def rotate(client,did,rid=None):
    return client.post('/api/devices/'+did+'/rotate',json={'request_id':rid or str(uuid.uuid4())})


def ready(issued,protocol='xui'):
    client,issue,calls=issued;sub=issue(protocol);device=add(client,sub['id']).json();asyncio.run(service.process_devices())
    calls.clear();return client,sub,device,calls,issue

@pytest.mark.parametrize('protocol',['xui','wg'])
def test_rotation_retires_old_key_then_issues_new_key_without_changing_device_slot(issued,protocol):
    client,sub,device,calls,issue=ready(issued,protocol);old=client.get('/api/devices/'+device['id']+'/config').json()['config']
    rid=str(uuid.uuid4());response=rotate(client,device['id'],rid)
    assert response.status_code==201 and response.json()['device']['state']=='rotating'
    assert client.get('/api/devices/'+device['id']+'/config').status_code==404
    assert rotate(client,device['id'],rid).status_code==200
    assert rotate(client,device['id']).status_code==409
    asyncio.run(service.process_devices())
    row=one('SELECT * FROM devices');job=one('SELECT * FROM device_rotations')
    assert row['id']==device['id'] and c.decrypt(row['credential_id'])!=device['id'] and job['state']=='completed'
    assert [x['id'] for x in calls]==[device['id'],c.decrypt(row['credential_id'])]
    assert calls[0]['disabled'] and not calls[1].get('disabled')
    assert {x['server_id'] for x in calls}=={sub['server_id']}
    assert client.get('/api/devices/'+device['id']+'/config').json()['config']!=old
    assert row['secret']!=client.get('/api/devices/'+device['id']+'/config').json()['config']
    assert c.decrypt(row['credential_id']) not in str(job) and c.decrypt(row['credential_id']) not in str(row)
    for suffix in ('qr','download'):assert client.get('/api/devices/'+device['id']+'/'+suffix).status_code==200
    assert len(client.get('/api/subscriptions/'+sub['id']+'/devices').json()['devices'])==1
    assert rotate(client,device['id'],rid).json()['rotation']['state']=='completed'
    assert one('SELECT count(*) AS n FROM device_rotations')['n']==1
    calls.clear();issue(protocol)
    assert {x['id'] for x in calls}=={sub['id'],c.decrypt(row['credential_id'])}
    client.delete('/api/devices/'+device['id']);asyncio.run(service.process_devices())
    assert calls[-1]['id']==c.decrypt(row['credential_id']) and calls[-1]['disabled']


def test_failed_old_key_revocation_never_starts_new_provision(issued,monkeypatch):
    client,sub,device,calls,_=ready(issued)
    async def fail(remote):calls.append(remote);raise TimeoutError('secret-upstream')
    monkeypatch.setattr(providers,'disable',fail);rotate(client,device['id']);asyncio.run(service.process_devices())
    job=one('SELECT * FROM device_rotations');assert job['state']=='revoking' and job['attempts']==1 and job['error']=='TimeoutError'
    assert len(calls)==1 and calls[0]['id']==device['id']
    assert 'secret-upstream' not in client.get('/api/subscriptions/'+sub['id']+'/devices').text
    assert client.get('/api/devices/'+device['id']+'/download').status_code==404


def test_lost_new_key_response_reuses_persisted_identity_after_restart(issued,monkeypatch):
    client,sub,device,calls,_=ready(issued);identities=[]
    async def lost(remote):identities.append(remote['id']);raise TimeoutError('private')
    monkeypatch.setattr(providers,'provision',lost);rotate(client,device['id']);asyncio.run(service.process_devices())
    job=one('SELECT * FROM device_rotations');assert job['state']=='issuing'
    assert calls[0]['id']==device['id'] and calls[0]['disabled']
    init_db();execute('UPDATE device_rotations SET next_attempt=0')
    async def ok(remote):identities.append(remote['id']);return 'new-private-key'
    monkeypatch.setattr(providers,'provision',ok);asyncio.run(service.process_devices())
    assert identities==[c.decrypt(job['new_credential_id'])]*2
    assert len(calls)==1  # The acknowledged old-key retirement is not repeated.
    assert one('SELECT credential_id FROM devices')['credential_id']==job['new_credential_id']


@pytest.mark.parametrize('expired',[False,True])
def test_cancel_or_expiry_after_ambiguous_issue_revokes_both_possible_keys(issued,monkeypatch,expired):
    client,sub,device,calls,_=ready(issued)
    async def lost(remote):raise TimeoutError('ambiguous')
    monkeypatch.setattr(providers,'provision',lost);rotate(client,device['id']);asyncio.run(service.process_devices())
    job=one('SELECT * FROM device_rotations');calls.clear()
    if expired:
        execute('UPDATE subscriptions SET expires=1');execute('UPDATE device_rotations SET next_attempt=0')
    else:assert client.delete('/api/devices/'+device['id']).status_code==200
    asyncio.run(service.process_devices())
    assert {x['id'] for x in calls}=={c.decrypt(job['old_credential_id']),c.decrypt(job['new_credential_id'])}
    assert all(x['disabled'] for x in calls)
    assert one('SELECT state,secret FROM devices')=={'state':'revoked','secret':None}
    assert one('SELECT state FROM device_rotations')['state']=='canceled'


def test_cancel_cleanup_failure_keeps_slot_occupied_until_both_keys_retired(issued,monkeypatch):
    client,sub,device,calls,_=ready(issued);rotate(client,device['id']);client.delete('/api/devices/'+device['id'])
    async def failed(remote):raise ConnectionError('private')
    monkeypatch.setattr(providers,'disable',failed);asyncio.run(service.process_devices())
    assert one('SELECT state FROM devices')['state']=='revoking'
    assert one('SELECT state FROM device_rotations')['state']=='canceling'
    assert len(client.get('/api/subscriptions/'+sub['id']+'/devices').json()['devices'])==1


def test_rotation_preserves_other_profiles_primary_and_quota(issued):
    client,sub,device,calls,_=ready(issued);other=add(client,sub['id'],'Laptop').json();asyncio.run(service.process_devices())
    original=client.get('/api/subscriptions/'+sub['id']+'/config').json();second=client.get('/api/devices/'+other['id']+'/config').json()
    execute('UPDATE subscriptions SET device_limit=0');rotate(client,device['id']);asyncio.run(service.process_devices())
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()==original
    assert client.get('/api/devices/'+other['id']+'/config').json()==second
    assert one('SELECT device_limit FROM subscriptions')['device_limit']==0
    old=one('SELECT credential_id FROM devices WHERE id=?',(device['id'],))['credential_id']
    rotate(client,device['id']);asyncio.run(service.process_devices())
    assert one('SELECT old_credential_id FROM device_rotations ORDER BY rowid DESC')['old_credential_id']==old


def test_expiry_after_completed_rotation_targets_latest_key(issued):
    client,sub,device,calls,_=ready(issued);rotate(client,device['id']);asyncio.run(service.process_devices())
    current=one('SELECT credential_id FROM devices')['credential_id'];calls.clear()
    execute('UPDATE subscriptions SET expires=?',(int(time.time())-1,));asyncio.run(service.tick())
    assert {x['id'] for x in calls if x.get('disabled')}=={sub['id'],c.decrypt(current)}


def test_rotation_ownership_origin_and_invalid_state(issued):
    client,sub,device,_,_=ready(issued)
    assert client.post('/api/devices/'+device['id']+'/rotate',json={'request_id':'bad'}).status_code==422
    assert client.post('/api/devices/'+device['id']+'/rotate',json={'request_id':str(uuid.uuid4())},headers={'Origin':'https://evil.example'}).status_code==403
    pending=add(client,sub['id'],'Pending').json();assert rotate(client,pending['id']).status_code==409
    execute('UPDATE subscriptions SET expires=1');assert rotate(client,device['id']).status_code==409
    client.post('/api/auth/logout');register(client,'other@example.com')
    assert rotate(client,device['id']).status_code==404
    assert rotate(client,sub['id']).status_code==404

def test_renewal_during_rotation_uses_new_term_without_reenabling_old_key(issued,monkeypatch):
    client,sub,device,calls,issue=ready(issued);rotate(client,device['id'])
    async def fail_new(remote):
        if remote['id']!=sub['id']:raise TimeoutError()
        calls.append(dict(remote));return 'primary'
    monkeypatch.setattr(providers,'provision',fail_new);asyncio.run(service.process_devices())
    job=one('SELECT * FROM device_rotations');issue();expiry=one('SELECT expires FROM subscriptions')['expires']
    assert not any(x['id']==device['id'] and not x.get('disabled') for x in calls)
    async def good(remote):
        assert one('SELECT state FROM device_rotations')['state']=='issuing'
        assert remote['expires']==expiry
        calls.append(dict(remote));return 'new'
    monkeypatch.setattr(providers,'provision',good);execute('UPDATE device_rotations SET next_attempt=0');asyncio.run(service.process_devices())
    assert calls[-1]['id']==c.decrypt(job['new_credential_id'])
    assert one('SELECT expires FROM devices')['expires']==expiry


def test_rotation_route_under_shop_prefix(issued,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app import config as c
    client,sub,device,_,_=ready(issued)
    monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'COOKIE_PATH','/shop/');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        shop.cookies.update(client.cookies)
        result=shop.post('/shop/api/devices/'+device['id']+'/rotate',json={'request_id':str(uuid.uuid4())},headers={'Origin':'http://testserver'})
        assert result.status_code==201
        asyncio.run(service.process_devices())
        assert shop.get('/shop/api/devices/'+device['id']+'/config').status_code==200


def test_old_device_migration_preserves_original_key(issued):
    from app.db import db
    client,sub,device,_,_=ready(issued);original=one('SELECT secret FROM devices')['secret']
    with db() as conn:conn.execute('ALTER TABLE devices DROP COLUMN credential_id')
    init_db();init_db()
    row=one('SELECT * FROM devices')
    assert c.decrypt(row['credential_id'])==device['id'] and row['secret']==original
    assert client.get('/api/devices/'+device['id']+'/config').status_code==200


def test_wrong_encryption_key_does_not_poison_device_migration(issued,monkeypatch):
    import os
    from cryptography.fernet import Fernet,InvalidToken
    from app.db import db
    client,sub,device,_,_=ready(issued);key=os.environ['ENCRYPTION_KEY']
    with db() as conn:conn.execute('ALTER TABLE devices DROP COLUMN credential_id')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):init_db()
    monkeypatch.setenv('ENCRYPTION_KEY',key);init_db()
    row=one('SELECT * FROM devices');assert c.decrypt(row['credential_id'])==device['id']
    assert client.get('/api/devices/'+device['id']+'/config').status_code==200
