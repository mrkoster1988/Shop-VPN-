import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app import flow_presence, vpn_plans, wg_client_platform as platform, wgdashboard
from app.db import db, execute, one
from test_flow_owner_extension import prepared,SID
from test_shop import client


def test_catalog_and_assignment_preserve_access_and_finance(client):
    prepared(client)
    actor,subject=uuid4(),uuid4()
    execute('INSERT INTO flow_bridge_identities VALUES(?,?,1,?)',(str(subject),12345678,int(time.time())))
    catalog=asyncio.run(vpn_plans.catalog('plans',b'{}'))
    base=next(p for p in catalog['plans'] if not p['is_free'])
    payload={k:base[k] for k in ('id','version','name','days','price','stars','enabled','device_limit')}
    payload.update(device_limit=6,name='Семейный тест')
    edited=asyncio.run(vpn_plans.catalog('plan-update',json.dumps(payload).encode()))
    with pytest.raises(HTTPException):
        asyncio.run(vpn_plans.catalog('plan-update',json.dumps(payload).encode()))
    before=one('SELECT expires,secret,state FROM subscriptions WHERE id=?',(SID,))
    cmd=vpn_plans.Choice(actor=actor,subject=subject,subscription_id=SID,before=before['expires'],version=0,
                         plan_id=edited['id'],plan_version=edited['version'])
    result=asyncio.run(vpn_plans.choose(cmd))
    assert asyncio.run(vpn_plans.choose(cmd))==result
    assert one('SELECT device_limit FROM subscriptions WHERE id=?',(SID,))['device_limit']==6
    assert one('SELECT expires,secret,state FROM subscriptions WHERE id=?',(SID,))==before
    assert one("SELECT count(*) n FROM cabinet_vpn_notices WHERE kind='renewal_plan'")['n']==1
    with pytest.raises(HTTPException) as err:
        asyncio.run(vpn_plans.choose(cmd.model_copy(update={'subject':uuid4()})))
    assert err.value.status_code==404


def test_device_hint_requires_same_key_address_and_unambiguous_platform(client):
    prepared(client)
    private=wgdashboard.stored_private_key('test-wg',SID,create=False)
    key=wgdashboard.WGDashboard.identity(private)
    request=SimpleNamespace(client=SimpleNamespace(host='8.8.4.4'),headers={'user-agent':'Pushok-Flow-Subscriptions'})
    config='[Interface]\nPrivateKey = '+private+'\n[Peer]'
    platform.observe_import(request,config)
    now=int(time.time())
    with db() as conn: hints=platform.current(conn,now)
    assert platform.match_peer(hints,key,'8.8.4.4',now)=='android'
    assert platform.match_peer(hints,key,'8.8.8.8',now) is None
    assert platform.match_peer(hints,'other','8.8.4.4',now) is None
    assert platform.match_peer(hints,key,'8.8.4.4',now-600) is None
    request.headers={'user-agent':'iPhone'}
    platform.observe_import(request,config)
    with db() as conn: hints=platform.current(conn,now)
    assert platform.match_peer(hints,key,'8.8.4.4',now) is None
    assert [platform.system(s) for s in ('iPad','Windows NT','Macintosh','Linux x86_64','curl')]==['ios','windows','macos','linux',None]


def test_forwarded_endpoint_only_trusted_from_configured_edge(monkeypatch):
    monkeypatch.setenv('SHOP_PLATFORM_EDGE_PROXY','shop-caddy')
    monkeypatch.setattr(platform,'proxy_addresses',lambda host, minute:{'172.16.0.4'})
    req=SimpleNamespace(client=SimpleNamespace(host='8.8.4.4'),headers={'x-forwarded-for':'1.1.1.1, 8.8.8.8'})
    assert platform.request_address(req)=='8.8.4.4'
    req.client.host='172.16.0.4'
    assert platform.request_address(req)=='8.8.8.8'


def test_catalog_bridge_rejects_unsigned_and_creates_with_signature(client,monkeypatch):
    import hmac
    raw_key='6a'*32
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',raw_key)
    route='/api/flow/vpn-settings/plan-create'
    body=json.dumps(dict(id='new_test',name='Новый тариф',price=100,stars=50,enabled=True,days=30)).encode()
    assert client.post(route,content=body).status_code==403
    stamp=str(int(time.time()))
    signature=hmac.digest(bytes.fromhex(raw_key),b'pushok-vpn-settings-v1:'+stamp.encode()+b':plan-create:'+body,'sha256').hex()
    response=client.post(route,content=body,headers={'X-Pushok-Management':stamp+':'+signature})
    assert response.status_code in (200,201),response.text
    assert response.json()['id']=='new_test'
