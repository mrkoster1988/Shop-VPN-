"""Проверки распределения без переноса действующих ключей и сетевых вызовов."""
import asyncio
import json
import time

import pytest

from app import config as c, providers, servers, server_routing as routing, service, flow_auto_provision
from app.db import db, execute, one, init_db
from test_shop import client, register, order, confirm, admin_headers, add_customer

PATH='/api/admin/server-routing'


def snapshot(client):
    r=client.get(PATH,headers=admin_headers())
    assert r.status_code==200,r.text
    return r.json()


def policy(client,**changes):
    data=snapshot(client)['policy']
    data.pop('updated')
    r=client.put(PATH+'/policy',json=data|changes,headers=admin_headers())
    assert r.status_code==200,r.text
    return r.json()


def node(client,sid,**changes):
    row=next(n for n in snapshot(client)['nodes'] if n['id']==sid)
    data={'version':row['routing_version'],'server_version':row['version'],
          'priority':row['priority'],'reserve':row['reserve'],'accept_new':row['accept_new']}|changes
    return client.put(PATH+'/nodes/'+sid,json=data,headers=admin_headers())


def make_node(sid,capacity=10,protocol='wg',state='active',health='ok',dashboard=False):
    if protocol=='wg':
        profile={'url':'https://private-panel.example/private','token':'fixture-token-'+'a'*40}
        if dashboard:profile|={'api':'wgdashboard','configuration':'wg0','schedule_expiry':True,'panel_timezone':'UTC'}
    else:
        profile={'url':'https://private-panel.example','username':'test','password':'fixture-password',
                 'inbound_id':1,'link_template':'vless://{uuid}@vpn.example'}
    execute("""INSERT INTO servers(id,name,protocol,state,health,capacity,settings,created)
        VALUES (?,?,?,?,?,?,?,?)""",(sid,sid,protocol,state,health,capacity,c.encrypt(json.dumps(profile)),1))


def fill(sid,count,protocol='wg'):
    for i in range(count):
        uid=add_customer(sid+'-'+str(i)+'@example.com')
        execute("""INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id)
            VALUES (?,?,?,?,'active',?)""",(sid+'-'+str(i),uid,protocol,int(time.time())+3600,sid))


def selected(proto='wg'):
    with db() as conn:
        candidate=servers.select(conn,proto)
    return candidate['id'] if candidate else None


def reset_wg():
    execute("DELETE FROM servers WHERE protocol='wg'")


def test_compatible_defaults_read_only_and_private_report(client):
    assert client.get(PATH).status_code==401
    data=snapshot(client)
    assert data['policy']==routing.DEFAULT
    assert selected()=='test-wg'
    assert data['next_assignment']['wg']=='test-wg'
    assert data['traffic_verified'] is False
    assert 'settings' not in data['nodes'][0]
    assert 'token' not in str(data) and 'agent.example' not in str(data)
    assert one('SELECT count(*) AS n FROM server_routing_policy')['n']==0
    assert one('SELECT count(*) AS n FROM server_routing_nodes')['n']==0


def test_priority_reserve_pause_capacity_and_freshness(client):
    reset_wg()
    make_node('primary',capacity=1);make_node('second',capacity=2);make_node('backup',capacity=2)
    assert node(client,'primary',priority=200).status_code==200
    assert node(client,'second',priority=100).status_code==200
    assert node(client,'backup',priority=1000,reserve=True).status_code==200
    assert selected()=='primary'
    fill('primary',1)
    assert selected()=='second'
    assert node(client,'second',accept_new=False).status_code==200
    assert selected()=='backup'
    policy(client,reserve_enabled=False)
    assert selected() is None
    policy(client,reserve_enabled=True,require_recent=True,max_age_minutes=10)
    assert selected() is None
    execute('UPDATE servers SET checked_at=? WHERE id=?',(int(time.time()),'backup'))
    assert selected()=='backup'
    execute("UPDATE servers SET health='error' WHERE id='backup'")
    assert selected() is None
    reasons={r['id']:r['blocked_reason'] for r in snapshot(client)['nodes']}
    assert reasons['primary']=='capacity' and reasons['second']=='new_assignments_paused'
    assert reasons['backup']=='api_error'


