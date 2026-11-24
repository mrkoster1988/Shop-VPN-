"""Financial settings and ledger: no money movement, no fabricated dates or balances."""
import time
import uuid

import pytest
from app import config as c, providers, service
from app.db import one, execute, init_db
from test_shop import client, register, order, confirm, admin_headers, add_customer, add_admin_order

POLICY='/api/admin/payment-policy'
REPORT='/api/admin/shop-finance'
ENTRIES=REPORT+'/entries'


def policy(client,**changes):
    response=client.get(POLICY,headers=admin_headers())
    assert response.status_code==200,response.text
    p=response.json()
    return {k:p[k] for k in ('version','web_mode','web_enabled','stars_enabled','manual_text')}|changes


def save(client,**changes):
    result=client.put(POLICY,headers=admin_headers(),json=policy(client,**changes))
    assert result.status_code==200,result.text
    return result.json()


def entry(**changes):
    return {'operation_id':str(uuid.uuid4()),'kind':'expense','category':'server','currency':'RUB',
            'amount_minor':12550,'occurred_at':int(time.time())-1,'order_id':None,
            'reference':'fixture-'+uuid.uuid4().hex,'note':'Сверено по тестовой выписке',
            'verified':True,**changes}


def record(client,data):
    return client.post(ENTRIES,headers=admin_headers(),json=data)


def report(client):
    r=client.get(REPORT,headers=admin_headers())
    assert r.status_code==200,r.text
    return r.json()


def test_payment_defaults_pause_only_new_orders_and_old_invoice_is_preserved(client,monkeypatch):
    monkeypatch.setenv('MANUAL_PAYMENT_TEXT','fixture-old-details')
    register(client)
    assert policy(client)=={'version':0,'web_mode':'inherit','web_enabled':True,'stars_enabled':True,'manual_text':''}
    assert client.get('/api/catalog').json()['payment_policy_version']==0
    first=order(client,'wg')
    assert first['method']=='manual' and first['instructions']=='fixture-old-details'
    paused=save(client,web_enabled=False)
    assert paused['version']==1 and paused['open_web_orders']==1
    assert client.get('/api/catalog').json()['payment_available'] is False
    assert order(client,'wg')['id']==first['id']
    assert client.post('/api/orders',json={'plan':'month','protocol':'xui'}).status_code==409
    assert client.post('/api/quote',json={'plan':'month','protocol':'xui','plan_version':1}).status_code==409
    assert client.put(POLICY,headers=admin_headers(),json=policy(client,manual_text='changed')).status_code==409
    assert confirm(client,first['id']).status_code==200
    saved=save(client,web_enabled=True,manual_text='fixture-new-details')
    assert saved['version']==2
    assert client.post('/api/orders',json={'plan':'month','protocol':'xui','payment_policy_version':0}).status_code==409
    second=order(client,'xui')
    assert second['instructions']=='fixture-new-details'
    assert one('SELECT count(*) AS n FROM orders')['n']==2


def test_method_switch_requires_https_config_and_no_open_ruble_orders(client,monkeypatch):
    monkeypatch.delenv('YOOKASSA_SHOP_ID',raising=False)
    monkeypatch.delenv('YOOKASSA_SECRET',raising=False)
    register(client)
    assert client.put(POLICY,headers=admin_headers(),json=policy(client,web_mode='sbp')).status_code==409
    first=order(client,'wg')
    monkeypatch.setenv('YOOKASSA_SHOP_ID','fixture-shop')
    monkeypatch.setenv('YOOKASSA_SECRET','fixture-secret-not-real')
    monkeypatch.setattr(c,'SECURE_COOKIE',True)
    assert client.put(POLICY,headers=admin_headers(),json=policy(client,web_mode='sbp')).status_code==409
    assert confirm(client,first['id']).status_code==200
    updated=save(client,web_mode='sbp')
    assert updated['effective_web_mode']=='sbp' and updated['merchant_configured'] is True
    assert 'fixture-secret-not-real' not in str(updated) and 'fixture-shop' not in str(updated)
    calls=[]
    async def fake_payment(row,email):
        calls.append(row)
        return {'id':'fixture-sbp-id','confirmation':{'confirmation_url':'https://payment.example/checkout'}}
    monkeypatch.setattr(providers,'payment',fake_payment)
    second=order(client,'xui')
    assert second['method']=='sbp' and calls[0]['method']=='sbp'
    assert client.get('/api/orders/'+first['id']).json()['method']=='manual'
    assert client.put(POLICY,headers=admin_headers(),json=policy(client,web_mode='manual')).status_code==409


def test_stars_pause_does_not_invalidate_existing_verified_invoice(client,monkeypatch):
    monkeypatch.setattr(c,'BOT_TOKEN','fixture-bot')
    execute("UPDATE store_settings SET terms_text='Условия'")
    user=service.telegram_account(777)
    client.get(service.ticket(user['id']))
    async def invoice(row):return 'https://t.me/$fixture-invoice'
    monkeypatch.setattr(providers,'stars_invoice',invoice)
    first=client.post('/api/orders',json={'plan':'month','protocol':'wg','accept_terms':True}).json()
    save(client,stars_enabled=False)
    catalog=client.get('/api/catalog').json()
    assert catalog['payment_mode']=='stars' and catalog['payment_available'] is False
    assert client.post('/api/orders',json={'plan':'month','protocol':'xui','accept_terms':True}).status_code==409
    event={'invoice_payload':first['id'],'currency':'XTR','total_amount':first['amount'],'telegram_payment_charge_id':'fixture-charge'}
    service.record_stars(777,event)
    assert one('SELECT status FROM orders WHERE id=?',(first['id'],))['status']=='paid'


