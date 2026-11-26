import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
from app import cabinet_vpn, providers, service
from app.db import execute, one
from test_flow_owner_extension import prepared, SID, _VALID
from test_shop import client


def test_cabinet_commands_are_scoped_and_repeatable(client, monkeypatch):
    plan = prepared(client)
    secret = 'ab' * 32
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET', secret)
    actor, subject = str(uuid4()), str(uuid4())
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)', (subject, 12345678, int(time.time())))
    def submit(obj, tamper=False, stamp=None):
        body = json.dumps(obj).encode()
        stamp = str(stamp or int(time.time()))
        sig = hmac.new(bytes.fromhex(secret), cabinet_vpn.DOMAIN+stamp.encode()+b':'+body, hashlib.sha256)
        return client.post('/api/flow/cabinet-management',content=body+(b' ' if tamper else b''),
            headers={'X-Pushok-Management':stamp+':'+sig.hexdigest(), 'Content-Type':'application/json'})
    before = one('SELECT device_limit FROM subscriptions WHERE id=?',(SID,))['device_limit']
    cmd = dict(actor=actor,subject=subject,action='limit',subscription_id=SID,
               request_id=str(uuid4()),before=before,value=10)
    assert submit(cmd, tamper=True).status_code == 403
    assert submit(cmd, stamp=int(time.time())-90).status_code == 403
    assert submit({**cmd,'subject':str(uuid4())}).status_code == 404
    assert submit(cmd).json() == submit(cmd).json() == {'ok': True}
    assert submit({**cmd,'value':9}).status_code == 409
    assert submit({**cmd,'request_id':str(uuid4()),'value':9}).status_code == 409
    assert one('SELECT device_limit FROM subscriptions WHERE id=?',(SID,))['device_limit'] == 10
    calls=[]
    async def provision(sub):
        calls.append(sub['expires'])
        return _VALID
    monkeypatch.setattr(providers,'provision',provision)
    extend = {**cmd,'action':'extend','value':10,'before':plan['before_expires'],'request_id':str(uuid4())}
    assert submit(extend).json() == submit(extend).json() == {'ok':True}
    assert calls == [plan['before_expires']+864000]
    shorten = {**extend,'action':'shorten','value':2,'before':calls[-1],'request_id':str(uuid4())}
    assert submit(shorten).json() == {'ok':True}
    assert calls[-1] == plan['before_expires']+8*86400


def test_expire_retries_after_panel_failure_and_reactivates_with_same_key(client,monkeypatch):
    plan=prepared(client)
    secret='cd'*32
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',secret)
    actor,subject=str(uuid4()),str(uuid4())
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(subject,12345678,int(time.time())))
    def submit(cmd):
        raw=json.dumps(cmd).encode();stamp=str(int(time.time()))
        proof=hmac.digest(bytes.fromhex(secret),cabinet_vpn.DOMAIN+stamp.encode()+b':'+raw,'sha256').hex()
        return client.post('/api/flow/cabinet-management',content=raw,headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+proof})
    cmd=dict(actor=actor,subject=subject,action='expire',subscription_id=SID,request_id=str(uuid4()),
             before=plan['before_expires'],value=0)
    calls=[]
    async def disable(sub):
        calls.append(sub['id'])
        if len(calls)==1:raise RuntimeError('test outage')
    monkeypatch.setattr(providers,'disable',disable)
    failed=submit(cmd)
    assert failed.status_code==502 and failed.json()['detail']=='vpn_node_unconfirmed'
    read=lambda:one('SELECT state,expires,credential_id FROM subscriptions WHERE id=?',(SID,))
    assert read()['state']=='active' and read()['expires']==cmd['before']
    assert submit(cmd).json()==submit(cmd).json()=={'ok':True}
    expired=read()
    assert expired['state']=='expired' and expired['expires']<=int(time.time())
    assert len(calls)==2
    async def provision(sub):
        assert sub['require_existing_wg_key']
        return _VALID
    monkeypatch.setattr(providers,'provision',provision)
    active={**cmd,'action':'extend','value':7,'before':expired['expires'],'request_id':str(uuid4())}
    assert submit(active).json()=={'ok':True}
    assert read()['state']=='active' and read()['credential_id']==expired['credential_id']


