"""Account terms, durable placement work and management trust boundaries."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest

from app import config as c, providers, service, wg_fleet as fleet, wg_fleet_health as health
from app.db import db, one
from test_shop import client, register, admin_headers

CONFIG = ('[Interface]\nPrivateKey = AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=\n'
          'Address = 10.77.0.2/32\n[Peer]\nPublicKey = AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=\n'
          'Endpoint = 8.8.8.8:51820\nAllowedIPs = 0.0.0.0/0, ::/0\n')


def command(client, action, payload, rid=None):
    return client.post('/api/admin/wg-fleet/commands',headers=admin_headers(),json={
        'action':action,'payload':payload,'request_id':rid or str(uuid.uuid4())})


def observation(sid,version=1,sequence=1,**changes):
    now=int(time.time())
    return {'schema_version':1,'server_id':sid,'server_version':version,'generation':1,
        'probe_id':'probe-'+sid,'sequence':sequence,'started_at':now-3,'finished_at':now,
        'handshake_at':now-1,'rx_delta_bytes':4096,'tx_delta_bytes':2048,'dns_ok':True,'https_ok':True,
        'cpu_percent':10.0,'rx_bps':1000,'tx_bps':1000,'bandwidth_capacity_bps':100000,
        'failure_code':None,**changes}


def node(sid,*,load=10,capacity=20):
    profile={'api':'wgdashboard','url':'https://'+sid+'.example','token':'t'*32,
             'configuration':'wg0','schedule_expiry':True,'panel_timezone':'Europe/Moscow'}
    with db() as conn:
        conn.execute("INSERT INTO servers(id,name,protocol,state,health,capacity,settings,created) VALUES (?,?,'wg','active','ok',?,?,?)",(sid,sid,capacity,c.encrypt(json.dumps(profile)),int(time.time())))
        health.register_probe(conn,sid,1,'probe-'+sid,1)
        data=observation(sid,cpu_percent=load)
        health.ingest(conn,data,data['probe_id'])
        fleet.record_failure(conn,data)


def entitled(client,server='node-a'):
    register(client)
    expires=int(time.time())+86400
    with db() as conn:
        conn.execute("INSERT INTO subscriptions(id,user_id,protocol,state,expires,server_id,secret,credential_id,access_source) VALUES ('sub',1,'wg','active',?,?,?,?,'purchase')",(expires,server,c.encrypt(CONFIG),c.encrypt('original-key')))
        conn.execute('UPDATE wg_fleet_policy SET enabled=1 WHERE id=1')
    return expires


def fake_provider(monkeypatch):
    created,revoked=[],[]
    async def provision(sub):
        created.append(dict(sub))
        return CONFIG.replace('8.8.8.8',sub['server_id']+'.example')
    async def disable(sub):
        revoked.append(dict(sub))
    monkeypatch.setattr(providers,'provision',provision)
    monkeypatch.setattr(providers,'disable',disable)
    return created,revoked


def reconcile():
    asyncio.run(fleet.reconcile())


def test_minimum_load_atomic_reservation_and_idempotent_feed(client,monkeypatch):
    node('node-a',load=60);node('node-b',load=10);node('node-c',load=40)
    expiry=entitled(client)
    created,_=fake_provider(monkeypatch)
    reconcile()
    assert len(created)==1 and created[0]['server_id']=='node-b'
    response=client.get('/api/flow/wireguard/subscriptions/sub').json()
    assert response['expires_at']==expiry and len(response['profiles'])==2
    assert response['profiles'][0]['id']=='primary'
    assert response['profiles'][1]['id']!='primary'
    for _ in range(4):
        assert client.get('/api/flow/wireguard/subscriptions/sub').json()==response
    reconcile()
    assert len(created)==1
    with db() as conn:
        assert fleet.counts(conn,'node-a')['occupied']==1
        assert fleet.counts(conn,'node-a')['reserved']==0
        assert fleet.counts(conn,'node-b')['reserved']==1
        assert conn.execute('SELECT count(*) FROM subscriptions').fetchone()[0]==1


def test_lost_ack_reuses_peer_identity_and_does_not_publish_unconfirmed(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    attempts=[]
    async def provision(sub):
        attempts.append(sub['id'])
        if len(attempts)==1:raise TimeoutError()
        return CONFIG
    monkeypatch.setattr(providers,'provision',provision)
    reconcile()
    assert len(client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles'])==1
    with db() as conn:conn.execute('UPDATE wg_fleet_placements SET next_attempt=0')
    reconcile()
    assert len(attempts)==2 and attempts[0]==attempts[1]
    assert len(client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles'])==2


def test_drain_preserves_link_term_and_keys_until_deadline(client,monkeypatch):
    node('node-a');node('node-b');node('node-c',load=30)
    expiry=entitled(client)
    created,revoked=fake_provider(monkeypatch)
    link=client.put('/api/flow/wireguard/subscriptions/sub/link',json={'action':'enable','version':0}).json()['url']
    reconcile()
    before=client.get(link).json()
    response=command(client,'drain',{'id':'node-a','version':1,'deadline_hours':24,'lost':False})
    assert response.status_code==200,response.text
    oid=response.json()['operation']['id']
    reconcile()
    current=one("SELECT * FROM subscriptions WHERE id='sub'")
    assert current['server_id']=='node-b' and current['expires']==expiry and current['user_id']==1
    assert not revoked
    moved=client.get(link).json()
    assert moved['revision']>before['revision'] and moved['expires_at']==expiry
    assert 'node-b' in moved['profiles'][0]['config']
    with db() as conn:
        op=fleet.operation(conn,oid)
        assert op['completed']==1 and op['state']=='revoking'
        conn.execute('UPDATE wg_fleet_placements SET retire_after=0 WHERE server_id=?',('node-a',))
    reconcile()
    assert any(r['server_id']=='node-a' and r['id']=='original-key' for r in revoked)
    with db() as conn:
        assert fleet.operation(conn,oid)['state']=='completed'
    assert client.put('/api/flow/wireguard/subscriptions/sub/link',json={'action':'enable','version':1}).json()['url']==link


def test_unreachable_old_node_stays_unconfirmed_and_revocation_cannot_reissue(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    created,_=fake_provider(monkeypatch)
    reconcile()
    async def offline(sub):raise TimeoutError()
    monkeypatch.setattr(providers,'disable',offline)
    result=client.post('/api/admin/subscriptions/sub/revoke',headers=admin_headers())
    assert result.status_code in (409,502)
    # Seeded test peers lack WGDashboard key rows, so exercise the same durable
    # revoke primitive directly as well; its result must not depend on the panel.
    with pytest.raises(RuntimeError,match='Unconfirmed'):
        asyncio.run(fleet.revoke_placements(one("SELECT * FROM subscriptions WHERE id='sub'")))
    total=len(created)
    reconcile()
    assert len(created)==total
    assert client.get('/api/flow/wireguard/subscriptions/sub').json()['state']=='revoked'
    assert one("SELECT count(*) n FROM wg_fleet_placements WHERE state='revoked'")['n']==0


def test_renewal_updates_standby_and_expiry_revokes_all(client,monkeypatch):
    node('node-a');node('node-b')
    expiry=entitled(client)
    created,revoked=fake_provider(monkeypatch)
    reconcile()
    with db() as conn:conn.execute('UPDATE subscriptions SET expires=? WHERE id=?',(expiry+86400,'sub'))
    assert len(client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles'])==1
    reconcile()
    assert created[-1]['expires']==expiry+86400
    assert created[0]['id']==created[-1]['id']
    assert len(client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles'])==2
    with db() as conn:conn.execute('UPDATE subscriptions SET expires=? WHERE id=?',(int(time.time())-1,'sub'))
    reconcile()
    assert {r['server_id'] for r in revoked}=={'node-a','node-b'}
    assert client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles']==[]


def test_monitoring_outage_never_mass_drains_or_revokes_ready_reserves(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    _,revoked=fake_provider(monkeypatch)
    reconcile()
    with db() as conn:
        conn.execute('UPDATE wg_fleet_health SET finished_at=finished_at-600,handshake_at=handshake_at-600')
    reconcile()
    with db() as conn:
        assert fleet.candidates(conn)==[]
        assert conn.execute('SELECT count(*) FROM wg_fleet_operations').fetchone()[0]==0
    assert not revoked
    assert len(client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles'])==2


def test_create_request_is_atomic_replayable_and_secret_never_returned(client):
    payload={'name':'Moscow','location':'RU','protocol':'wg','capacity':50,
             'settings':{'api':'wgdashboard','url':'https://panel.example','token':'s'*64,
                         'configuration':'wg0','schedule_expiry':True,'panel_timezone':'Europe/Moscow'}}
    rid=str(uuid.uuid4())
    first=command(client,'create',payload,rid)
    assert first.status_code==200,first.text
    assert 's'*64 not in first.text
    assert first.json()['node']['domain']=='wg1.pushok.shop'
    assert command(client,'create',payload,rid).json()==first.json()
    assert command(client,'create',{**payload,'name':'Other'},rid).status_code==409
    assert command(client,'create',payload).json()['node']['domain']=='wg2.pushok.shop'
    with db() as conn:assert conn.execute('SELECT count(*) FROM wg_fleet_nodes').fetchone()[0]==2
    invalid=command(client,'create',{**payload,'settings':{**payload['settings'],'token':'SECRET-BAD'}})
    assert invalid.status_code==422 and 'SECRET-BAD' not in invalid.text


def test_management_bridge_domain_role_and_key_are_enforced(client,monkeypatch):
    node('node-a')
    monkeypatch.setattr(c,'SECURE_COOKIE',True)
    monkeypatch.setenv('WG_FLEET_MANAGEMENT_ENABLED','true')
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET','ab'*32)
    actor=str(uuid.uuid4())
    def send(action='snapshot',role='admin',domain=b'pushok-wg-fleet-management-v1:',payload=None):
        body=json.dumps({'actor':actor,'actor_role':role,'action':action,'payload':payload or {},'request_id':str(uuid.uuid4())}).encode()
        stamp=str(int(time.time()))
        digest=hmac.new(bytes.fromhex('ab'*32),domain+stamp.encode()+b':'+body,hashlib.sha256).hexdigest()
        return client.post('/api/admin/wg-fleet/bridge',content=body,headers={'X-Pushok-Management':stamp+':'+digest,'Content-Type':'application/json'})
    assert send(domain=b'wrong:').status_code==401
    result=send()
    assert result.status_code==200 and 'settings' not in result.json()['nodes'][0]
    assert send('state',payload={'id':'node-a','version':1,'state':'disabled'}).status_code==403
    assert send(role='developer').json()['nodes'][0]['settings']['token_configured'] is True


def test_metadata_change_rebases_probe_but_panel_change_requires_reenrollment(client,monkeypatch):
    node('node-a')
    monkeypatch.setattr(c,'SECURE_COOKIE',True)
    enrolled=client.post('/api/admin/wg-fleet/probes/enroll',headers=admin_headers(),json={'id':'node-a','version':1}).json()
    headers={'Authorization':'Bearer '+enrolled['token']}
    assert client.get('/api/admin/wg-fleet/probes/assignment',headers=headers).json()['server_version']==1
    result=command(client,'update',{'id':'node-a','version':1,'name':'Renamed','location':'RU','capacity':20})
    assert result.status_code==200,result.text
    assert client.get('/api/admin/wg-fleet/probes/assignment',headers=headers).json()['server_version']==2
    result=command(client,'update',{'id':'node-a','version':2,'name':'Renamed','location':'RU','capacity':20,'settings':{'token':'x'*64}})
    assert result.status_code==200
    assert client.get('/api/admin/wg-fleet/probes/assignment',headers=headers).status_code==409


def test_registration_without_nodes_preserves_trial_start_and_one_claim(client):
    from app import flow_auto_provision
    with db() as conn:
        conn.execute('UPDATE wg_fleet_policy SET enabled=1')
        conn.execute('UPDATE flow_auto_policy SET enabled=1,days=7')
    register(client)
    user=one('SELECT * FROM users WHERE id=1')
    sub=one("SELECT * FROM subscriptions WHERE user_id=1 AND protocol='wg'")
    grant=one('SELECT * FROM flow_auto_grants WHERE user_id=1')
    assert sub['state']=='pending' and sub['server_id'] is None
    assert grant['target_expires']==user['created']+7*86400
    assert flow_auto_provision.enqueue(1)=='pending'
    assert one('SELECT count(*) n FROM flow_auto_grants')['n']==1


def test_managed_endpoint_uses_udp_domain_and_requires_matching_export(client):
    node('node-a')
    with db() as conn:
        conn.execute("INSERT INTO wg_fleet_nodes(server_id,domain,endpoint_ip,dns_state,created) VALUES ('node-a','wg1.pushok.shop','8.8.8.8','ready',0)")
    assert 'Endpoint = wg1.pushok.shop:51820' in fleet.render_config('node-a',CONFIG)
    with pytest.raises(RuntimeError,match='Mismatch'):
        fleet.render_config('node-a',CONFIG.replace('8.8.8.8','1.1.1.1'))


def test_common_probe_target_failure_keeps_entire_fleet_recoverable(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    created,revoked=fake_provider(monkeypatch)
    reconcile()
    with db() as conn:
        for sequence in (2,3):
            for sid in ('node-a','node-b'):
                report=observation(sid,sequence=sequence,https_ok=False,failure_code='https_failed')
                health.ingest(conn,report,report['probe_id'])
                fleet.record_failure(conn,report)
    reconcile()
    with db() as conn:
        assert conn.execute('SELECT count(*) FROM wg_fleet_operations').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM servers WHERE id IN ('node-a','node-b') AND state='active'").fetchone()[0]==2
        assert fleet.candidates(conn)==[]
    assert not revoked and len(created)==1
    assert len(client.get('/api/flow/wireguard/subscriptions/sub').json()['profiles'])==2
    with db() as conn:
        report=observation('node-b',sequence=4)
        health.ingest(conn,report,report['probe_id']);fleet.record_failure(conn,report)
    reconcile()
    assert one("SELECT server_id FROM subscriptions WHERE id='sub'")['server_id']=='node-b'


def test_concurrent_reservations_do_not_overbook_the_minimum(client):
    from concurrent.futures import ThreadPoolExecutor
    node('node-a',load=60);node('node-b',load=0,capacity=1);node('node-c',load=0,capacity=1)
    entitled(client)
    with db() as conn:
        conn.execute("INSERT INTO users(id,email,created) VALUES (2,'second@example.com',?)",(int(time.time()),))
        conn.execute("INSERT INTO subscriptions(id,user_id,protocol,state,expires,server_id,secret) VALUES ('sub2',2,'wg','active',?,'node-a',?)",(int(time.time())+86400,c.encrypt(CONFIG)))
    def reserve(sid):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub=dict(conn.execute('SELECT * FROM subscriptions WHERE id=?',(sid,)).fetchone())
            return fleet.reserve_standby(conn,sub)['server_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        selected=list(pool.map(reserve,['sub','sub2']))
    assert set(selected)=={'node-b','node-c'}


def test_device_reserve_migrates_with_same_account_term_and_identity(client,monkeypatch):
    node('node-a');node('node-b')
    expires=entitled(client)
    created,revoked=fake_provider(monkeypatch)
    with db() as conn:
        conn.execute("INSERT INTO devices(id,subscription_id,request_id,name,state,expires,secret,credential_id,created) VALUES ('phone','sub','req','Phone','active',?,?,?,0)",(expires,c.encrypt(CONFIG),c.encrypt('device-key')))
    reconcile()
    feed=client.get('/api/flow/wireguard/devices/phone').json()
    # Use the module entry point too, independent of route naming.
    from app import flow_wireguard_devices
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        feed=flow_wireguard_devices._feed_locked(conn,1,'phone')
    assert len(feed['profiles'])==2
    assert sum(p['id']=='device-key' for p in created)==1
    assert command(client,'drain',{'id':'node-a','version':1,'deadline_hours':1}).status_code==200
    reconcile()
    assert one("SELECT server_id FROM subscriptions WHERE id='sub'")['server_id']=='node-b'
    device=one("SELECT * FROM devices WHERE id='phone'")
    assert device['expires']==expires and c.decrypt(device['credential_id'])=='device-key'
    assert 'node-b' in c.decrypt(device['secret'])
    assert not revoked


def test_primary_key_rotation_also_invalidates_exported_backup(client,monkeypatch):
    node('node-a');node('node-b');node('node-c',load=40)
    entitled(client)
    created,revoked=fake_provider(monkeypatch)
    reconcile()
    original_backup=created[0]['id']
    with db() as conn:
        conn.execute("UPDATE subscriptions SET credential_id=?,secret=? WHERE id='sub'",(c.encrypt('rotated-primary'),c.encrypt(CONFIG)))
    reconcile()
    assert any(r['id']==original_backup for r in revoked)
    assert all(p['id']!=original_backup for p in created[1:])


def test_fleet_account_does_not_apply_legacy_commercial_device_limit(client):
    node('node-a')
    entitled(client)
    with db() as conn:conn.execute("UPDATE subscriptions SET device_limit=0 WHERE id='sub'")
    for n in range(3):
        response=client.post('/api/subscriptions/sub/devices',json={'request_id':str(uuid.uuid4()),'name':f'Device {n}'})
        assert response.status_code==201,response.text
    assert client.get('/api/subscriptions/sub/devices').json()['max_extra'] is None
    assert client.get('/api/me').json()['subscriptions'][0]['unlimited_devices'] is True


def test_reissue_waits_for_revoking_standby_without_unique_constraint_deadlock(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    created,revoked=fake_provider(monkeypatch)
    reconcile()
    with db() as conn:
        conn.execute("UPDATE wg_fleet_placements SET state='revoking' WHERE role='standby'")
        fleet.new_access(conn,'sub')
    reconcile()
    assert len(created)==1 and len(revoked)==1
    reconcile()
    assert len(created)==2 and created[0]['id']!=created[1]['id']


def test_never_attempted_reservation_can_be_canceled_without_remote_key(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    _,revoked=fake_provider(monkeypatch)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        sub=dict(conn.execute("SELECT * FROM subscriptions WHERE id='sub'").fetchone())
        reserved=fleet.reserve_standby(conn,sub)
        conn.execute("UPDATE wg_fleet_placements SET state='revoking' WHERE id=?",(reserved['id'],))
    asyncio.run(fleet._apply_placement(reserved['id']))
    assert not revoked
    assert one('SELECT state FROM wg_fleet_placements WHERE id=?',(reserved['id'],))['state']=='revoked'


def test_draining_standby_only_node_retires_device_peers_and_finishes(client,monkeypatch):
    node('node-a');node('node-b');node('node-c',load=40)
    expires=entitled(client)
    _,revoked=fake_provider(monkeypatch)
    with db() as conn:
        conn.execute("INSERT INTO devices(id,subscription_id,request_id,name,state,expires,secret,credential_id,created) VALUES ('phone','sub','req','Phone','active',?,?,?,0)",(expires,c.encrypt(CONFIG),c.encrypt('device-key')))
    reconcile()
    op=command(client,'drain',{'id':'node-b','version':1,'deadline_hours':24}).json()['operation']
    reconcile()
    with db() as conn:
        old=conn.execute("SELECT role,state FROM wg_fleet_placements WHERE server_id='node-b'").fetchall()
        assert len(old)==2 and all(row['role']=='retiring' for row in old)
        assert not revoked  # planned grace applies also to clients using a backup
        conn.execute("UPDATE wg_fleet_placements SET retire_after=0 WHERE server_id='node-b'")
    reconcile()
    with db() as conn:
        assert fleet.operation(conn,op['id'])['state']=='completed'
    assert len([r for r in revoked if r['server_id']=='node-b'])==2


@pytest.mark.parametrize('device_state',['revoking','rotating'])
def test_device_revoke_reaches_backup_even_when_primary_is_unreachable(client,monkeypatch,device_state):
    node('node-a');node('node-b')
    expires=entitled(client)
    fake_provider(monkeypatch)
    with db() as conn:
        conn.execute("INSERT INTO devices(id,subscription_id,request_id,name,state,expires,secret,credential_id,created) VALUES ('phone','sub','req','Phone','active',?,?,?,0)",(expires,c.encrypt(CONFIG),c.encrypt('device-key')))
    reconcile()
    calls=[]
    async def disable(sub):
        calls.append(sub['server_id'])
        if sub['server_id']=='node-a':raise TimeoutError()
    monkeypatch.setattr(providers,'disable',disable)
    with db() as conn:conn.execute("UPDATE devices SET state=? WHERE id='phone'",(device_state,))
    reconcile()
    assert 'node-b' in calls
    assert one("SELECT state FROM wg_fleet_placements WHERE device_id='phone' AND server_id='node-b'")['state']=='revoked'


def test_primary_rotation_cuts_old_backup_while_source_revoke_retries(client,monkeypatch):
    node('node-a');node('node-b')
    entitled(client)
    fake_provider(monkeypatch)
    reconcile()
    calls=[]
    async def disable(sub):
        calls.append(sub['server_id'])
        if sub['server_id']=='node-a':raise TimeoutError()
    monkeypatch.setattr(providers,'disable',disable)
    with db() as conn:conn.execute("UPDATE subscriptions SET primary_key_state='rotating' WHERE id='sub'")
    reconcile()
    assert 'node-b' in calls


def test_failed_paid_renewal_does_not_deadlock_node_migration(client,monkeypatch):
    node('node-a');node('node-b')
    expires=entitled(client)
    fake_provider(monkeypatch)
    reconcile()
    with db() as conn:
        conn.execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,status,created,target_expires) VALUES ('renew',1,'wg','month',30,199,'paid',?,?)",(int(time.time()),expires+30*86400))
    assert command(client,'drain',{'id':'node-a','version':1,'deadline_hours':24}).status_code==200
    reconcile()
    assert one("SELECT server_id FROM subscriptions WHERE id='sub'")['server_id']=='node-b'
    assert one("SELECT target_expires FROM orders WHERE id='renew'")['target_expires']==expires+30*86400
    asyncio.run(service.fulfill('renew'))
    assert one("SELECT state,expires FROM subscriptions WHERE id='sub'")['expires']==expires+30*86400
