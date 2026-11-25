import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from app import config, providers, service, vpn_lifecycle, wg_activity
from app.db import execute, one
from test_admin_cleanup import subscription
from test_shop import client
from test_wg_peer_diagnostics import profile


def test_restore_uses_fresh_key_once_and_preserves_expiry_on_retry(client,monkeypatch):
    sid=subscription(client)
    disabled=[]
    issued=[]
    async def disable(sub):disabled.append(sub['id'])
    async def provision(sub):
        issued.append((sub['id'],sub['expires']))
        return '[Interface]\nPrivateKey = fixture\n[Peer]\nPublicKey = fixture'
    monkeypatch.setattr(providers,'disable',disable)
    monkeypatch.setattr(providers,'provision',provision)
    def command(action,days):
        row=one('SELECT expires FROM subscriptions WHERE id=?',(sid,))
        return SimpleNamespace(subscription_id=sid,actor=uuid4(),request_id=uuid4(),before=row['expires'],action=action,value=days)
    revoke=command('revoke',0)
    asyncio.run(vpn_lifecycle.change(revoke))
    asyncio.run(vpn_lifecycle.change(revoke))
    assert disabled==[sid]
    execute('UPDATE servers SET settings=? WHERE id=?',(config.encrypt(json.dumps(profile(schedule_expiry=True))),'test-wg'))
    restore=command('restore',7)
    asyncio.run(vpn_lifecycle.change(restore))
    asyncio.run(vpn_lifecycle.change(restore))
    sub=one('SELECT * FROM subscriptions WHERE id=?',(sid,))
    assert sub['state']=='active' and issued==[(service.primary_identity(sub),sub['expires'])]
    assert issued[0][0]!=sid and sub['expires']-restore.before in range(7*86400-5,7*86400+6)


def test_daily_counters_survive_key_and_counter_changes(client):
    sid=subscription(client)
    def record(key,rx,tx,stamp):
        return wg_activity.record([(sid,key,dict(received=rx,sent=tx,last_seen=stamp))],stamp)[sid]
    t=1800000000
    assert record('a',None,None,t)['periods'] is None
    assert record('a',100,200,t+1)['periods']['today']==[0,0]
    assert record('a',120,260,t+60)['periods']['week']==[20,60]
    reset=record('a',9,12,t+120)
    assert reset['received']==129 and reset['periods']['month']==[29,72]
    assert record('b',500,33,t+180)['received']==629
    assert record('b',515,43,t+40*86400)['periods']['month']==[15,10]
