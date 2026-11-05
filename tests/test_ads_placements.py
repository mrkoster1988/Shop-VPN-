"""Площадки, ручные подтверждения, паузы и исключительное занятие размещения."""
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.db import db,one,all_rows,init_db
from app import ads_placements as placements
from test_shop import client,register,admin_headers
from test_ads import policy,submit,decision
from test_ads_payments import record


def site(client,**changes):
    sid=changes.pop('id',str(uuid.uuid4()))
    return client.put('/api/admin/ads/sites/'+sid,json={'version':0,'name':'Площадка','enabled':True,'verified':True,'note':'private-site-check',**changes},headers=admin_headers())


def paid(client):
    register(client);policy(client,accepting=True,hours=1)
    row=decision(client,submit(client).json()).json();assert record(client,row).status_code==200
    return row


def change(client,row,action='start',**changes):
    body={'operation_id':str(uuid.uuid4()),'request_version':row['version'],'payment_version':1,'placement_version':0,
          'note':'private-placement-check','checked':True,**changes}
    return client.post('/api/admin/ads/requests/'+row['id']+'/placement/'+action,json=body,headers=admin_headers())


def launch(client,row,s,**changes):
    return change(client,row,site_id=s['id'],site_version=s['version'],**changes)


def test_time_starts_on_confirmation_pause_retains_time_and_finish_frees_site(client,monkeypatch):
    row=paid(client);s=site(client).json();clock=[10000];monkeypatch.setattr(placements.time,'time',lambda:clock[0])
    started=launch(client,row,s).json();assert started['remaining_seconds']==3600 and started['expected_stop_at']==13600
    clock[0]+=300;paused=change(client,row,'pause',placement_version=1).json()
    assert paused['remaining_seconds']==3300 and paused['expected_stop_at'] is None
    clock[0]+=900;assert client.get('/api/ads/requests/'+row['id']).json()['placement']['remaining_seconds']==3300
    resumed=change(client,row,'resume',placement_version=2).json();assert resumed['expected_stop_at']==14500
    clock[0]+=3301;due=client.get('/api/ads/requests/'+row['id']).json()['placement']
    assert due['state']=='active' and due['attention_due'] and due['remaining_seconds']==0
    assert client.get('/api/admin/ads/sites',headers=admin_headers()).json()['sites'][0]['occupied']
    finished=change(client,row,'finish',placement_version=3).json();assert finished['state']=='finished' and finished['used_seconds']==3600
    assert not client.get('/api/admin/ads/sites',headers=admin_headers()).json()['sites'][0]['occupied']


def test_early_finish_preserves_unserved_seconds_and_allows_new_same_channel(client,monkeypatch):
    row=paid(client);s=site(client).json();clock=[10000];monkeypatch.setattr(placements.time,'time',lambda:clock[0])
    launch(client,row,s);clock[0]+=120
    finished=change(client,row,'finish',placement_version=1).json()
    assert finished['remaining_seconds']==3480 and finished['used_seconds']==120
    assert submit(client).status_code==200
    assert launch(client,row,s,placement_version=2).status_code==409


def test_paused_site_cannot_be_sold_to_another_request(client):
    row=paid(client);s=site(client).json();launch(client,row,s);change(client,row,'pause',placement_version=1)
    other=decision(client,submit(client,channel='@other_channel').json()).json();record(client,other)
    assert launch(client,other,s).status_code==409
    change(client,row,'finish',placement_version=2)
    assert launch(client,other,s).status_code==200


@pytest.mark.parametrize('state',['pending','rejected','canceled','unpaid','refunded'])
def test_start_requires_moderation_and_current_payment(client,state):
    register(client);policy(client,accepting=True);row=submit(client).json();s=site(client).json()
    if state=='rejected':row=decision(client,row,state='rejected',reason='Не подходит').json()
    elif state=='canceled':row=client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':1}).json()
    elif state in ('unpaid','refunded'):
        row=decision(client,row).json()
        if state=='refunded':record(client,row);record(client,row,'refund')
    assert launch(client,row,s,payment_version=2 if state=='refunded' else 0).status_code==409


def test_site_verification_versions_and_resume_guards(client):
    row=paid(client);s=site(client,enabled=False,verified=False).json()
    assert launch(client,row,s).status_code==409
    enabled=site(client,id=s['id'],version=1).json()
    assert launch(client,row,s).status_code==409
    assert launch(client,row,enabled).status_code==200
    assert site(client,id=s['id'],version=2,verified=False).status_code==409
    assert change(client,row,'pause',placement_version=1).status_code==200
    disabled=site(client,id=s['id'],version=2,enabled=False).json()
    assert disabled['version']==3
    assert change(client,row,'resume',placement_version=2).status_code==409
    assert change(client,row,'finish',placement_version=2).status_code==200


def test_refund_and_cancel_require_confirmed_stop(client):
    row=paid(client);s=site(client).json();launch(client,row,s)
    assert record(client,row,'refund').status_code==409
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':row['version']}).status_code==409
    change(client,row,'pause',placement_version=1);assert record(client,row,'refund').status_code==409
    change(client,row,'finish',placement_version=2);assert record(client,row,'refund').status_code==200
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':row['version']}).status_code==200


def test_idempotency_and_conflicting_versions(client):
    row=paid(client);s=site(client).json();key=str(uuid.uuid4())
    assert launch(client,row,s,operation_id=key).status_code==200
    assert launch(client,row,s,operation_id=key).status_code==200
    assert launch(client,row,s,operation_id=key,note='Другое').status_code==409
    assert change(client,row,'pause',placement_version=0).status_code==409
    assert change(client,row,'pause',placement_version=1,request_version=1).status_code==409
    assert change(client,row,'pause',placement_version=1,payment_version=0).status_code==409
    assert change(client,row,'pause',placement_version=1).status_code==200
    assert launch(client,row,s,operation_id=key).json()['state']=='paused'
    assert len(all_rows('SELECT * FROM ads_placement_actions'))==2


