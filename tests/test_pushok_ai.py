"""Local advisory engine is configurable, bounded and never executes business operations."""
import time
import uuid

import pytest
from fastapi import HTTPException
from app import pushok_ai as ai, providers, service, shop_finance
from app.db import db, execute, one, init_db
from test_shop import client, admin_headers, add_customer, add_admin_order

BASE='/api/admin/pushok-ai'
OWNER={'role':'owner','id':None}


def status(client):
    r=client.get(BASE,headers=admin_headers())
    assert r.status_code==200,r.text
    return r.json()


def policy(client,**changes):
    data=status(client)['policy'];data.pop('updated')
    r=client.put(BASE+'/policy',json=data|changes,headers=admin_headers())
    assert r.status_code==200,r.text
    return r.json()


def findings(client):
    return {f['code']:f for f in status(client)['findings']}


def decision(client,code,version,action='acknowledge',reason='investigate',operation_id=None):
    return client.post(BASE+'/findings/'+code+'/review',headers=admin_headers(),
        json={'version':version,'action':action,'reason':reason,'operation_id':operation_id or str(uuid.uuid4())})


def synthetic(code='test_fixture',category='servers',**evidence):
    return ai.finding(code,category,'warning','Тестовая рекомендация','Проверить данные',evidence)


def test_default_off_requires_explicit_policy_and_permissions(client):
    data=status(client)
    assert data['name']=='Pushok ИИ' and data['engine']=='local_rules_v1'
    assert data['external_model_connected'] is False and data['executes_business_actions'] is False
    assert data['policy']['enabled'] is False and data['policy']['automatic'] is False
    assert data['findings']==[] and data['run']['scan_count']==0
    assert client.get(BASE).status_code==401
    assert client.post(BASE+'/scan',headers=admin_headers()).status_code==409
    assert ai.scan(service.worker_health,automatic=True) is False
    assert one('SELECT count(*) AS n FROM shop_ai_state')['n']==0
    assert one('SELECT count(*) AS n FROM shop_ai_policy')['n']==0


@pytest.mark.parametrize('changes',[
    {'automatic':True},{'enabled':True,'diagnostics_enabled':False,'servers_enabled':False},
    {'interval_minutes':4},{'capacity_percent':101},{'sales_days':91},{'enabled':'true'},
    {'min_orders':0},{'queue_age_minutes':False},
])
def test_invalid_policy_fails_without_writes(client,changes):
    p=status(client)['policy'];p.pop('updated')
    assert client.put(BASE+'/policy',headers=admin_headers(),json=p|changes).status_code==422
    assert one('SELECT count(*) AS n FROM shop_ai_policy')['n']==0


