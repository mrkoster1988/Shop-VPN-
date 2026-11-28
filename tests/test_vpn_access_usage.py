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
    from app import flow_wireguard
    from app.db import db
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        assert flow_wireguard._feed_locked(conn,sub['user_id'],sid)['state']=='active'


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


def test_expired_restore_releases_only_after_accounting_for_its_key(client, monkeypatch):
    from fastapi import HTTPException
    import pytest
    from app import wgdashboard

    sid = subscription(client)
    execute("UPDATE subscriptions SET state='revoked' WHERE id=?", (sid,))
    execute('UPDATE servers SET settings=? WHERE id=?',
            (config.encrypt(json.dumps(profile(schedule_expiry=True))), 'test-wg'))
    before = one('SELECT expires FROM subscriptions WHERE id=?', (sid,))['expires']
    cmd = SimpleNamespace(subscription_id=sid, actor=uuid4(), request_id=uuid4(),
                          before=before, action='restore', value=1)
    calls = []
    async def offline(sub):
        calls.append(sub['id'])
        raise OSError('offline')
    monkeypatch.setattr(providers, 'disable', offline)
    for key_written in (False, True):
        cmd.request_id = uuid4()
        identity = str(uuid4())
        execute('INSERT INTO vpn_access_changes VALUES(?,?,?,?,?,?,?,?,NULL)',
                (str(cmd.request_id), sid, str(cmd.actor), 'restore', before, 1, 1, identity))
        if key_written:
            wgdashboard.stored_private_key('test-wg', identity)
            with pytest.raises(HTTPException) as error:
                asyncio.run(vpn_lifecycle.change(cmd))
            assert error.value.detail == 'vpn_node_unconfirmed'
            assert one('SELECT applied FROM vpn_access_changes WHERE request_id=?',
                       (str(cmd.request_id),))['applied'] is None
            async def confirm(sub):
                calls.append(sub['id'])
            monkeypatch.setattr(providers, 'disable', confirm)
        for _ in range(2):
            with pytest.raises(HTTPException) as error:
                asyncio.run(vpn_lifecycle.change(cmd))
            assert error.value.detail == 'vpn_restore_elapsed'
        assert calls == ([identity, identity] if key_written else [])
        assert one('SELECT state FROM subscriptions WHERE id=?', (sid,))['state'] == 'revoked'


def test_presence_distinguishes_unissued_first_grant_from_unobserved_subscription(client, monkeypatch):
    from app import flow_auto_provision, flow_presence
    from app.db import db
    from test_shop import register
    register(client)
    owner = str(uuid4())
    execute("INSERT INTO flow_bridge_identities VALUES (?,?,1,0)", (owner, 12345678))
    with db() as conn:
        flow_auto_provision.reserve(conn, 1, {'id':'test-wg'}, {'version':1}, 1800000000, 7)
    monkeypatch.setattr(flow_presence, '_until', 0)
    def snapshot():
        flow_presence._until = 0
        return asyncio.run(flow_presence.snapshot())['accounts'][owner]
    assert snapshot()['first_profile_unissued'] is True
    execute("UPDATE subscriptions SET state='expired'")
    execute("UPDATE flow_auto_grants SET state='expired'")
    assert snapshot()['first_profile_unissued'] is True
    execute("UPDATE subscriptions SET secret=?", (config.encrypt('issued-profile'),))
    assert snapshot()['first_profile_unissued'] is False
