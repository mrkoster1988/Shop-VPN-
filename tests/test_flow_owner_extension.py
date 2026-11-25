"""Owner extension of a verified *account* subscription, never a phone lease."""
import json
import time
from uuid import uuid4

import pytest
from app import config as c, providers, wgdashboard
from app.db import db, one, execute
from test_shop import client
from test_wg_peer_diagnostics import setup_user, profile, owner, SID
from test_flow_wireguard import _VALID


def path(uid=1):
    return f'/api/admin/users/{uid}/wireguard/extend'


def prepared(client):
    setup_user(client)
    execute("UPDATE servers SET settings=?,state='active',health='ok' WHERE id='test-wg'",
            (c.encrypt(json.dumps(profile(schedule_expiry=True))),))
    wgdashboard.stored_private_key('test-wg', SID)
    plan=client.get(path()+'/plan?days=30',headers=owner())
    assert plan.status_code==200,plan.text
    return plan.json()


def request(plan, **changes):
    return {
        'request_id':str(uuid4()),
        'subscription_id':plan['subscription_id'],
        'before_expires':plan['before_expires'],
        'target_expires':plan['target_expires'],
        'days':plan['days'],
        'reason':'Ручное продление владельцем',
        'confirm':'EXTEND_ACCOUNT_WIREGUARD',
        **changes,
    }


def test_plan_and_extension_preserve_peer_identity_and_exact_idempotence(client, monkeypatch):
    plan=prepared(client)
    assert plan['subscription_id']==SID
    assert plan['target_expires']==plan['before_expires']+30*86400
    assert client.get(path()+'/plan?days=30').status_code==401
    assert client.get(path()+'/plan?days=366',headers=owner()).status_code==422
    calls=[]
    async def provision(sub):
        calls.append((sub['id'],sub['expires']))
        return _VALID
    monkeypatch.setattr(providers,'provision',provision)
    data=request(plan)
    result=client.post(path(),headers=owner(),json=data)
    assert result.status_code==200,result.text
    assert result.json()=={'ok':True,'status':'extended','expires_at':plan['target_expires']}
    assert calls==[(SID,plan['target_expires'])]
    assert one('SELECT expires,state FROM subscriptions WHERE id=?',(SID,))=={
        'expires':plan['target_expires'],'state':'active'}
    assert one('SELECT status,before_expires,target_expires,reason FROM flow_owner_extensions')['status']=='applied'
    assert client.post(path(),headers=owner(),json=data).json()==result.json()
    assert calls==[(SID,plan['target_expires'])]
    conflict=client.post(path(),headers=owner(),json=request(plan))
    assert conflict.status_code==409
    assert one('SELECT count(*) AS n FROM flow_owner_extensions')['n']==1


def test_panel_error_retains_pending_exact_request_and_retries_safely(client, monkeypatch):
    plan=prepared(client)
    data=request(plan)
    calls=[]
    async def fail(sub):
        calls.append(sub['expires'])
        raise TimeoutError('private panel URL never reaches customer')
    monkeypatch.setattr(providers,'provision',fail)
    blocked=client.post(path(),headers=owner(),json=data)
    assert blocked.status_code==502
    assert 'private panel' not in blocked.text
    assert one('SELECT expires FROM subscriptions WHERE id=?',(SID,))['expires']==plan['before_expires']
    assert one('SELECT status FROM flow_owner_extensions')['status']=='pending'
    assert client.get(path()+'/plan?days=30',headers=owner()).status_code==409
    assert client.get(path()+'/pending').status_code==401
    recovered=client.get(path()+'/pending',headers=owner()).json()['request']
    assert recovered==data
    assert client.post(path(),headers=owner(),json=request(plan)).status_code==409
    async def succeed(sub):
        calls.append(sub['expires'])
        return _VALID
    monkeypatch.setattr(providers,'provision',succeed)
    retry=client.post(path(),headers=owner(),json=recovered)
    assert retry.status_code==200,retry.text
    assert calls==[plan['target_expires'],plan['target_expires']]
    assert one('SELECT expires FROM subscriptions WHERE id=?',(SID,))['expires']==plan['target_expires']
    assert one('SELECT status FROM flow_owner_extensions')['status']=='applied'


def test_expired_account_grant_can_be_explicitly_renewed_with_same_key(client, monkeypatch):
    prepared(client)
    now=int(time.time())
    execute("UPDATE subscriptions SET state='expired',expires=? WHERE id=?",(now-60,SID))
    execute("INSERT INTO flow_auto_grants(user_id,subscription_id,target_expires,policy_version,"
            "state,created,next_attempt,attempts) VALUES (1,?,?,1,'expired',?,0,0)",
            (SID,now-60,now-86400))
    planned=client.get(path()+'/plan?days=7',headers=owner())
    assert planned.status_code==200
    p=planned.json()
    assert p['target_expires']>=now+7*86400
    async def provision(sub):
        assert sub['id']==SID
        return _VALID
    monkeypatch.setattr(providers,'provision',provision)
    result=client.post(path(),headers=owner(),json=request(p))
    assert result.status_code==200,result.text
    assert one('SELECT state,expires FROM subscriptions WHERE id=?',(SID,))=={
        'state':'active','expires':p['target_expires']}
    assert one('SELECT state,target_expires FROM flow_auto_grants')['state']=='active'
    assert one('SELECT target_expires FROM flow_auto_grants')['target_expires']==p['target_expires']