@pytest.mark.parametrize('changes',[{'checked':False},{'checked':1},{'note':' '},{'placement_version':True},{'extra':'private-secret'}])
def test_invalid_confirmation_never_writes_or_echoes(client,changes):
    row=paid(client);s=site(client).json();result=launch(client,row,s,**changes)
    assert result.status_code==422 and 'private-secret' not in result.text
    assert not one('SELECT * FROM ads_placements')


def test_private_site_notes_and_actions_are_not_customer_data(client):
    row=paid(client);s=site(client).json();launch(client,row,s)
    for path in ['/api/ads/requests','/api/ads/requests/'+row['id']]:
        response=client.get(path)
        assert 'private-site-check' not in response.text and 'private-placement-check' not in response.text and s['id'] not in response.text
    assert client.get('/api/admin/ads/sites').status_code==401
    assert client.put('/api/admin/ads/sites/'+s['id'],json={}).status_code==401
    assert client.post('/api/admin/ads/requests/'+row['id']+'/placement/pause',json={}).status_code==401
    assert client.post('/api/admin/ads/requests/'+row['id']+'/placement/pause',json={},headers={**admin_headers(),'Origin':'https://evil.example'}).status_code==403
    client.cookies.clear();register(client,'other@example.com')
    assert client.get('/api/ads/requests/'+row['id']).status_code==404


def test_concurrent_starts_reserve_one_site_and_retries_do_not_duplicate(client):
    row=paid(client);s=site(client).json();other=decision(client,submit(client,channel='@other_channel').json()).json();record(client,other)
    with ThreadPoolExecutor() as pool:responses=list(pool.map(lambda r:launch(client,r,s),[row,other]))
    assert sorted(r.status_code for r in responses)==[200,409]
    assert len(all_rows('SELECT * FROM ads_placements'))==1


def test_refund_start_race_cannot_create_refunded_active_placement(client):
    row=paid(client);s=site(client).json()
    with ThreadPoolExecutor() as pool:
        start=pool.submit(launch,client,row,s);refund=pool.submit(record,client,row,'refund')
        assert sorted([start.result().status_code,refund.result().status_code])==[200,409]
    current=client.get('/api/ads/requests/'+row['id']).json()
    assert not(current['payment']['status']=='refunded' and current['placement'])


def test_exclusive_site_is_also_enforced_by_database(client):
    row=paid(client);s=site(client).json();launch(client,row,s)
    other=decision(client,submit(client,channel='@other_channel').json()).json()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with db() as conn:conn.execute("INSERT INTO ads_placements SELECT ?,site_id,site_name,state,version,allocated_seconds,used_seconds,segment_started,started,finished,updated FROM ads_placements",(other['id'],))


def test_site_inputs_versions_and_empty_initial_state(client):
    assert client.get('/api/admin/ads/sites',headers=admin_headers()).json()=={'sites':[]}
    assert not one('SELECT * FROM ads_sites')
    for changes in [{'name':' '},{'verified':1},{'note':' '},{'version':True}]:assert site(client,**changes).status_code==422
    s=site(client).json();assert site(client,id=s['id']).status_code==409
    assert not one('SELECT * FROM ads_placements')


def test_pause_exhausted_time_cannot_resume_and_site_identity_is_fixed(client,monkeypatch):
    row=paid(client);s=site(client).json();clock=[10000];monkeypatch.setattr(placements.time,'time',lambda:clock[0]);launch(client,row,s)
    clock[0]+=3601;change(client,row,'pause',placement_version=1)
    assert change(client,row,'resume',placement_version=2).status_code==409
    assert change(client,row,'finish',placement_version=2,site_id=s['id'],site_version=1).status_code==422
    assert change(client,row,'finish',placement_version=2).status_code==200


def test_migration_backup_restores_occupied_site_and_remaining_time(client,tmp_path):
    from app import config as c
    from test_backup import b
    import os,sqlite3
    row=paid(client);s=site(client).json();launch(client,row,s);change(client,row,'pause',placement_version=1)
    before=all_rows('SELECT * FROM ads_placements');init_db();assert all_rows('SELECT * FROM ads_placements')==before
    archive=tmp_path/'placements.pvb';b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'long-backup-password');b.restore(archive,'long-backup-password',tmp_path/'restored')
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        conn.row_factory=sqlite3.Row
        assert [dict(r) for r in conn.execute('SELECT * FROM ads_placements')]==before
        assert conn.execute('SELECT count(*) FROM ads_placement_actions').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
    assert not one('SELECT * FROM orders') and not one('SELECT * FROM subscriptions')


def test_channel_uniqueness_remains_atomic_after_index_migration(client):
    register(client);policy(client,accepting=True)
    with ThreadPoolExecutor() as pool:results=list(pool.map(lambda _:submit(client),range(2)))
    assert sorted(r.status_code for r in results)==[200,409]


def test_terminal_action_replay_after_refund_is_safe(client):
    row=paid(client);s=site(client).json();key=str(uuid.uuid4());launch(client,row,s,operation_id=key)
    change(client,row,'finish',placement_version=1);record(client,row,'refund')
    assert launch(client,row,s,operation_id=key).json()['state']=='finished'
    assert len(all_rows('SELECT * FROM ads_placement_actions'))==2
