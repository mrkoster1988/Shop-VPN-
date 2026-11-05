"""Атомарный перенос не теряет исходную бронь при отказе или конкурирующем действии."""
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from app import ads_bookings as bookings
from app.db import db,all_rows,init_db
from test_shop import client,admin_headers
from test_ads_bookings import setup,reserve,cancel,another
from test_ads_payments import record
from test_ads_placements import site,launch


def move(client,row,s,**changes):
    body={'operation_id':str(uuid.uuid4()),'request_version':row['version'],'payment_version':1,'booking_version':1,
          'site_id':s['id'],'site_version':s['version'],'starts_at':20000,'note':'private-move-note',**changes}
    return client.post('/api/admin/ads/requests/'+row['id']+'/booking/reschedule',json=body,headers=admin_headers())


def state():
    return {t:all_rows('SELECT * FROM '+t+' ORDER BY rowid') for t in ('ads_bookings','ads_booking_actions','ads_events','ads_payment_entries')}


def test_transfer_changes_slot_in_one_version_and_preserves_history(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s);target=site(client,name='Новая площадка').json()
    result=move(client,row,target).json()
    assert result['state']=='reserved' and result['version']==2 and result['starts_at']==20000 and result['ends_at']==23600
    assert result['site_id']==target['id']
    history=all_rows('SELECT * FROM ads_booking_actions ORDER BY rowid')
    assert [r['action'] for r in history]==['reserve','reschedule']
    assert [json.loads(r['details'])['starts_at'] for r in history]==[11000,20000]
    assert all_rows("SELECT * FROM ads_events WHERE action='booking_reschedule'")
    own=client.get('/api/ads/requests/'+row['id'])
    assert own.json()['booking']['starts_at']==20000 and 'private-move-note' not in own.text


@pytest.mark.parametrize('changes',[{'starts_at':10000},{'starts_at':10000+366*86400},{'site_version':99},{'booking_version':0},{'request_version':1},{'payment_version':0}])
def test_rejected_transfer_keeps_original_slot_and_journal(client,monkeypatch,changes):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s);before=state()
    assert move(client,row,s,**changes).status_code==409
    assert state()==before


