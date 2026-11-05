"""Ручной учёт расходов, границы периода и отдельный срез неисполненных размещений."""
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from app import ads_finance as finance
from app.db import db,all_rows,one,init_db
from test_shop import client,register,admin_headers
from test_ads import policy,submit,decision
from test_ads_payments import record
from test_ads_placements import site,launch,change


def setup(client,monkeypatch):
    register(client);clock=[10000];monkeypatch.setattr(finance.time,'time',lambda:clock[0])
    policy(client,accepting=True,hours=1);row=decision(client,submit(client).json()).json();record(client,row)
    return row,clock


def expense(client,**changes):
    body={'operation_id':str(uuid.uuid4()),'amount':50,'category':'server','incurred_at':9990,'reference':'cost/'+uuid.uuid4().hex,'note':'private-source-note',**changes}
    return client.post('/api/admin/ads/expenses',json=body,headers=admin_headers())


def void(client,row,**changes):
    return client.post('/api/admin/ads/expenses/'+row['id']+'/void',json={'operation_id':str(uuid.uuid4()),'version':row['version'],'note':'private-correction',**changes},headers=admin_headers())


def report(client,**params):
    return client.get('/api/admin/ads/finance',params=params,headers=admin_headers())


def test_empty_report_is_read_only_not_profit_and_no_seeded_expenses(client):
    before=all_rows('SELECT * FROM ads_events');result=report(client).json()
    assert result['cash']=={'received':0,'refunded':0,'net_received':0} and result['expenses']['amount']==0
    assert result['is_net_profit'] is False and result['source']=='manual_ledger'
    assert result['period']['end']-result['period']['start']==30*86400
    assert all_rows('SELECT * FROM ads_events')==before and not all_rows('SELECT * FROM ads_expenses')


def test_manual_cash_expenses_and_corrections_are_separate(client,monkeypatch):
    row,clock=setup(client,monkeypatch);entry=expense(client).json()
    result=report(client,start=9000,end=10001).json()
    assert result['cash']['received']==300 and result['expenses']['amount']==50 and result['cash_after_recorded_expenses']==250
    assert result['current_paid_requests']['not_started']['contract_amount']==300
    assert void(client,entry).status_code==200
    result=report(client,start=9000,end=10001).json()
    assert result['expenses']['amount']==0 and result['expenses']['voided_count']==1 and result['cash_after_recorded_expenses']==300
    assert result['expenses']['entries'][0]['void_reason']=='private-correction'
    clock[0]=10010;record(client,row,'refund')
    result=report(client,start=9000,end=10011).json()
    assert result['cash']['net_received']==0 and result['current_paid_requests']['not_started']['requests']==0
    assert not one('SELECT * FROM orders') and not one('SELECT * FROM subscriptions')


def test_cash_dates_and_expense_dates_have_explicit_exclusive_boundaries(client,monkeypatch):
    row,clock=setup(client,monkeypatch);expense(client,incurred_at=9999,amount=40);expense(client,incurred_at=10000,amount=60)
    before=report(client,start=9999,end=10000).json();after=report(client,start=10000,end=10001).json()
    assert before['cash']['received']==0 and before['expenses']['amount']==40
    assert after['cash']['received']==300 and after['expenses']['amount']==60
    clock[0]=10020;record(client,row,'refund')
    assert report(client,start=10000,end=10001).json()['cash']['refunded']==0
    assert report(client,start=10020,end=10021).json()['cash']['net_received']==-300


def test_current_paid_groups_do_not_treat_expired_active_timer_as_completed(client,monkeypatch):
    first,clock=setup(client,monkeypatch);rows=[first]
    for i in range(4):
        row=decision(client,submit(client,channel='@finance_channel_'+str(i)).json()).json();record(client,row);rows.append(row)
    for row in rows[1:]:launch(client,row,site(client).json())
    clock[0]=10600;change(client,rows[2],'pause',placement_version=1);change(client,rows[3],'finish',placement_version=1)
    clock[0]=13600;change(client,rows[4],'finish',placement_version=1)
    groups=report(client,start=10000,end=10001).json()['current_paid_requests']
    assert all(r['requests']==1 for r in groups.values())
    assert groups['active']['remaining_seconds']==0 and groups['paused']['remaining_seconds']==3000
    assert groups['early_stopped']['remaining_seconds']==3000 and groups['not_started']['remaining_seconds']==3600
    assert groups['completed']['remaining_seconds']==0


def test_expense_uuid_replay_after_void_does_not_add_cost(client,monkeypatch):
    setup(client,monkeypatch);key=str(uuid.uuid4());fields={'operation_id':key,'reference':'cost/same'}
    first=expense(client,**fields).json();assert expense(client,**fields).json()==first
    assert expense(client,**fields,amount=60).status_code==409
    assert void(client,first).status_code==200
    assert expense(client,**fields).json()['state']=='void'
    assert len(all_rows('SELECT * FROM ads_expenses'))==1 and len(all_rows('SELECT * FROM ads_expense_actions'))==2


