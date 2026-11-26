import hashlib
import hmac
import json
import time
from uuid import uuid4

from app import cabinet_vpn, providers
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


def test_presence_reports_purchased_tariff_and_precise_order_conflict(client,monkeypatch):
    import asyncio
    from app import flow_presence
    from test_admin_cleanup import subscription
    sid=subscription(client)
    subject=str(uuid4())
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(subject,12345678,int(time.time())))
    execute("UPDATE subscriptions SET state='revoked' WHERE id=?",(sid,))
    execute("""INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,created,status,kind)
               VALUES ('old-plan',1,'wg','old','Семейный',30,399,1,'active','purchase'),
                      ('bonus',1,'wg','ref','Бонус',1,0,2,'active','referral'),
                      ('draft',1,'wg','new','Ещё не оплачен',90,899,3,'pending','purchase')""")
    monkeypatch.setattr(flow_presence,'_until',0)
    data=asyncio.run(flow_presence.snapshot())
    assert data['accounts'][subject]['tariff_name']=='Семейный'
    key='de'*32;monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',key)
    cmd=dict(actor=str(uuid4()),subject=subject,action='restore',subscription_id=sid,
             request_id=str(uuid4()),before=one('SELECT expires FROM subscriptions WHERE id=?',(sid,))['expires'],value=7)
    raw=json.dumps(cmd).encode();stamp=str(int(time.time()))
    digest=hmac.digest(bytes.fromhex(key),cabinet_vpn.DOMAIN+stamp.encode()+b':'+raw,'sha256').hex()
    result=client.post('/api/flow/cabinet-management',content=raw,headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+digest})
    assert result.status_code==409 and result.json()['detail']=='vpn_order_in_progress'
