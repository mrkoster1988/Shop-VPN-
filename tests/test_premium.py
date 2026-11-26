import asyncio
import time
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import cabinet_notices, flow_wireguard, premium, providers, vpn_plans, wgdashboard
from app.db import execute, one
from test_flow_owner_extension import prepared, SID
from test_flow_wireguard import _VALID
from test_shop import client
from test_wg_peer_diagnostics import profile


def grant_request(client):
    prepared(client)
    subject=uuid4()
    execute('INSERT INTO flow_bridge_identities VALUES(?,?,1,?)',(str(subject),12345678,int(time.time())))
    before=one('SELECT expires FROM subscriptions WHERE id=?',(SID,))['expires']
    return vpn_plans.Choice(actor=uuid4(),subject=subject,before=before,subscription_id=SID,
                           plan_id=premium.PLAN_ID,plan_version=1,version=0)


def test_gift_confirmed_once_and_downgrade_creates_finite_term(client,monkeypatch):
    request=grant_request(client)
    targets=[]
    async def configure(sub):
        targets.append(sub['expires'])
        return _VALID
    monkeypatch.setattr(providers,'provision',configure)
    assert one('SELECT id FROM plans WHERE id=?',(premium.PLAN_ID,)) is None
    orders=one('SELECT count(*) n FROM orders')['n']
    with pytest.raises(HTTPException) as denied:
        asyncio.run(vpn_plans.choose(request.model_copy(update={'subject':uuid4()})))
    assert denied.value.status_code==404
    for _ in range(2):
        assert asyncio.run(vpn_plans.choose(request))=={'name':'Premium','version':1}
    assert targets==[premium.NO_EXPIRY]
    assert flow_wireguard.feed_for_owned_subscription(1,SID)['state']=='active'
    assert one('SELECT count(*) n FROM orders')['n']==orders
    assert premium.checkout_blocked(1)
    event=one("SELECT * FROM cabinet_vpn_notices WHERE kind='premium'")
    assert 'Оплата за счёт заведения' in cabinet_notices.message(event,{})
    assert one("SELECT count(*) n FROM cabinet_vpn_notices WHERE kind='premium'")['n']==1
    rate=one('SELECT * FROM plans WHERE enabled AND NOT is_free ORDER BY days LIMIT 1')
    remove=request.model_copy(update=dict(plan_id=rate['id'],plan_version=rate['version'],version=1,before=premium.NO_EXPIRY))
    result=asyncio.run(vpn_plans.choose(remove))
    assert result['version']==2
    assert abs(targets[-1]-time.time()-86400*rate['days'])<3
    assert not premium.checkout_blocked(1)


def test_gift_waits_for_node_then_recovers(client,monkeypatch):
    request=grant_request(client)
    async def offline(sub): raise ValueError('offline')
    monkeypatch.setattr(providers,'provision',offline)
    with pytest.raises(HTTPException):
        asyncio.run(vpn_plans.choose(request))
    assert one('SELECT expires FROM subscriptions WHERE id=?',(SID,))['expires']==request.before
    assert not one("SELECT 1 FROM cabinet_vpn_notices WHERE kind='premium'")
    async def ready(sub): return _VALID
    monkeypatch.setattr(providers,'provision',ready)
    asyncio.run(premium.resume_pending())
    assert one('SELECT expires FROM subscriptions WHERE id=?',(SID,))['expires']==premium.NO_EXPIRY
    assert one("SELECT count(*) n FROM cabinet_vpn_notices WHERE kind='premium'")['n']==1


def test_only_shop_owned_peer_schedule_is_removed(client,monkeypatch):
    grant_request(client)
    private=wgdashboard.stored_private_key('test-wg',SID,create=False)
    adapter=wgdashboard.WGDashboard(profile(schedule_expiry=True))
    execute('INSERT OR REPLACE INTO wgdashboard_expirations VALUES(?,?,?,?,?)',('test-wg',SID,'ours',1800000000,1))
    jobs=[{'JobID':'ours','Action':'delete'},{'JobID':'someone-else','Action':'restrict'}]
    async def peer(client,key):return 'configurationPeers',{'jobs':jobs.copy()}
    async def remove(client,method,path,json):
        assert method=='DELETE' and path=='PeerScheduleJob' and json['JobID']=='ours'
        jobs.clear()
    monkeypatch.setattr(adapter,'peer',peer)
    monkeypatch.setattr(adapter,'request',remove)
    with pytest.raises(wgdashboard.PanelError):
        asyncio.run(adapter.clear_expiration(None,private,'test-wg',SID))
    assert len(jobs)==2
    jobs.pop()
    asyncio.run(adapter.clear_expiration(None,private,'test-wg',SID))
    asyncio.run(adapter.clear_expiration(None,private,'test-wg',SID))
    assert not jobs and not one('SELECT 1 FROM wgdashboard_expirations WHERE credential_id=?',(SID,))


def test_miniapp_owner_assigning_a_plan_addresses_the_subscriber(client):
    req=grant_request(client)
    now=int(time.time())
    execute('UPDATE users SET telegram_id=12345678 WHERE id=1')
    execute('INSERT INTO users(telegram_id,created) VALUES(?,?)',(87654321,now))
    owner=one('SELECT id FROM users WHERE telegram_id=87654321')
    execute('INSERT INTO flow_bridge_identities VALUES(?,?,?,?)',(str(req.actor),87654321,owner['id'],now))
    rate=one('SELECT * FROM plans WHERE enabled AND NOT is_free ORDER BY days LIMIT 1')
    regular=req.model_copy(update={'plan_id':rate['id'],'plan_version':rate['version']})
    asyncio.run(vpn_plans.choose(regular))
    asyncio.run(vpn_plans.choose(regular))
    events=cabinet_notices.claim(now+1,True)
    assert [(n['category'],n['telegram_id']) for n in events]==[('renewal_plan',12345678)]
