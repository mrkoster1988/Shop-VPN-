import asyncio,json
import pytest
from test_devices import issued, add
from test_shop import client, admin_headers, register
from app import service, providers
from app.db import one,execute,db,init_db


def edit(client,limit):
    # Select by ID rather than relying on catalogue ordering.
    plan=next(p for p in client.get('/api/admin/plans',headers=admin_headers()).json()['plans'] if p['id']=='month')
    body={k:plan[k] for k in ('name','days','price','stars','enabled','version')}
    return client.put('/api/admin/plans/month',headers=admin_headers(),json={**body,'device_limit':limit})


def buy(client):
    plan=next(p for p in client.get('/api/catalog').json()['plans'] if p['id']=='month')
    result=client.post('/api/orders',json={'plan':'month','protocol':'xui','plan_version':plan['version']})
    assert result.status_code==200,result.text
    oid=result.json()['id'];client.post('/api/admin/orders/'+oid+'/confirm',headers=admin_headers());asyncio.run(service.fulfill(oid))
    return oid

@pytest.mark.parametrize('value',[-1,21,True,1.5,'2'])
def test_limit_validation_is_strict_and_does_not_change_plan(client,value):
    assert edit(client,value).status_code==422
    assert one("SELECT device_limit,version FROM plans WHERE id='month'")=={'device_limit':4,'version':1}


def test_limit_snapshot_and_version_conflict(client,monkeypatch):
    async def provision(_):return 'secret'
    monkeypatch.setattr(providers,'provision',provision);register(client)
    original=client.post('/api/orders',json={'plan':'month','protocol':'xui'}).json()
    assert original['device_limit']==4
    changed=edit(client,0);assert changed.status_code==200 and changed.json()['version']==2
    assert client.post('/api/orders',json={'plan':'month','protocol':'xui','plan_version':1}).status_code==409
    client.post('/api/admin/orders/'+original['id']+'/confirm',headers=admin_headers());asyncio.run(service.fulfill(original['id']))
    sub=one('SELECT * FROM subscriptions');assert sub['device_limit']==4
    assert client.get('/api/orders/'+original['id']).json()['device_limit']==4
    buy(client)
    assert one('SELECT device_limit FROM subscriptions')['device_limit']==0
    assert add(client,sub['id']).status_code==409
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200


def test_downgrade_preserves_existing_keys_and_upgrade_enables_new_profiles(issued):
    client,issue,calls=issued;sub=issue()
    a=add(client,sub['id']).json();b=add(client,sub['id'],'Laptop').json();asyncio.run(service.process_devices())
    secrets={d:client.get('/api/devices/'+d+'/config').json()['config'] for d in (a['id'],b['id'])}
    assert edit(client,1).status_code==200;buy(client)
    listing=client.get('/api/subscriptions/'+sub['id']+'/devices').json();assert listing['max_extra']==1 and len(listing['devices'])==2
    assert all(d['ready'] for d in listing['devices']) and add(client,sub['id']).status_code==409
    assert all(client.get('/api/devices/'+did+'/config').json()['config']==value for did,value in secrets.items())
    assert not any(call.get('disabled') for call in calls)
    edit(client,3);buy(client);assert add(client,sub['id'],'Third').status_code==201
    assert add(client,sub['id'],'Fourth').status_code==409


def test_plan_edit_and_failed_fulfillment_do_not_change_entitlement(issued,monkeypatch):
    client,issue,_=issued;sub=issue();d=add(client,sub['id']).json();asyncio.run(service.process_devices())
    edit(client,0)
    assert one('SELECT device_limit FROM subscriptions')['device_limit']==4
    async def failing(remote):
        if remote['id']==d['id']:raise TimeoutError()
        return 'primary'
    monkeypatch.setattr(providers,'provision',failing);oid=buy(client)
    assert one('SELECT status,device_limit FROM orders WHERE id=?',(oid,))=={'status':'paid','device_limit':0}
    assert one('SELECT device_limit FROM subscriptions')['device_limit']==4
    edit(client,20);execute('UPDATE orders SET next_attempt=0')
    async def ok(_):return 'private'
    monkeypatch.setattr(providers,'provision',ok);asyncio.run(service.fulfill(oid))
    assert one('SELECT device_limit FROM subscriptions')['device_limit']==0


def test_old_admin_requests_preserve_limit_and_audit_includes_change(client):
    edit(client,7)
    row=one("SELECT * FROM plans WHERE id='month'")
    body={k:row[k] for k in ('name','days','price','stars','enabled','version')};body['enabled']=bool(body['enabled'])
    result=client.put('/api/admin/plans/month',headers=admin_headers(),json=body)
    assert result.status_code==200 and result.json()['device_limit']==7 and result.json()['version']==2
    after=json.loads(one('SELECT after_data FROM plan_changes ORDER BY id DESC')['after_data'])
    assert after['device_limit']==7
    created=client.post('/api/admin/plans',headers=admin_headers(),json={'id':'single','name':'Один','days':1,'price':10,'stars':1,'device_limit':0})
    assert created.status_code==201 and created.json()['device_limit']==0


def test_old_database_defaults_keep_four_profiles(issued):
    client,issue,_=issued;sub=issue();before=one('SELECT secret FROM subscriptions')['secret']
    with db() as conn:
        for table in ('plans','orders','subscriptions'):conn.execute('ALTER TABLE '+table+' DROP COLUMN device_limit')
    init_db();init_db()
    for table in ('plans','orders','subscriptions'):assert one('SELECT device_limit FROM '+table)['device_limit']==4
    assert one('SELECT secret FROM subscriptions')['secret']==before
    assert client.get('/api/me').json()['subscriptions'][0]['device_limit']==4

def test_already_accepted_device_creation_survives_lower_limit(issued):
    client,issue,_=issued;sub=issue();device=add(client,sub['id']).json()
    assert device['state']=='pending'
    edit(client,0);buy(client);asyncio.run(service.process_devices())
    assert client.get('/api/devices/'+device['id']+'/config').status_code==200
    assert add(client,sub['id']).status_code==409
