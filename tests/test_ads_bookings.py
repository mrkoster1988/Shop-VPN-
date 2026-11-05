"""Резервы периодов, пересечения, поздний запуск, паузы и восстановление."""
import uuid
import json
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from app import ads_bookings as bookings
from app.db import db,all_rows,init_db
from test_shop import client,register,admin_headers
from test_ads import policy,submit,decision
from test_ads_payments import record
from test_ads_placements import paid,site,launch,change

def setup(client,monkeypatch):
    row=paid(client);clock=[10000]
    monkeypatch.setattr(bookings.time,'time',lambda:clock[0])
    s=site(client).json()
    return row,s,clock

def another(client,index=1):
    row=decision(client,submit(client,channel='@booking_channel_'+str(index)).json()).json()
    assert record(client,row).status_code==200
    return row

def reserve(client,row,s,**changes):
    body={'operation_id':str(uuid.uuid4()),'request_version':row['version'],'payment_version':1,'booking_version':0,
          'site_id':s['id'],'site_version':s['version'],'starts_at':11000,'note':'private-booking-note',**changes}
    return client.post('/api/admin/ads/requests/'+row['id']+'/booking/reserve',json=body,headers=admin_headers())

def cancel(client,row,**changes):
    body={'operation_id':str(uuid.uuid4()),'request_version':row['version'],'payment_version':1,'booking_version':1,'note':'private-cancel-note',**changes}
    return client.post('/api/admin/ads/requests/'+row['id']+'/booking/cancel',json=body,headers=admin_headers())

def listing(client,**params):
    return client.get('/api/admin/ads/bookings',params=params,headers=admin_headers())

def test_empty_booking_list_does_not_seed_data(client):
    assert listing(client).json()['requests']==[]
    assert not all_rows('SELECT * FROM ads_bookings') and not all_rows('SELECT * FROM ads_booking_actions')

def test_snapshot_duration_privacy_and_read_only_listing(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);b=reserve(client,row,s).json()
    assert b['state']=='reserved' and b['ends_at']==14600 and b['version']==1
    policy(client,amount=700,hours=24)
    own=client.get('/api/ads/requests/'+row['id'])
    assert own.json()['booking']['ends_at']==14600
    assert 'private-booking-note' not in own.text and s['id'] not in own.text and 'actions' not in own.json()['booking']
    before=all_rows('SELECT * FROM ads_booking_actions')
    assert listing(client).json()['total']==1
    assert all_rows('SELECT * FROM ads_booking_actions')==before
    clock[0]=14600
    assert listing(client,view='expired').json()['total']==1
    assert client.get('/api/ads/requests/'+row['id']).json()['booking']['expired']

@pytest.mark.parametrize('starts',[10999,11000,11001,14599])
def test_overlapping_periods_are_rejected(client,monkeypatch,starts):
    row,s,_=setup(client,monkeypatch);assert reserve(client,row,s).status_code==200
    other=another(client);assert reserve(client,other,s,starts_at=starts).status_code==409

