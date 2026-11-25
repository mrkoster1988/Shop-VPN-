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
