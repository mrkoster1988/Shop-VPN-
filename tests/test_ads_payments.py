"""Проверки ручного учёта рекламных поступлений и полных возвратов."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.db import one,all_rows,init_db
from test_shop import client,register,admin_headers
from test_ads import policy,submit,decision


def approved(client):
    register(client);policy(client,accepting=True)
    return decision(client,submit(client).json()).json()


def record(client,row,kind='receipt',**changes):
    body={'operation_id':str(uuid.uuid4()),'request_version':row['version'],'payment_version':0 if kind=='receipt' else 1,
          'amount':row['amount'],'reference':'bank/'+uuid.uuid4().hex,'note':'Сверено в кабинете банка','checked':True,**changes}
    return client.post('/api/admin/ads/requests/'+row['id']+'/payments/'+kind,json=body,headers=admin_headers())


def test_manual_receipt_and_refund_are_separate_from_vpn_and_customer_secrets(client):
    row=approved(client);received=record(client,row,reference='Bank/Receipt-123',note='private-note').json()
    assert received['status']=='confirmed' and received['version']==1 and received['received_amount']==300
    customer=client.get('/api/ads/requests/'+row['id']).json()
    assert customer['payment']=={'status':'confirmed','version':1,'received_amount':300,'refunded_amount':0,'currency':'RUB'}
    for path in ['/api/ads/requests/'+row['id'],'/api/ads/requests']:
        text=client.get(path).text
        assert 'private-note' not in text and 'bank/receipt' not in text and 'operation_id' not in text
    history=client.get('/api/admin/ads/requests/'+row['id']+'/payments',headers=admin_headers()).json()
    assert history['entries'][0]['reference']=='bank/receipt-123'
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':row['version']}).status_code==409
    refunded=record(client,row,'refund').json()
    assert refunded['status']=='refunded' and refunded['version']==2 and refunded['refunded_amount']==300
    assert client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':row['version']}).status_code==200
    assert not one('SELECT * FROM orders') and not one('SELECT * FROM subscriptions')
    assert len(all_rows("SELECT * FROM ads_events WHERE action LIKE 'payment_%'"))==2


def test_payment_idempotency_survives_refund_and_rejects_payload_reuse(client):
    row=approved(client);key=str(uuid.uuid4());fields={'operation_id':key,'reference':'receipt/abc'}
    first=record(client,row,**fields);assert first.status_code==200
    assert record(client,row,**fields).json()==first.json()
    assert record(client,row,**fields,note='Другая проверка').status_code==409
    assert record(client,row,'refund').status_code==200
    assert record(client,row,**fields).json()['status']=='refunded'
    assert len(all_rows('SELECT * FROM ads_payment_entries'))==2


@pytest.mark.parametrize('changes',[{'checked':False},{'checked':1},{'amount':True},{'amount':0},{'amount':1.5},{'note':'  '},{'reference':'<private-secret>'},{'payment_version':-1},{'extra':'private-secret'}])
def test_invalid_payment_never_records_or_echoes_fields(client,changes):
    row=approved(client);result=record(client,row,**changes)
    assert result.status_code==422 and 'private-secret' not in result.text
    assert not one('SELECT * FROM ads_payment_entries')


def test_exact_snapshot_amount_and_versions_required(client):
    row=approved(client);policy(client,amount=700)
    for changes in [{'amount':700},{'amount':299},{'request_version':1},{'payment_version':1}]:
        assert record(client,row,**changes).status_code==409
    assert record(client,row).status_code==200
    assert record(client,row).status_code==409
    assert record(client,row,'refund',amount=100).status_code==409
    assert record(client,row,'refund',payment_version=0).status_code==409
    assert record(client,row,'refund').status_code==200
    assert record(client,row,'refund',payment_version=2).status_code==409
    assert record(client,row,payment_version=2).status_code==409


@pytest.mark.parametrize('state',['pending','rejected','canceled'])
def test_unapproved_or_canceled_request_cannot_receive_money(client,state):
    register(client);policy(client,accepting=True);row=submit(client).json()
    if state=='rejected':row=decision(client,row,state='rejected',reason='Не подходит').json()
    if state=='canceled':row=client.post('/api/ads/requests/'+row['id']+'/cancel',json={'version':1}).json()
    assert record(client,row).status_code==409
    assert not one('SELECT * FROM ads_payment_entries')


def test_refund_requires_receipt_and_distinct_global_reference(client):
    row=approved(client)
    assert record(client,row,'refund',payment_version=0).status_code==409
    assert record(client,row,reference='Bank/ABC').status_code==200
    assert record(client,row,'refund',reference='bank/abc').status_code==409
    other=decision(client,submit(client,channel='@other_channel').json()).json()
    assert record(client,other,reference='BANK/ABC').status_code==409
    assert record(client,other,reference='bank/xyz').status_code==200
    assert record(client,row,'refund',reference='bank/refund').status_code==200
    assert record(client,other,'refund',reference='BANK/REFUND').status_code==409


def test_access_csrf_and_unknown_requests(client):
    row=approved(client);path='/api/admin/ads/requests/'+row['id']+'/payments'
    assert client.get(path).status_code==401
    assert client.post(path+'/receipt',json={}).status_code==401
    assert client.post(path+'/receipt',json={},headers={**admin_headers(),'Origin':'https://evil.example'}).status_code==403
    missing={**row,'id':str(uuid.uuid4())}
    assert record(client,missing).status_code==404
    assert client.get('/api/admin/ads/requests/'+missing['id']+'/payments',headers=admin_headers()).status_code==404
    assert client.get('/api/ads/requests/'+row['id']+'/payments').status_code==404


def test_simultaneous_payment_confirmation_is_counted_once(client):
    row=approved(client);key=str(uuid.uuid4());fields={'operation_id':key,'reference':'bank/same-operation'}
    with ThreadPoolExecutor() as pool:results=list(pool.map(lambda _:record(client,row,**fields),range(2)))
    assert all(r.status_code==200 for r in results)
    assert len(all_rows('SELECT * FROM ads_payment_entries'))==1
    assert len(all_rows("SELECT * FROM ads_events WHERE action='payment_receipt'"))==1


def test_receipt_cancel_race_cannot_leave_canceled_paid_request(client):
    row=approved(client)
    with ThreadPoolExecutor() as pool:
        receipt=pool.submit(record,client,row)
        cancel=pool.submit(client.post,'/api/ads/requests/'+row['id']+'/cancel',json={'version':row['version']})
        statuses=[receipt.result().status_code,cancel.result().status_code]
    assert sorted(statuses)==[200,409]
    result=client.get('/api/ads/requests/'+row['id']).json()
    assert (result['state'],result['payment']['status']) in [('approved','confirmed'),('canceled','unpaid')]


def test_payment_backup_and_repeated_migration_preserve_journal(client,tmp_path):
    from app import config as c
    from test_backup import b
    import os,sqlite3
    row=approved(client);record(client,row);record(client,row,'refund')
    before=all_rows('SELECT * FROM ads_payment_entries');init_db()
    assert all_rows('SELECT * FROM ads_payment_entries')==before
    archive=tmp_path/'ads-payments.pvb'
    b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'long-backup-password')
    b.restore(archive,'long-backup-password',tmp_path/'restored')
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        assert conn.execute('SELECT kind,amount FROM ads_payment_entries ORDER BY id').fetchall()==[('receipt',300),('refund',300)]
        assert conn.execute("SELECT count(*) FROM ads_events WHERE action LIKE 'payment_%'").fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