def test_adjacent_slots_different_sites_and_cancel_rebook_history(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    other=another(client);assert reserve(client,other,s,starts_at=14600).status_code==200
    third=another(client,2);other_site=site(client).json();assert reserve(client,third,other_site).status_code==200
    assert cancel(client,row).status_code==200
    assert reserve(client,row,s,booking_version=2,starts_at=20000).status_code==200
    row_book=client.get('/api/ads/requests/'+row['id']).json()['booking']
    assert row_book['version']==3 and row_book['starts_at']==20000
    details=[json.loads(r['details']) for r in all_rows('SELECT * FROM ads_booking_actions WHERE request_id=? ORDER BY rowid',(row['id'],))]
    assert [d['starts_at'] for d in details]==[11000,11000,20000]

@pytest.mark.parametrize('changes',[{'starts_at':True},{'starts_at':1.5},{'starts_at':0},{'booking_version':True},{'note':' '},{'amount':300},{'extra':'private-secret'}])
def test_invalid_booking_fields_never_write_or_echo(client,monkeypatch,changes):
    row,s,_=setup(client,monkeypatch);r=reserve(client,row,s,**changes)
    assert r.status_code==422 and 'private-secret' not in r.text
    assert not all_rows('SELECT * FROM ads_bookings')

@pytest.mark.parametrize('changes',[{'starts_at':10000},{'starts_at':10000+366*86400},{'request_version':1},{'payment_version':0},{'booking_version':1},{'site_version':99}])
def test_invalid_time_and_stale_versions_are_conflicts(client,monkeypatch,changes):
    row,s,_=setup(client,monkeypatch);assert reserve(client,row,s,**changes).status_code==409

@pytest.mark.parametrize('state',['pending','rejected','unpaid','refunded'])
def test_booking_requires_approval_and_payment(client,monkeypatch,state):
    register(client);policy(client,accepting=True,hours=1);row=submit(client).json()
    if state=='rejected':row=decision(client,row,state='rejected',reason='Не подходит').json()
    if state in ('unpaid','refunded'):
        row=decision(client,row).json()
        if state=='refunded':record(client,row);record(client,row,'refund')
    monkeypatch.setattr(bookings.time,'time',lambda:10000);s=site(client).json()
    assert reserve(client,row,s,payment_version=2 if state=='refunded' else 0).status_code==409

def test_booking_idempotency_survives_cancel_and_conflicting_payload_is_rejected(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);key=str(uuid.uuid4())
    assert reserve(client,row,s,operation_id=key).status_code==200
    assert reserve(client,row,s,operation_id=key).status_code==200
    assert reserve(client,row,s,operation_id=key,note='Другое').status_code==409
    assert cancel(client,row).status_code==200
    assert reserve(client,row,s,operation_id=key).json()['state']=='canceled'
    assert len(all_rows('SELECT * FROM ads_booking_actions'))==2

def test_start_checks_window_site_and_booking_version_and_consumes_once(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);reserve(client,row,s)
    assert launch(client,row,s,booking_version=1).status_code==409
    clock[0]=11000
    assert launch(client,row,s).status_code==409
    other_site=site(client).json()
    assert launch(client,row,other_site,booking_version=1).status_code==409
    key=str(uuid.uuid4())
    assert launch(client,row,s,booking_version=1,operation_id=key).status_code==200
    assert launch(client,row,s,booking_version=1,operation_id=key).status_code==200
    assert client.get('/api/ads/requests/'+row['id']).json()['booking']['state']=='consumed'
    assert len(all_rows("SELECT * FROM ads_events WHERE action='booking_consumed'"))==1
    assert cancel(client,row,booking_version=2).status_code==409

def test_late_start_preserves_full_time_only_when_next_booking_is_safe(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);reserve(client,row,s)
    other=another(client);reserve(client,other,s,starts_at=14600)
    clock[0]=11001
    assert launch(client,row,s,booking_version=1).status_code==409
    assert cancel(client,other).status_code==200
    started=launch(client,row,s,booking_version=1).json()
    assert started['remaining_seconds']==3600 and started['expected_stop_at']==14601

def test_expired_booking_requires_explicit_cancel_before_unbooked_start(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);reserve(client,row,s);clock[0]=14600
    assert launch(client,row,s,booking_version=1).status_code==409
    assert cancel(client,row).status_code==200
    assert launch(client,row,s).status_code==200

@pytest.mark.parametrize('gap',[3599,3600])
def test_unbooked_start_and_paused_overrun_cannot_displace_next_reservation(client,monkeypatch,gap):
    row,s,clock=setup(client,monkeypatch)
    future=another(client);reserve(client,future,s,starts_at=10000+gap)
    result=launch(client,row,s)
    assert result.status_code==(200 if gap==3600 else 409)
    if gap==3600:
        clock[0]=10010;change(client,row,'pause',placement_version=1)
        clock[0]=10020;assert change(client,row,'resume',placement_version=2).status_code==409
        clock[0]=13600;assert launch(client,future,s,booking_version=1).status_code==409
        assert change(client,row,'finish',placement_version=2).status_code==200
        assert launch(client,future,s,booking_version=1).status_code==200

@pytest.mark.parametrize('paused',[False,True])
def test_current_occupancy_blocks_new_future_reservation(client,monkeypatch,paused):
    row,s,_=setup(client,monkeypatch);launch(client,row,s)
    if paused:change(client,row,'pause',placement_version=1)
    other=another(client);assert reserve(client,other,s,starts_at=10000+86400).status_code==409

def test_refund_requires_cancel_and_customer_cannot_modify_booking(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    assert record(client,row,'refund').status_code==409
    path='/api/admin/ads/requests/'+row['id']+'/booking/cancel'
    assert client.post(path,json={}).status_code==403
    assert client.post(path,json={},headers={**admin_headers(),'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/admin/ads/bookings').status_code==403
    assert client.get('/api/ads/bookings').status_code==404
    assert cancel(client,row).status_code==200
    assert record(client,row,'refund').status_code==200

def test_competing_reservations_and_database_triggers_prevent_overlap(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);other=another(client)
    with ThreadPoolExecutor() as pool:rs=list(pool.map(lambda r:reserve(client,r,s),[row,other]))
    assert sorted(r.status_code for r in rs)==[200,409]
    winner=all_rows('SELECT * FROM ads_bookings')[0];loser=other if winner['request_id']==row['id'] else row
    with pytest.raises(sqlite3.IntegrityError):
        with db() as conn:conn.execute("INSERT INTO ads_bookings SELECT ?,site_id,site_name,starts_at,ends_at,state,version,created,updated FROM ads_bookings",(loser['id'],))

def test_reservation_refund_race_is_atomic(client,monkeypatch):
    row,s,_=setup(client,monkeypatch)
    with ThreadPoolExecutor() as pool:
        a=pool.submit(reserve,client,row,s);b=pool.submit(record,client,row,'refund')
        assert sorted([a.result().status_code,b.result().status_code])==[200,409]

def test_start_cancel_race_has_consistent_booking_and_placement(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);reserve(client,row,s);clock[0]=11000
    with ThreadPoolExecutor() as pool:
        a=pool.submit(launch,client,row,s,booking_version=1);b=pool.submit(cancel,client,row)
        assert sorted([a.result().status_code,b.result().status_code])==[200,409]
    result=client.get('/api/ads/requests/'+row['id']).json()
    assert (result['booking']['state']=='consumed')==bool(result['placement'])

def test_legacy_placement_retry_keeps_fingerprint_after_upgrade(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);key=str(uuid.uuid4())
    old={'operation_id':key,'request_version':row['version'],'payment_version':1,'placement_version':0,'site_id':s['id'],'site_version':s['version'],'note':'private-placement-check','checked':True}
    assert launch(client,row,s,operation_id=key).status_code==200
    legacy=hashlib.sha256(json.dumps(old,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db() as conn:conn.execute('UPDATE ads_placement_actions SET fingerprint=? WHERE operation_id=?',(legacy,key))
    assert launch(client,row,s,operation_id=key).status_code==200
    assert len(all_rows('SELECT * FROM ads_placement_actions'))==1

def test_booking_migration_backup_and_history_preserve_reserved_slot(client,monkeypatch,tmp_path):
    from app import config as c
    from test_backup import b
    import os
    real_time=bookings.time.time
    row,s,_=setup(client,monkeypatch);reserve(client,row,s);cancel(client,row);reserve(client,row,s,booking_version=2,starts_at=20000)
    before=all_rows('SELECT * FROM ads_bookings');actions=all_rows('SELECT * FROM ads_booking_actions')
    init_db();assert all_rows('SELECT * FROM ads_bookings')==before
    monkeypatch.setattr(bookings.time,'time',real_time)
    archive=tmp_path/'bookings.pvb';b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'long-backup-password');b.restore(archive,'long-backup-password',tmp_path/'restored')
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        conn.row_factory=sqlite3.Row
        assert [dict(r) for r in conn.execute('SELECT * FROM ads_bookings')]==before
        assert [dict(r) for r in conn.execute('SELECT * FROM ads_booking_actions')]==actions
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0


@pytest.mark.parametrize('changes',[{'enabled':False},{'verified':False}])
def test_disabled_or_unverified_sites_reject_reservation(client,monkeypatch,changes):
    row,s,_=setup(client,monkeypatch)
    changed=site(client,id=s['id'],version=1,**changes).json()
    assert reserve(client,row,changed).status_code==409

def test_booking_pagination_and_database_update_trigger(client,monkeypatch):
    first,s,clock=setup(client,monkeypatch);rows=[first]
    for index in range(1,23):
        rid=str(uuid.uuid4())
        with db() as conn:
            conn.execute('INSERT INTO ads_requests(id,user_id,request_id,channel,comment,amount,hours,policy_version,state,reason,version,created,updated) SELECT ?,user_id,?,?,comment,amount,hours,policy_version,state,reason,version,created,updated FROM ads_requests WHERE id=?',(rid,str(uuid.uuid4()),'booking_page_'+str(index),first['id']))
        row=client.get('/api/ads/requests/'+rid).json();assert record(client,row).status_code==200;rows.append(row)
    for index,row in enumerate(rows):assert reserve(client,row,s,starts_at=11000+index*3600).status_code==200
    a=listing(client).json();b=listing(client,page=2).json()
    assert a['total']==23 and len(a['requests'])==20 and len(b['requests'])==3
    assert len({r['id'] for r in a['requests']+b['requests']})==23
    with pytest.raises(sqlite3.IntegrityError):
        with db() as conn:conn.execute('UPDATE ads_bookings SET starts_at=11001 WHERE request_id=?',(rows[1]['id'],))
    assert listing(client,page=3).json()['requests']==[]