def test_renewal_never_bypasses_missing_wgdashboard_or_unissued_config(client, monkeypatch):
    prepared(client)
    data=client.get(path()+'/plan',headers=owner()).json()
    execute("UPDATE subscriptions SET secret=NULL WHERE id=?",(SID,))
    assert client.post(path(),headers=owner(),json=request(data)).status_code==409
    assert one('SELECT count(*) AS n FROM flow_owner_extensions')['n']==0
    execute("UPDATE subscriptions SET secret=? WHERE id=?",(c.encrypt(_VALID),SID))
    execute("UPDATE servers SET settings=? WHERE id='test-wg'",
            (c.encrypt(json.dumps(profile(schedule_expiry=False))),))
    assert client.get(path()+'/plan',headers=owner()).status_code==409
    assert client.post(path(),headers=owner(),json=request(data)).status_code==409
    assert one('SELECT count(*) AS n FROM flow_owner_extensions')['n']==0


def test_owner_approval_and_plan_version_are_required(client, monkeypatch):
    plan=prepared(client)
    body=request(plan)
    assert client.post(path(),json=body).status_code==401
    assert client.post(path(),headers=owner(),json={**body,'confirm':'DELETE'}).status_code==422
    assert client.post(path(),headers=owner(),json={**body,'days':366}).status_code==422
    assert client.post(path(),headers=owner(),json={**body,'target_expires':plan['target_expires']+86400}).status_code==409
    assert client.post(path(),headers=owner(),json={**body,'before_expires':plan['before_expires']+60}).status_code==409
    assert one('SELECT count(*) AS n FROM flow_owner_extensions')['n']==0

def test_missing_existing_key_never_provisions_or_records_intent(client, monkeypatch):
    plan=prepared(client)
    execute("DELETE FROM wgdashboard_keys WHERE credential_id=?", (SID,))
    async def forbidden(sub):
        raise AssertionError('Missing key must fail before panel I/O')
    monkeypatch.setattr(providers,'provision',forbidden)
    assert client.get(path()+'/plan',headers=owner()).status_code==409
    assert client.post(path(),headers=owner(),json=request(plan)).status_code==409
    assert one('SELECT count(*) AS n FROM flow_owner_extensions')['n']==0
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n']==0


def test_extension_updates_existing_device_and_primary_with_same_target(client, monkeypatch):
    plan=prepared(client)
    device=str(uuid4())
    wgdashboard.stored_private_key('test-wg', device)
    execute("INSERT INTO devices(id,subscription_id,request_id,name,state,expires,secret,created) "
            "VALUES (?,?,?,'Phone','active',?,?,1)",
            (device,SID,str(uuid4()),plan['before_expires'],c.encrypt(_VALID)))
    calls=[]
    async def provision(sub):
        assert sub['require_existing_wg_key'] is True
        calls.append((sub['id'],sub['expires']))
        return _VALID
    monkeypatch.setattr(providers,'provision',provision)
    result=client.post(path(),headers=owner(),json=request(plan))
    assert result.status_code==200,result.text
    assert calls==[(SID,plan['target_expires']),(device,plan['target_expires'])]
    assert one('SELECT expires FROM devices WHERE id=?',(device,))['expires']==plan['target_expires']
    assert client.get(path()+'/pending',headers=owner()).json()=={'request':None}


def test_failed_device_extension_preserves_local_expiry_and_recoverable_request(client, monkeypatch):
    plan=prepared(client)
    device=str(uuid4())
    wgdashboard.stored_private_key('test-wg',device)
    execute("INSERT INTO devices(id,subscription_id,request_id,name,state,expires,secret,created) "
            "VALUES (?,?,?,'Phone','active',?,?,1)",
            (device,SID,str(uuid4()),plan['before_expires'],c.encrypt(_VALID)))
    async def provision(sub):
        if sub['id']==device:
            raise TimeoutError()
        return _VALID
    monkeypatch.setattr(providers,'provision',provision)
    body=request(plan)
    assert client.post(path(),headers=owner(),json=body).status_code==502
    assert one('SELECT expires FROM subscriptions WHERE id=?',(SID,))['expires']==plan['before_expires']
    assert one('SELECT expires FROM devices WHERE id=?',(device,))['expires']==plan['before_expires']
    assert client.get(path()+'/pending',headers=owner()).json()['request']==body


def test_owner_can_shorten_active_access_and_retry_the_same_date(client, monkeypatch):
    prepared(client)
    before = int(time.time()) + 30 * 86400
    execute("UPDATE subscriptions SET expires=? WHERE id=?", (before, SID))
    plan = client.get(path()+'/plan?days=5&shorten=true', headers=owner()).json()
    assert plan['target_expires'] == before - 5 * 86400
    seen = []
    async def provision(sub):
        seen.append(sub['expires'])
        return _VALID
    monkeypatch.setattr(providers, 'provision', provision)
    command = request(plan, confirm='SHORTEN_ACCOUNT_WIREGUARD')
    for _ in range(2):
        assert client.post(path(), headers=owner(), json=command).status_code == 200
    assert seen == [before - 5 * 86400]
    assert client.get(path()+'/plan?days=30&shorten=true', headers=owner()).status_code == 422