def test_real_local_rules_read_aggregates_and_never_modify_business_data(client,monkeypatch):
    now=int(time.time())
    uid=add_customer('private-fixture@example.com')
    for i in range(6):
        add_admin_order(uid,'private-order-'+str(i),status='pending',created=now-2*86400)
    add_admin_order(uid,'private-paid-order',status='paid',created=now-7200)
    execute("UPDATE orders SET error='private-provider-token' WHERE id='private-paid-order'")
    execute("UPDATE servers SET capacity=1 WHERE id='test-wg'")
    execute("""INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id,secret)
        VALUES ('private-subscription',?,'wg',?,'active','test-wg',?)""",(uid,now+3600,'private-fixture-cipher'))
    execute("""INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id)
        VALUES ('expired-subscription',?,'xui',?,'active','test-xui')""",(uid,now-3600))
    shop_finance.record(shop_finance.Entry(operation_id=uuid.uuid4(),kind='expense',category='server',
        currency='XTR',amount_minor=25,occurred_at=now-10,reference='private-expense-reference',
        note='private expense note',verified=True),OWNER)
    policy(client,enabled=True,commerce_enabled=True,finance_enabled=True,min_orders=5)
    async def forbid(*args,**kwargs):raise AssertionError('No external operations allowed')
    monkeypatch.setattr(providers,'probe_server',forbid)
    monkeypatch.setattr(providers,'provision',forbid)
    monkeypatch.setattr(providers,'disable',forbid)
    def snapshot():
        with db() as conn:
            return {table:[tuple(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('users','orders','servers','subscriptions','plans','shop_finance_entries','shop_finance_actions','shop_payment_policy')}
    before=snapshot()
    result=client.post(BASE+'/scan',headers=admin_headers())
    assert result.status_code==200,result.text
    rows={f['code']:f for f in result.json()['findings']}
    for code in ('queue_issuance','expiry_delay','routing_capacity_wg','routing_unavailable_wg','monitor_paused','payment_conversion','finance_negative_xtr'):
        assert code in rows,code
    assert rows['expiry_delay']['severity']=='critical'
    assert rows['payment_conversion']['evidence']['orders']==6
    assert rows['finance_negative_xtr']['evidence']['net_recorded']==-25
    assert rows['finance_negative_xtr']['evidence']['currency']=='XTR'
    for private in ('private-fixture@example.com','private-provider-token','private-paid-order',
                    'private-subscription','private-fixture-cipher','private-expense-reference','private expense note'):
        assert private not in result.text
    assert snapshot()==before
    assert result.headers['cache-control']=='no-store'
    assert client.post(BASE+'/scan',headers=admin_headers()).status_code==429
    assert status(client)['run']['scan_count']==1


def test_review_is_idempotent_versioned_and_not_execution(client,monkeypatch):
    policy(client,enabled=True)
    monkeypatch.setattr(ai,'collect',lambda *args:[synthetic(value=1)])
    now=int(time.time())
    assert ai.scan({},now=now)
    row=findings(client)['test_fixture']
    op=str(uuid.uuid4())
    first=decision(client,row['code'],row['version'],operation_id=op)
    assert first.status_code==200 and first.json()['state']=='acknowledged'
    assert decision(client,row['code'],row['version'],operation_id=op).json()==first.json()
    assert decision(client,row['code'],row['version'],action='dismiss',reason='later',operation_id=op).status_code==409
    assert decision(client,row['code'],row['version'],action='dismiss',reason='later').status_code==409
    assert decision(client,row['code'],2,action='dismiss',reason='investigate').status_code==422
    dismissed=decision(client,row['code'],2,action='dismiss',reason='later')
    assert dismissed.status_code==200 and dismissed.json()['state']=='dismissed'
    reopened=decision(client,row['code'],3,action='reopen',reason='revisit')
    assert reopened.status_code==200 and reopened.json()['state']=='open'
    h=client.get(BASE+'/history',headers=admin_headers()).json()
    assert [x['action'] for x in h['events']]==['reopen','dismiss','acknowledge','detected']
    assert h['total']==4
    assert one('SELECT count(*) AS n FROM orders')['n']==0
    assert one('SELECT count(*) AS n FROM subscriptions')['n']==0
    assert one('SELECT count(*) AS n FROM server_routing_nodes')['n']==0


def test_repeated_conditions_resolve_and_reopen_without_duplicate_findings(client,monkeypatch):
    policy(client,enabled=True)
    values=[synthetic(value=1)]
    monkeypatch.setattr(ai,'collect',lambda *args:values[:])
    now=int(time.time())
    assert ai.scan({},now=now)
    original=findings(client)['test_fixture']
    assert ai.scan({},now=now+31)
    repeated=findings(client)['test_fixture']
    assert repeated['version']==original['version'] and repeated['occurrences']==1
    assert one('SELECT count(*) AS n FROM shop_ai_events')['n']==1
    values[:]=[synthetic(value=2)]
    assert ai.scan({},now=now+62)
    changed=findings(client)['test_fixture']
    assert changed['version']==original['version']+1
    assert decision(client,'test_fixture',original['version']).status_code==409
    values.clear()
    assert ai.scan({},now=now+93)
    assert findings(client)['test_fixture']['state']=='resolved'
    values[:]=[synthetic(value=3)]
    assert ai.scan({},now=now+124)
    recurrence=findings(client)['test_fixture']
    assert recurrence['state']=='open' and recurrence['occurrences']==2
    assert one('SELECT count(*) AS n FROM shop_ai_findings')['n']==1
    assert [e['action'] for e in ai.history(OWNER)['events']]==['recurrence','resolved','detected']


def test_paused_category_does_not_claim_resolution_and_failed_analysis_preserves_data(client,monkeypatch):
    policy(client,enabled=True)
    monkeypatch.setattr(ai,'collect',lambda *args:[synthetic(value=1)])
    now=int(time.time())
    assert ai.scan({},now=now)
    policy(client,servers_enabled=False)
    monkeypatch.setattr(ai,'collect',lambda *args:[])
    assert ai.scan({},now=now+31)
    row=findings(client)['test_fixture']
    assert row['state']=='open' and row['active'] is False
    assert decision(client,'test_fixture',row['version']).status_code==409
    policy(client,servers_enabled=True)
    def fail(*args):raise RuntimeError('private-api-key')
    monkeypatch.setattr(ai,'collect',fail)
    with pytest.raises(HTTPException) as e:ai.scan({},now=now+62)
    assert e.value.status_code==503 and 'private-api-key' not in e.value.detail
    assert status(client)['run']['scan_count']==2
    assert findings(client)['test_fixture']['state']=='open'


def test_automatic_interval_and_policy_change_during_scan(client,monkeypatch):
    p=policy(client,enabled=True,automatic=True,interval_minutes=5)
    monkeypatch.setattr(ai,'collect',lambda *args:[])
    now=int(time.time())
    assert ai.scan({},automatic=True,now=now)
    assert not ai.scan({},automatic=True,now=now+299)
    assert not ai.scan({},automatic=True,now=now-1)
    assert ai.scan({},automatic=True,now=now+300)
    assert status(client)['run']['source']=='automatic'
    def changed(*args):
        ai.update_policy(ai.PolicyUpdate(**({k:v for k,v in p.items() if k!='updated'}|{'automatic':False})))
        return [synthetic(value=1)]
    monkeypatch.setattr(ai,'collect',changed)
    assert not ai.scan({},automatic=True,now=now+600)
    assert status(client)['run']['scan_count']==2
    assert findings(client)=={}
    init_db()
    assert status(client)['policy']['automatic'] is False


def test_ai_rights_filter_finance_and_prevent_moderator_or_unscoped_review(client,monkeypatch):
    from test_staff_roles import bootstrap,add_role,login,use,ROOT
    policy(client,enabled=True,finance_enabled=True)
    monkeypatch.setattr(ai,'collect',lambda *args:[synthetic('server_fixture','servers',value=1),synthetic('finance_fixture','finance',value=2)])
    assert ai.scan({},now=int(time.time()))
    dev=bootstrap(client);add_role(client,'administrator');add_role(client,'moderator')
    adm=login(client,'administrator')
    assert client.get(BASE).status_code==403
    use(client,dev)
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['ai_review']}).status_code==422
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['ai_read','ai_review','servers_read']}).status_code==200
    use(client,adm)
    result=client.get(BASE).json()
    assert result['categories']==['servers']
    assert [f['code'] for f in result['findings']]==['server_fixture']
    assert [e['code'] for e in client.get(BASE+'/history').json()['events']]==['server_fixture']
    assert client.post(BASE+'/scan').status_code==403
    assert client.put(BASE+'/policy',json={}).status_code==403
    body={'version':1,'operation_id':str(uuid.uuid4()),'action':'acknowledge','reason':'investigate'}
    assert client.post(BASE+'/findings/finance_fixture/review',json=body).status_code==404
    assert client.post(BASE+'/findings/server_fixture/review',json=body).status_code==200
    login(client,'moderator')
    assert client.get(BASE).status_code==403
    assert client.get(BASE+'/history').status_code==403
