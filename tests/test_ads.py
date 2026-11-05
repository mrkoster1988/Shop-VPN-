"""Проверки приёма рекламных заявок, модерации и изоляции от VPN/оплаты."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.db import one,all_rows,execute,init_db
from test_shop import client,register,admin_headers

def policy(client,**changes):
    current=client.get('/api/admin/ads/policy',headers=admin_headers()).json()
    body={k:current[k] for k in ('accepting','amount','hours','instructions','version')};body.update(changes)
    return client.put('/api/admin/ads/policy',json=body,headers=admin_headers())

def submit(client,**changes):
    body={'request_id':str(uuid.uuid4()),'policy_version':1,'channel':'@channel_name','comment':'Предложение',**changes}
    return client.post('/api/ads/requests',json=body)

def decision(client,row,**changes):
    return client.put('/api/admin/ads/requests/'+row['id'],json={'version':row['version'],'state':'approved','reason':'',**changes},headers=admin_headers())

def test_default_policy_read_does_not_write_and_intake_is_closed(client):
    p=client.get('/api/ads/policy').json()
    assert (p['accepting'],p['amount'],p['hours'],p['version'])==(False,300,24,0)
    assert not one('SELECT * FROM ads_policy') and not one('SELECT * FROM ads_events')
    register(client);assert submit(client,policy_version=0).status_code==409

def test_policy_changes_are_versioned_audited_and_require_admin(client):
    assert client.put('/api/admin/ads/policy',json={}).status_code==403
    first=policy(client,accepting=True,amount=500,hours=48);assert first.status_code==200
    p=first.json();assert p['version']==1 and p['amount']==500
    payload={k:p[k] for k in ('accepting','amount','hours','instructions','version')};payload['version']=0
    assert client.put('/api/admin/ads/policy',json=payload,headers=admin_headers()).status_code==409
    assert len(all_rows('SELECT * FROM ads_events'))==1

@pytest.mark.parametrize('changes',[{'amount':0},{'amount':True},{'hours':721},{'hours':0},{'accepting':1},{'version':-1},{'extra':'private-secret'}])
def test_invalid_policy_cannot_change_data_or_reflect_input(client,changes):
    p=client.get('/api/ads/policy').json();body={k:p[k] for k in ('accepting','amount','hours','instructions','version')};body.update(changes)
    response=client.put('/api/admin/ads/policy',json=body,headers=admin_headers())
    assert response.status_code==422 and 'private-secret' not in response.text
    assert not one('SELECT * FROM ads_policy')

def test_normalized_channel_snapshot_and_duplicate_request_survive_policy_change(client):
    register(client);assert policy(client,accepting=True).status_code==200
    key=str(uuid.uuid4());created=submit(client,request_id=key,channel='https://t.me/Channel_Name').json()
    assert created['channel']=='channel_name' and created['state']=='pending' and created['amount']==300
    assert policy(client,amount=700,accepting=False).status_code==200
    again=submit(client,request_id=key,channel='@channel_name')
    assert again.status_code==200 and again.json()==created
    assert submit(client,request_id=key,comment='Другое').status_code==409
    assert one('SELECT count(*) AS n FROM ads_requests')['n']==1
    assert not one('SELECT * FROM orders') and not one('SELECT * FROM subscriptions')
    assert client.get('/api/ads/requests').json()['requests'][0]==created

@pytest.mark.parametrize('channel',['https://evil.example/channel_name','https://t.me/+private','@bad','@bad-name','@channel_name?x=1','<script>private-secret</script>'])
def test_invalid_channel_refuses_without_echo(client,channel):
    register(client);policy(client,accepting=True)
    response=submit(client,channel=channel)
    assert response.status_code==422 and 'private-secret' not in response.text
    assert not one('SELECT * FROM ads_requests')

def test_customer_and_admin_boundaries_and_csrf(client):
    assert client.get('/api/ads/requests').status_code==401
    register(client);policy(client,accepting=True);row=submit(client).json()
    assert client.get('/api/admin/ads/requests').status_code==403
    assert client.post('/api/ads/requests',json={'request_id':str(uuid.uuid4()),'policy_version':1,'channel':'@other_channel'},headers={'Origin':'https://evil.example'}).status_code==403
    client.post('/api/auth/logout');register(client,'other@example.com')
    assert client.get('/api/ads/requests/'+row['id']).status_code==404
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':1}).status_code==404
    assert client.get('/api/ads/requests').json()['requests']==[]

def test_moderation_conflicts_rejection_reason_and_cancellation(client):
    register(client);policy(client,accepting=True);row=submit(client).json()
    assert decision(client,row,state='rejected').status_code==422
    approved=decision(client,row).json();assert approved['state']=='approved' and approved['version']==2
    assert decision(client,row,state='rejected',reason='Другое решение').status_code==409
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':1}).status_code==409
    canceled=client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':2})
    assert canceled.status_code==200 and canceled.json()['state']=='canceled'
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':3}).status_code==409
    assert not one('SELECT * FROM orders') and not one('SELECT * FROM subscriptions')
    assert [r['action'] for r in all_rows('SELECT action FROM ads_events WHERE request_id=? ORDER BY id',(row['id'],))]==['created','approved','canceled']

def test_declined_channel_can_be_resubmitted_and_admin_filter_paginates(client):
    register(client);policy(client,accepting=True);row=submit(client).json()
    rejected=decision(client,row,state='rejected',reason='Уточните содержание');assert rejected.status_code==200
    assert submit(client).status_code==200
    result=client.get('/api/admin/ads/requests?state=rejected&page=1',headers=admin_headers()).json()
    assert result['total']==1 and result['requests'][0]['reason']=='Уточните содержание'
    assert 'user_id' in result['requests'][0]
    assert client.get('/api/ads/requests?page=2').json()['requests']==[]

def test_open_channel_and_customer_limits_are_enforced(client):
    register(client);policy(client,accepting=True);assert submit(client).status_code==200
    assert submit(client).status_code==409
    for i in range(4):assert submit(client,channel='@other_channel'+str(i)).status_code==200
    assert submit(client,channel='@last_channel').status_code==409

def test_simultaneous_same_intent_creates_one_record(client):
    register(client);policy(client,accepting=True);key=str(uuid.uuid4())
    with ThreadPoolExecutor() as pool:responses=list(pool.map(lambda _:submit(client,request_id=key),range(2)))
    assert all(r.status_code==200 for r in responses) and responses[0].json()['id']==responses[1].json()['id']
    assert one('SELECT count(*) AS n FROM ads_requests')['n']==1

def test_stale_policy_and_forged_price_do_not_create_request(client):
    register(client);policy(client,accepting=True)
    assert submit(client,policy_version=0).status_code==409
    assert submit(client,amount=1).status_code==422
    assert not one('SELECT * FROM ads_requests')

def test_repeated_migration_and_real_backup_preserve_advertising_records(client,tmp_path):
    from app import config as c
    from test_backup import b
    import os
    register(client);policy(client,accepting=True);row=submit(client).json();decision(client,row,state='rejected',reason='Проверено')
    before=all_rows('SELECT * FROM ads_requests');events=all_rows('SELECT * FROM ads_events')
    init_db();assert all_rows('SELECT * FROM ads_requests')==before and all_rows('SELECT * FROM ads_events')==events
    archive=tmp_path/'ads.pvb';b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'long-backup-password')
    b.restore(archive,'long-backup-password',tmp_path/'restored')
    import sqlite3
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        assert conn.execute('SELECT reason FROM ads_requests').fetchone()[0]=='Проверено'
        assert conn.execute('SELECT count(*) FROM ads_events').fetchone()[0]==len(events)
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0


def test_staging_digest_ignores_only_empty_new_tables(client):
    import importlib.util
    from pathlib import Path
    from app import config as c
    spec=importlib.util.spec_from_file_location('ads_staging_data',Path(__file__).with_name('staging_data.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    empty=module.snapshot(c.DB)
    from app.db import db
    with db() as conn:
        conn.execute('DROP TABLE ads_events');conn.execute('DROP TABLE ads_requests');conn.execute('DROP TABLE ads_policy')
    old=module.snapshot(c.DB)
    assert old['digest']==empty['digest'] and 'ads_requests' not in old['tables']
    init_db();register(client);policy(client,accepting=True);submit(client)
    populated=module.snapshot(c.DB)
    assert populated['digest']!=empty['digest'] and populated['counts']['ads_requests']==1