def test_utilization_strategy_and_unknown_checks_compatibility(client):
    reset_wg();make_node('small',capacity=2);make_node('large',capacity=10);make_node('unlimited',capacity=0,state='draining')
    fill('small',1);fill('large',2)
    assert selected()=='small'
    policy(client,strategy='utilization')
    assert selected()=='large'
    execute("UPDATE servers SET state='active' WHERE id='unlimited'")
    assert selected()=='large'
    assert node(client,'unlimited',priority=300).status_code==200
    assert selected()=='unlimited'
    init_db()
    assert snapshot(client)['policy']['strategy']=='utilization'
    assert selected()=='unlimited'


def test_version_conflicts_and_invalid_policy_are_rejected(client):
    before=snapshot(client)['policy']
    body={k:v for k,v in before.items() if k!='updated'}
    assert client.put(PATH+'/policy',headers=admin_headers(),json=body|{'monitor_batch':5}).status_code==422
    assert client.put(PATH+'/policy',headers=admin_headers(),json=body|{'require_recent':'true'}).status_code==422
    policy(client,monitor_enabled=True)
    assert client.put(PATH+'/policy',headers=admin_headers(),json=body).status_code==409
    assert node(client,'test-wg',priority=300).status_code==200
    assert node(client,'test-wg',version=0,priority=400).status_code==409
    assert node(client,'test-wg',server_version=999,priority=400).status_code==409
    assert node(client,'test-wg',priority=-1).status_code==422


def test_paid_renewal_remains_pinned_after_assignment_policy_change(client,monkeypatch):
    reset_wg();make_node('primary');make_node('backup')
    assert node(client,'primary',priority=500).status_code==200
    assert node(client,'backup',reserve=True).status_code==200
    monkeypatch.setattr(providers,'available',servers.available)
    calls=[]
    async def provision(sub):
        calls.append(sub['server_id']);return '[Interface]\n[Peer]\n'
    monkeypatch.setattr(providers,'provision',provision)
    register(client)
    first=order(client,'wg')
    assert confirm(client,first['id']).status_code==200
    asyncio.run(service.fulfill(first['id']))
    sub=one('SELECT id,server_id,expires FROM subscriptions')
    assert sub['server_id']=='primary'
    assert node(client,'primary',accept_new=False).status_code==200
    assert selected()=='backup'
    renewal=order(client,'wg');assert confirm(client,renewal['id']).status_code==200
    asyncio.run(service.fulfill(renewal['id']))
    assert calls==['primary','primary']
    assert one('SELECT server_id,expires FROM subscriptions')=={'server_id':'primary','expires':sub['expires']+30*86400}


def test_starter_uses_routing_but_only_expiring_wgdashboard(client):
    reset_wg();make_node('plain');make_node('protected',dashboard=True);make_node('protected-reserve',dashboard=True)
    assert node(client,'plain',priority=1000).status_code==200
    assert node(client,'protected-reserve',priority=1000,reserve=True).status_code==200
    assert selected()=='plain'
    with db() as conn:assert flow_auto_provision._node(conn)['id']=='protected'
    assert node(client,'protected',accept_new=False).status_code==200
    with db() as conn:assert flow_auto_provision._node(conn)['id']=='protected-reserve'
    policy(client,reserve_enabled=False)
    with db() as conn:assert flow_auto_provision._node(conn) is None


