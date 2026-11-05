"""Очередь остановок: единый снимок времени, фильтры, доступ и отсутствие скрытых изменений."""
import uuid
import pytest
from app import ads_placements as placements
from app.db import db,all_rows
from test_shop import client,admin_headers
from test_ads_placements import paid,site,launch,change
from test_ads_payments import record


def seed(client,monkeypatch,specs):
    first=paid(client);clock=[10000]
    monkeypatch.setattr(placements.time,'time',lambda:clock[0])
    rows=[]
    for index,(state,delta) in enumerate(specs):
        if index==0:row=first
        else:
            rid=str(uuid.uuid4())
            with db() as conn:
                conn.execute('INSERT INTO ads_requests(id,user_id,request_id,channel,comment,amount,hours,policy_version,state,reason,version,created,updated) SELECT ?,user_id,?,?,comment,amount,hours,policy_version,state,reason,version,created,updated FROM ads_requests WHERE id=?',
                             (rid,str(uuid.uuid4()),'queue_channel_'+str(index),first['id']))
            row=client.get('/api/ads/requests/'+rid).json()
            assert record(client,row).status_code==200
        s=site(client,name='Площадка '+str(index)).json()
        assert launch(client,row,s).status_code==200
        if state in ('paused','finished'):
            action='pause' if state=='paused' else 'finish'
            assert change(client,row,action,placement_version=1).status_code==200
        else:
            with db() as conn:conn.execute('UPDATE ads_placements SET segment_started=? WHERE request_id=?',(clock[0]-3600+delta,row['id']))
        rows.append(row)
    return rows,clock


def queue(client,view='all',page=1):
    return client.get('/api/admin/ads/stop-queue',params={'view':view,'page':page},headers=admin_headers())


def snapshot():
    return {t:all_rows('SELECT * FROM '+t) for t in ('ads_sites','ads_placements','ads_placement_actions','ads_events','ads_payment_entries')}


def test_empty_stop_queue_is_read_only_and_has_no_seeded_sites(client):
    before=snapshot();response=queue(client).json()
    assert response['requests']==[] and response['counts']=={'due':0,'upcoming':0,'paused':0} and response['total']==0
    assert snapshot()==before


def test_queue_filters_counts_boundary_order_and_private_data(client,monkeypatch):
    rows,clock=seed(client,monkeypatch,[('active',60),('active',-120),('paused',0),('finished',0),('active',0)])
    before=snapshot();result=queue(client).json()
    assert result['counts']=={'due':2,'upcoming':1,'paused':1} and result['total']==4
    assert [r['id'] for r in result['requests']]==[rows[1]['id'],rows[4]['id'],rows[0]['id'],rows[2]['id']]
    assert result['requests'][0]['overdue_seconds']==120 and result['requests'][1]['overdue_seconds']==0
    assert result['requests'][0]['placement']['state']=='active'
    assert result['requests'][-1]['placement']['expected_stop_at'] is None
    assert result['generated_at']==10000
    for view,count in [('due',2),('upcoming',1),('paused',1)]:
        filtered=queue(client,view).json()
        assert filtered['total']==count and filtered['counts']==result['counts']
        assert all(r['queue_status']==view for r in filtered['requests'])
    text=queue(client).text
    assert 'private-site-check' not in text and 'private-placement-check' not in text
    assert 'reference' not in text and 'note' not in text
    assert snapshot()==before


def test_stop_queue_pagination_has_no_duplicates_and_stable_ties(client,monkeypatch):
    rows,_=seed(client,monkeypatch,[('active',-10)]*23)
    first=queue(client).json();second=queue(client,page=2).json()
    assert first['total']==23 and len(first['requests'])==20 and len(second['requests'])==3
    ids=[r['id'] for r in first['requests']+second['requests']]
    assert len(set(ids))==23 and set(ids)=={r['id'] for r in rows}
    assert [r['id'] for r in queue(client).json()['requests']]==ids[:20]
    assert queue(client,page=3).json()['requests']==[]


def test_stop_queue_requires_administrator_and_has_no_customer_route(client):
    paid(client)
    assert client.get('/api/admin/ads/stop-queue').status_code==403
    assert client.get('/api/admin/ads/stop-queue',headers={'Authorization':'Bearer wrong'}).status_code==403
    assert client.get('/api/ads/stop-queue').status_code==404


@pytest.mark.parametrize('params',[{'view':'<private-secret>'},{'page':0},{'page':100001},{'page':'1.5'},{'view':'finished'}])
def test_invalid_stop_queue_filters_are_safe(client,params):
    response=client.get('/api/admin/ads/stop-queue',params=params,headers=admin_headers())
    assert response.status_code==422 and 'private-secret' not in response.text


def test_due_queue_changes_with_clock_without_mutating_placement(client,monkeypatch):
    rows,clock=seed(client,monkeypatch,[('active',10)])
    before=snapshot();assert queue(client,'due').json()['total']==0
    clock[0]+=11;result=queue(client,'due').json()
    assert result['total']==1 and result['requests'][0]['overdue_seconds']==1
    assert result['requests'][0]['placement']['remaining_seconds']==0 and result['requests'][0]['placement']['state']=='active'
    assert snapshot()==before


def test_queue_commands_recheck_versions_and_completion_removes_entry(client,monkeypatch):
    rows,_=seed(client,monkeypatch,[('active',-1)]);row=rows[0]
    item=queue(client,'due').json()['requests'][0]
    assert item['version']==row['version'] and item['payment']['version']==1 and item['placement']['version']==1
    assert change(client,row,'pause',placement_version=1).status_code==200
    assert change(client,row,'finish',placement_version=item['placement']['version']).status_code==409
    assert queue(client,'due').json()['total']==0 and queue(client,'paused').json()['total']==1
    assert change(client,row,'finish',placement_version=2).status_code==200
    assert queue(client).json()['total']==0


def test_queue_uses_one_clock_snapshot_for_all_rows(client,monkeypatch):
    seed(client,monkeypatch,[('active',0),('active',1)])
    calls=[]
    def ticking():
        calls.append(1)
        return 10000+len(calls)-1
    monkeypatch.setattr(placements.time,'time',ticking)
    result=placements.stop_queue('all')
    assert len(calls)==1 and result['generated_at']==10000
    assert [r['queue_status'] for r in result['requests']]==['due','upcoming']
    assert result['requests'][1]['placement']['remaining_seconds']==1