def test_receipts_use_observed_payment_date_and_never_add_currencies(client):
    uid=add_customer('buyer@example.com')
    rub=add_admin_order(uid,'rub-order',status='paid',created=100)
    add_admin_order(uid,'stars-order',status='paid',method='stars',protocol='wg')
    add_admin_order(uid,'old-order',status='paid',protocol='xui')
    execute("DELETE FROM order_milestones WHERE order_id='old-order'")
    add_admin_order(uid,'unpaid-order',status='pending')
    data=report(client)
    assert data['totals']['RUB']['receipts']==19900
    assert data['totals']['XTR']['receipts']==199
    assert data['missing_payment_timestamp']==1
    assert data['is_wallet'] is False and data['real_refunds_executed'] is False
    fee=entry(kind='fee',category='commission',amount_minor=150,order_id='rub-order')
    refund=entry(kind='refund',category='refund',amount_minor=5000,order_id='rub-order')
    stars=entry(kind='fee',category='commission',currency='XTR',amount_minor=9,order_id='stars-order')
    for item in (entry(amount_minor=2500),fee,refund,stars):
        assert record(client,item).status_code==200
    totals=report(client)['totals']
    assert totals['RUB']=={'receipts':19900,'refunds':5000,'fees':150,'expenses':2500,'net_recorded':12250}
    assert totals['XTR']=={'receipts':199,'refunds':0,'fees':9,'expenses':0,'net_recorded':190}
    assert one("SELECT status FROM orders WHERE id='rub-order'")['status']=='paid'
    assert one('SELECT count(*) AS n FROM subscriptions')['n']==0


def test_refund_limit_and_replay_and_void_are_atomic(client):
    uid=add_customer('buyer@example.com')
    add_admin_order(uid,'refund-order',status='paid')
    payload=entry(kind='refund',category='refund',amount_minor=10000,order_id='refund-order')
    first=record(client,payload);assert first.status_code==200,first.text
    saved=first.json()
    assert record(client,payload).json()==saved
    assert record(client,{**payload,'amount_minor':9000}).status_code==409
    assert record(client,entry(kind='refund',category='refund',amount_minor=10000,order_id='refund-order')).status_code==409
    assert record(client,entry(reference=payload['reference'])).status_code==409
    correction={'operation_id':str(uuid.uuid4()),'version':1,'note':'Ошибочная запись, проверено'}
    url=ENTRIES+'/'+saved['id']+'/void'
    done=client.post(url,headers=admin_headers(),json=correction)
    assert done.status_code==200 and done.json()['state']=='void'
    assert client.post(url,headers=admin_headers(),json=correction).json()==done.json()
    assert client.post(url,headers=admin_headers(),json={**correction,'operation_id':str(uuid.uuid4())}).status_code==409
    assert report(client)['totals']['RUB']['refunds']==0
    assert one('SELECT count(*) AS n FROM shop_finance_actions')['n']==2
    assert one("SELECT status FROM orders WHERE id='refund-order'")['status']=='paid'


@pytest.mark.parametrize('changes',[
    {'amount_minor':True},{'amount_minor':0},{'amount_minor':1.5},{'currency':'USD'},
    {'verified':False},{'occurred_at':32503680000},{'reference':'<bad>'},
    {'kind':'refund','category':'refund','order_id':None},{'kind':'fee','category':'server'},
])
def test_unverified_or_invalid_financial_entries_do_not_write(client,changes):
    assert record(client,entry(**changes)).status_code==422
    assert one('SELECT count(*) AS n FROM shop_finance_entries')['n']==0


def test_refunds_require_paid_order_and_same_currency(client):
    uid=add_customer('buyer@example.com')
    add_admin_order(uid,'pending-order',status='pending')
    add_admin_order(uid,'rub-order',status='paid')
    for order_id,currency in [('missing','RUB'),('pending-order','RUB'),('rub-order','XTR')]:
        assert record(client,entry(kind='refund',category='refund',order_id=order_id,currency=currency)).status_code==409
    assert report(client)['entries']==[]


def test_finance_admin_requires_explicit_rights_and_moderator_cannot_read(client):
    from test_staff_roles import bootstrap,add_role,login,use,ROOT
    dev=bootstrap(client);add_role(client,'administrator');add_role(client,'moderator')
    admin=login(client,'administrator')
    assert client.get(REPORT).status_code==403
    assert client.get(POLICY).status_code==403
    use(client,dev)
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['finance_write']}).status_code==422
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['finance_read']}).status_code==200
    use(client,admin)
    assert client.get(REPORT).status_code==200
    assert client.post(ENTRIES,json=entry()).status_code==403
    use(client,dev)
    assert client.put(ROOT+'role-policies/administrator',json={'version':1,'permissions':['finance_read','finance_write','payment_settings']}).status_code==200
    use(client,admin)
    assert client.post(ENTRIES,json=entry()).status_code==200
    assert client.get(POLICY).status_code==200
    login(client,'moderator')
    assert client.get(REPORT).status_code==403
    assert client.post(ENTRIES,json=entry()).status_code==403