def test_reference_is_normalized_and_can_be_corrected_after_void(client,monkeypatch):
    setup(client,monkeypatch);first=expense(client,reference='Cost/ABC').json()
    assert expense(client,reference='COST/ABC').status_code==409
    void(client,first);assert expense(client,reference='cost/abc',amount=70).status_code==200
    assert report(client,start=9000,end=10001).json()['expenses']['amount']==70


@pytest.mark.parametrize('changes',[{'amount':0},{'amount':True},{'amount':1.5},{'incurred_at':True},{'incurred_at':finance.MAX_TIME+1},{'category':'secret'},{'reference':'<private-secret>'},{'note':' '},{'extra':'private-secret'}])
def test_invalid_expense_never_writes_or_echoes(client,monkeypatch,changes):
    setup(client,monkeypatch);r=expense(client,**changes)
    assert r.status_code==422 and 'private-secret' not in r.text
    assert not all_rows('SELECT * FROM ads_expenses')


@pytest.mark.parametrize('params',[{'start':1},{'end':10000},{'start':10000,'end':10000},{'start':0,'end':367*86400},{'start':-1,'end':10000},{'start':1,'end':finance.MAX_TIME+1},{'page':0},{'page':100001}])
def test_invalid_report_ranges_are_rejected(client,params):
    assert report(client,**params).status_code==422


def test_future_cost_and_stale_void_are_rejected(client,monkeypatch):
    setup(client,monkeypatch)
    assert expense(client,incurred_at=10001).status_code==409
    first=expense(client).json();assert void(client,first,version=2).status_code==409
    key=str(uuid.uuid4());r=void(client,first,operation_id=key)
    assert r.status_code==200 and void(client,first,operation_id=key).status_code==200
    assert void(client,first).status_code==409


def test_negative_manual_balance_is_visible_without_profit_claim(client,monkeypatch):
    setup(client,monkeypatch);expense(client,amount=500)
    result=report(client,start=9000,end=10001).json()
    assert result['cash_after_recorded_expenses']==-200 and result['is_net_profit'] is False


def test_finance_is_admin_only_and_costs_do_not_leak_to_customer(client,monkeypatch):
    setup(client,monkeypatch);entry=expense(client).json()
    assert client.get('/api/admin/ads/finance').status_code==403
    assert client.post('/api/admin/ads/expenses',json={}).status_code==403
    assert client.post('/api/admin/ads/expenses/'+entry['id']+'/void',json={}).status_code==403
    assert client.post('/api/admin/ads/expenses',json={},headers={**admin_headers(),'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/ads/finance').status_code==404
    for path in ['/api/ads/requests','/api/me']:
        assert 'private-source-note' not in client.get(path).text


def test_concurrent_same_expense_and_same_reference_are_counted_once(client,monkeypatch):
    setup(client,monkeypatch);fields={'operation_id':str(uuid.uuid4()),'reference':'cost/concurrent'}
    with ThreadPoolExecutor() as pool:responses=list(pool.map(lambda _:expense(client,**fields),range(2)))
    assert all(r.status_code==200 for r in responses) and len(all_rows('SELECT * FROM ads_expenses'))==1
    with ThreadPoolExecutor() as pool:responses=list(pool.map(lambda _:expense(client,reference='cost/other'),range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]


def test_expense_pagination_and_read_only_snapshot(client,monkeypatch):
    setup(client,monkeypatch)
    for i in range(23):assert expense(client,incurred_at=9990+i%2).status_code==200
    before=all_rows('SELECT * FROM ads_expense_actions');first=report(client,start=9000,end=10001).json()['expenses'];second=report(client,start=9000,end=10001,page=2).json()['expenses']
    assert first['total']==23 and len(first['entries'])==20 and len(second['entries'])==3
    assert len({r['id'] for r in first['entries']+second['entries']})==23
    assert all_rows('SELECT * FROM ads_expense_actions')==before


def test_expenses_survive_repeated_migration_and_encrypted_restore(client,monkeypatch,tmp_path):
    from app import config as c
    from test_backup import b
    import os,sqlite3
    real_time=finance.time.time;setup(client,monkeypatch)
    first=expense(client).json();void(client,first);expense(client,amount=70)
    before=all_rows('SELECT * FROM ads_expenses');actions=all_rows('SELECT * FROM ads_expense_actions')
    init_db();assert all_rows('SELECT * FROM ads_expenses')==before
    monkeypatch.setattr(finance.time,'time',real_time)
    archive=tmp_path/'finance.pvb';b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'long-backup-password');b.restore(archive,'long-backup-password',tmp_path/'restored')
    with sqlite3.connect(tmp_path/'restored/shop.db') as conn:
        conn.row_factory=sqlite3.Row
        assert [dict(r) for r in conn.execute('SELECT * FROM ads_expenses')]==before
        assert [dict(r) for r in conn.execute('SELECT * FROM ads_expense_actions')]==actions
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