def test_monitor_off_bounded_due_checks_recovery_and_history(client,monkeypatch):
    execute("UPDATE servers SET state='disabled' WHERE protocol='xui'")
    reset_wg();make_node('active',health='unknown');make_node('draining',state='draining');make_node('disabled',state='disabled')
    calls=[]
    async def bad(row):
        calls.append(row['id'])
        if row['id']=='active':raise RuntimeError('fixture-private-panel-token')
    monkeypatch.setattr(providers,'probe_server',bad)
    assert asyncio.run(routing.run_due())==0
    assert calls==[]
    policy(client,monitor_enabled=True,monitor_interval_minutes=5,monitor_batch=1)
    assert asyncio.run(routing.run_due())==1 and calls==['active']
    assert one("SELECT health,state FROM servers WHERE id='active'")=={'health':'error','state':'active'}
    assert asyncio.run(routing.run_due())==1 and calls==['active','draining']
    assert asyncio.run(routing.run_due())==0
    history=client.get(PATH+'/history/active',headers=admin_headers())
    assert history.status_code==200 and history.json()['checks'][0]['status']=='error'
    assert history.json()['checks'][0]['source']=='automatic'
    assert 'fixture-private-panel-token' not in history.text
    async def recovered(row):calls.append(row['id'])
    monkeypatch.setattr(providers,'probe_server',recovered)
    result=client.post('/api/admin/servers/active/check',headers=admin_headers())
    assert result.status_code==200 and result.json()['health']=='ok'
    assert selected()=='active'
    checks=client.get(PATH+'/history/active',headers=admin_headers()).json()['checks']
    assert [r['status'] for r in checks]==['ok','error']
    assert checks[0]['source']=='manual'


def test_stale_probe_and_disabled_policy_cannot_write_new_health(client,monkeypatch):
    execute("UPDATE servers SET state='disabled' WHERE protocol='xui'")
    execute("UPDATE servers SET health='unknown',checked_at=NULL WHERE id='test-wg'")
    p=policy(client,monitor_enabled=True,monitor_batch=1)
    async def changed(row):
        execute('UPDATE servers SET version=version+1 WHERE id=?',(row['id'],))
    monkeypatch.setattr(providers,'probe_server',changed)
    assert asyncio.run(routing.run_due())==0
    assert one("SELECT health FROM servers WHERE id='test-wg'")['health']=='unknown'
    assert one('SELECT count(*) AS n FROM server_probe_history')['n']==0
    async def stopped(row):
        routing.update_policy(routing.PolicyUpdate(**{k:v for k,v in p.items() if k!='updated'}|{'monitor_enabled':False}))
    monkeypatch.setattr(providers,'probe_server',stopped)
    assert asyncio.run(routing.run_due())==0
    assert one('SELECT count(*) AS n FROM server_probe_history')['n']==0


def test_readiness_diagnostics_respects_routing_policy(client):
    assert node(client,'test-wg',accept_new=False).status_code==200
    diag=client.get('/api/admin/diagnostics',headers=admin_headers())
    assert diag.status_code==200,diag.text
    assert diag.json()['servers']['wg']['assignable']==0
    assert 'no_new_assignments_wg' in diag.json()['warnings']


def test_delegated_routing_permissions_are_explicit(client):
    from test_staff_roles import bootstrap,add_role,login,use,ROOT
    dev=bootstrap(client);add_role(client,'administrator');add_role(client,'moderator')
    admin=login(client,'administrator')
    assert client.get(PATH).status_code==200
    payload={k:v for k,v in client.get(PATH).json()['policy'].items() if k!='updated'}
    assert client.put(PATH+'/policy',json=payload).status_code==403
    use(client,dev)
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['server_routing']}).status_code==422
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['servers_read','server_routing']}).status_code==200
    use(client,admin)
    assert client.put(PATH+'/policy',json=payload|{'require_recent':True}).status_code==200
    assert client.patch('/api/admin/servers/test-wg/credentials',json={'version':1,'token':'z'*40}).status_code==403
    login(client,'moderator')
    assert client.get(PATH).status_code==403
    assert client.get(PATH+'/history/test-wg').status_code==403