def test_overlap_keeps_source_even_for_transfer_to_same_site(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    other=another(client);reserve(client,other,s,starts_at=20000);before=state()
    assert move(client,row,s,starts_at=20001).status_code==409
    assert state()==before
    assert move(client,row,s,starts_at=11001).status_code==200


def test_occupied_target_keeps_source(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    other=another(client);target=site(client).json();launch(client,other,target);before=state()
    assert move(client,row,target).status_code==409 and state()==before


def test_transfer_uuid_retries_do_not_repeat_and_changed_payload_conflicts(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s);key=str(uuid.uuid4())
    assert move(client,row,s,operation_id=key).status_code==200
    before=state();assert move(client,row,s,operation_id=key).status_code==200
    assert move(client,row,s,operation_id=key,starts_at=21000).status_code==409
    assert state()==before
    cancel(client,row,booking_version=2)
    assert move(client,row,s,operation_id=key).json()['state']=='canceled'


def test_transfer_requires_existing_unconsumed_reservation(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch)
    assert move(client,row,s,booking_version=0).status_code==409
    reserve(client,row,s);cancel(client,row)
    assert move(client,row,s,booking_version=2).status_code==409
    reserve(client,row,s,booking_version=2);clock[0]=11000
    launch(client,row,s,booking_version=3)
    assert move(client,row,s,booking_version=4).status_code==409


def test_expired_reservation_can_be_moved_without_cancel(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);reserve(client,row,s);clock[0]=14600
    assert move(client,row,s).status_code==200
    assert client.get('/api/ads/requests/'+row['id']).json()['booking']['version']==2


def test_move_vs_move_uses_one_version(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    with ThreadPoolExecutor() as pool:responses=list(pool.map(lambda start:move(client,row,s,starts_at=start),[20000,25000]))
    assert sorted(r.status_code for r in responses)==[200,409]
    assert len(all_rows("SELECT * FROM ads_booking_actions WHERE action='reschedule'"))==1


def test_move_vs_start_cannot_move_used_booking(client,monkeypatch):
    row,s,clock=setup(client,monkeypatch);reserve(client,row,s);clock[0]=11000
    with ThreadPoolExecutor() as pool:
        a=pool.submit(move,client,row,s);b=pool.submit(launch,client,row,s,booking_version=1)
        assert sorted([a.result().status_code,b.result().status_code])==[200,409]
    current=client.get('/api/ads/requests/'+row['id']).json()
    if current['placement']:assert current['booking']['state']=='consumed'
    else:assert current['booking']['starts_at']==20000


def test_access_and_invalid_input_cannot_touch_original(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s);before=state()
    path='/api/admin/ads/requests/'+row['id']+'/booking/reschedule'
    assert client.post(path,json={}).status_code==403
    assert client.post(path,json={},headers={**admin_headers(),'Origin':'https://evil.example'}).status_code==403
    assert move(client,row,s,extra='private-secret').status_code==422
    assert state()==before


def test_v036_journal_migration_preserves_rowids_and_old_idempotency(client,monkeypatch,tmp_path):
    from app import config as c
    from test_backup import b
    import os
    real_time=bookings.time.time
    row,s,_=setup(client,monkeypatch);key=str(uuid.uuid4())
    reserve(client,row,s,operation_id=key);cancel(client,row);reserve(client,row,s,booking_version=2)
    with db() as conn:
        original=conn.execute('SELECT sql FROM sqlite_master WHERE name=\'ads_booking_actions\'').fetchone()['sql']
        rows=conn.execute('SELECT rowid,* FROM ads_booking_actions ORDER BY rowid').fetchall()
        conn.execute('DROP TABLE ads_booking_actions')
        conn.execute(original.replace(",'reschedule'",''))
        conn.executemany('INSERT INTO ads_booking_actions(rowid,operation_id,request_id,action,fingerprint,note,details,created) VALUES (?,?,?,?,?,?,?,?)',[tuple(r) for r in rows])
    before=state();init_db();assert state()==before
    with db() as conn:assert [tuple(r) for r in conn.execute('SELECT rowid,* FROM ads_booking_actions ORDER BY rowid')]==[tuple(r) for r in rows]
    assert move(client,row,s,booking_version=3).status_code==200
    assert reserve(client,row,s,operation_id=key).status_code==200
    migrated=state();init_db();assert state()==migrated
    monkeypatch.setattr(bookings.time,'time',real_time)
    archive=tmp_path/'reschedule.pvb';b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'long-backup-password');b.restore(archive,'long-backup-password',tmp_path/'restored')
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        assert conn.execute("SELECT count(*) FROM ads_booking_actions WHERE action='reschedule'").fetchone()[0]==1
        assert conn.execute('SELECT starts_at FROM ads_bookings').fetchone()[0]==20000
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0


def test_move_vs_competing_reservation_never_loses_source_on_conflict(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s);other=another(client)
    with ThreadPoolExecutor() as pool:
        a=pool.submit(move,client,row,s);b=pool.submit(reserve,client,other,s,starts_at=20000)
        statuses=[a.result().status_code,b.result().status_code]
    assert sorted(statuses)==[200,409]
    current=client.get('/api/ads/requests/'+row['id']).json()['booking']
    assert current['state']=='reserved' and current['starts_at']==(20000 if statuses[0]==200 else 11000)


def test_refund_cannot_race_transfer_of_reserved_slot(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    with ThreadPoolExecutor() as pool:
        a=pool.submit(move,client,row,s);b=pool.submit(record,client,row,'refund')
        assert a.result().status_code==200 and b.result().status_code==409


def test_custom_journal_columns_are_not_discarded_by_migration(client,monkeypatch):
    row,s,_=setup(client,monkeypatch);reserve(client,row,s)
    with db() as conn:
        definition=conn.execute("SELECT sql FROM sqlite_master WHERE name='ads_booking_actions'").fetchone()['sql']
        rows=conn.execute('SELECT * FROM ads_booking_actions').fetchall()
        conn.execute('DROP TABLE ads_booking_actions');conn.execute(definition.replace(",'reschedule'",''))
        conn.execute("ALTER TABLE ads_booking_actions ADD COLUMN custom_note TEXT DEFAULT 'kept'")
        conn.executemany('INSERT INTO ads_booking_actions(operation_id,request_id,action,fingerprint,note,details,created) VALUES (?,?,?,?,?,?,?)',[tuple(r) for r in rows])
    before=all_rows('SELECT * FROM ads_booking_actions')
    with pytest.raises(ValueError,match='Нестандартная схема'):init_db()
    assert all_rows('SELECT * FROM ads_booking_actions')==before