@pytest.mark.parametrize("invoice_state", ["pending", "checkout", "paid"])
def test_restore_with_unissued_invoice_and_later_payment(client,monkeypatch,invoice_state):
    import asyncio
    from app import flow_presence
    prepared(client)
    sid=SID
    subject=str(uuid4())
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(subject,12345678,int(time.time())))
    execute("UPDATE subscriptions SET state='revoked',primary_key_state='revoked',secret=NULL,expires=1 WHERE id=?",(sid,))
    execute("""INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,created,status,kind)
               VALUES ('old-plan',1,'wg','old','Семейный',30,399,1,'active','purchase'),
                      ('bonus',1,'wg','ref','Бонус',1,0,2,'active','referral'),
                      ('draft',1,'wg','new','Ещё не оплачен',90,899,3,'pending','purchase')""")
    execute('UPDATE orders SET status=? WHERE id=?',(invoice_state,'draft'))
    monkeypatch.setattr(flow_presence,'_until',0)
    data=asyncio.run(flow_presence.snapshot())
    assert data['accounts'][subject]['tariff_name']=='Семейный'
    key='de'*32;monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',key)
    cmd=dict(actor=str(uuid4()),subject=subject,action='restore',subscription_id=sid,
             request_id=str(uuid4()),before=one('SELECT expires FROM subscriptions WHERE id=?',(sid,))['expires'],value=1)
    raw=json.dumps(cmd).encode();stamp=str(int(time.time()))
    digest=hmac.digest(bytes.fromhex(key),cabinet_vpn.DOMAIN+stamp.encode()+b':'+raw,'sha256').hex()
    def submit():
        return client.post('/api/flow/cabinet-management',content=raw,
            headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+digest})
    calls=[]
    async def provision(sub):
        calls.append((sub['id'], sub['expires']))
        return _VALID
    monkeypatch.setattr(providers, 'provision', provision)
    assert submit().json() == submit().json() == {'ok': True}
    assert len(calls) == 1 and calls[0][0] != SID
    assert abs(calls[0][1] - time.time() - 86400) < 5
    assert one('SELECT state,expires FROM subscriptions WHERE id=?',(sid,)) == {'state':'active','expires':calls[0][1]}
    assert one("SELECT status,target_expires FROM orders WHERE id='draft'") == {'status':invoice_state,'target_expires':None}
    execute("UPDATE orders SET status='paid' WHERE id='draft'")
    asyncio.run(service.fulfill('draft'))
    asyncio.run(service.fulfill('draft'))
    assert calls == [calls[0], (calls[0][0], calls[0][1]+90*86400)]
    assert one("SELECT status FROM orders WHERE id='draft'")['status'] == 'active'


def test_revoked_purchase_retry_keeps_one_new_key_and_identifies_real_order(client,monkeypatch):
    import asyncio
    from app import vpn_lifecycle
    from fastapi import HTTPException
    prepared(client)
    before=1
    execute("UPDATE subscriptions SET state='revoked',primary_key_state='revoked',secret=NULL,expires=? WHERE id=?",(before,SID))
    execute("""INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status)
               VALUES ('paid-order',1,'wg','month',30,199,1,'paid')""")
    calls=[]
    disabled=[]
    async def provision(sub):
        calls.append((sub['id'],sub['expires']))
        if len(calls)==1: raise RuntimeError('Simulated interrupted issuance')
        return _VALID
    async def disable(sub): disabled.append(sub['id'])
    monkeypatch.setattr(providers,'provision',provision)
    monkeypatch.setattr(providers,'disable',disable)
    asyncio.run(service.fulfill('paid-order'))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(vpn_lifecycle.change(cabinet_vpn.Command(actor=uuid4(),subject=uuid4(),request_id=uuid4(),
            subscription_id=SID,action='restore',before=before,value=1)))
    assert failure.value.status_code == 409
    assert failure.value.detail == {'code':'vpn_order_issuing','order_id':'paid-order'}
    execute("UPDATE orders SET next_attempt=0 WHERE id='paid-order'")
    asyncio.run(service.fulfill('paid-order'))
    assert calls[0] == calls[1] and calls[0][0] != SID
    assert disabled == [SID]
    assert one("SELECT status FROM orders WHERE id='paid-order'")['status'] == 'active'
    assert service.primary_identity(one('SELECT * FROM subscriptions WHERE id=?',(SID,))) == calls[0][0]
