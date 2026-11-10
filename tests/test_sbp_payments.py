"""SBP payment creation and independently confirmed YooKassa callbacks.

No real merchant is contacted. All gateway responses are explicit fixtures.
"""
import asyncio
import copy
import json
import uuid

import pytest
from app import config as c, providers, service
from app.db import one, execute
from test_shop import client, order, register


def external_payment(order_id, *, method='sbp', amount='199.00', status='succeeded', paid=True):
    return {
        'id': 'provider-' + order_id,
        'metadata': {'order_id': order_id},
        'amount': {'value': amount, 'currency': 'RUB'},
        'payment_method': {'type': method},
        'status': status,
        'paid': paid,
    }


def notify(client, payment_id, event='payment.succeeded', **changes):
    body = {'type': 'notification', 'event': event, 'object': {'id': payment_id, **changes}}
    return client.post('/api/payments/yookassa/notify', json=body)


def test_sbp_is_explicit_opt_in_for_email_but_telegram_keeps_stars(client, monkeypatch):
    monkeypatch.setattr(c, 'PAYMENT_MODE', 'sbp')
    monkeypatch.setenv('YOOKASSA_SHOP_ID', 'configured-fixture')
    monkeypatch.setenv('YOOKASSA_SECRET', 'private-fixture')
    requests = []

    async def gateway(order, email):
        requests.append((order['id'], order['method'], email))
        return {'id': 'provider-' + order['id'], 'confirmation': {
            'confirmation_url': 'https://yookassa.ru/test/payment'}}

    monkeypatch.setattr(providers, 'payment', gateway)
    register(client)
    assert client.get('/api/catalog').json()['payment_mode'] == 'sbp'
    result = order(client)
    assert result['method'] == 'sbp'
    assert result['payment_url'] == 'https://yookassa.ru/test/payment'
    assert requests == [(result['id'], 'sbp', 'user@example.com')]
    assert order(client)['id'] == result['id'] and len(requests) == 1
    assert one('SELECT method FROM orders')['method'] == 'sbp'


def test_yookassa_sbp_payload_only_for_sbp_and_stable_idempotency(monkeypatch):
    monkeypatch.setenv('YOOKASSA_SHOP_ID', 'merchant-fixture')
    monkeypatch.setenv('YOOKASSA_SECRET', 'private-secret')
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'id': 'provider-response-id'}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, *, json, headers):
            calls.append((url, json, headers))
            return Response()
    monkeypatch.setattr(providers.httpx, 'AsyncClient', Client)
    purchase={'id': str(uuid.uuid4()), 'amount': 199, 'method': 'sbp'}
    asyncio.run(providers.payment(purchase, 'receipt@example.com'))
    url, data, headers = calls[-1]
    assert url == 'https://api.yookassa.ru/v3/payments'
    assert data['payment_method_data'] == {'type': 'sbp'}
    assert data['capture'] is True and data['confirmation']['type'] == 'redirect'
    assert data['amount'] == {'value': '199.00', 'currency': 'RUB'}
    assert data['metadata']['order_id'] == purchase['id']
    assert headers['Idempotence-Key'] == purchase['id']
    purchase['method']='yookassa'
    asyncio.run(providers.payment(purchase))
    assert 'payment_method_data' not in calls[-1][1]
    purchase['method']='stars'
    with pytest.raises(ValueError, match='UnsupportedPaymentMethod'):
        asyncio.run(providers.payment(purchase))


def test_fake_webhook_never_trusts_client_amount_or_paid_flag(client, monkeypatch):
    register(client)
    purchase = order(client)
    payment_id='provider-'+purchase['id']
    execute("UPDATE orders SET method='sbp',payment_id=? WHERE id=?", (payment_id,purchase['id']))
    current=external_payment(purchase['id'])
    sent=[]
    async def gateway(pid):
        sent.append(pid)
        return current
    monkeypatch.setattr(providers,'get_payment',gateway)
    for amount, method, status in [
        ('1.00','sbp', 'succeeded'),
        ('199.00','bank_card','succeeded'),
        ('199.00','sbp','pending'),
    ]:
        current['amount']['value']=amount
        current['payment_method']['type']=method
        current['status']=status
        res=notify(client,payment_id,paid=True,amount={'value':'199.00','currency':'RUB'})
        assert res.status_code == 503 if status == 'pending' and method == 'sbp' else res.status_code == 503
        assert one('SELECT status FROM orders')['status']=='pending'
    assert len(sent)==3
    current.update(external_payment(purchase['id']))
    success=notify(client,payment_id,object={'status':'paid'},event='payment.succeeded')
    assert success.status_code==200, success.text
    assert one('SELECT status FROM orders')['status']=='paid'
    assert one("SELECT count(*) AS n FROM order_milestones WHERE order_id=? AND event='paid'",(purchase['id'],))['n']==1
    assert notify(client,payment_id).status_code==200
    assert one("SELECT count(*) AS n FROM order_milestones WHERE order_id=? AND event='paid'",(purchase['id'],))['n']==1
    assert len(sent)==4


def test_webhook_ignores_unknown_ids_and_rejects_bad_input(client, monkeypatch):
    async def unexpected(*args):
        raise AssertionError('External provider must not be contacted')
    monkeypatch.setattr(providers,'get_payment',unexpected)
    assert notify(client,'provider-unknown').status_code==200
    assert notify(client,'provider-unknown',event='refund.succeeded').status_code==200
    assert client.post('/api/payments/yookassa/notify',content='not-json',
        headers={'Content-Type':'application/json'}).status_code==400
    assert client.post('/api/payments/yookassa/notify',json={'type':'notification',
        'event':'payment.succeeded','object':{'id':'https://evil.test/'}}).status_code==400
    assert client.post('/api/payments/yookassa/notify',content='x'*4097,
        headers={'Content-Type':'application/json'}).status_code==413
    assert client.post('/api/payments/yookassa/notify',content='{}',
        headers={'Content-Type':'text/plain'}).status_code==415


def test_verified_cancellation_never_makes_paid_order_active(client, monkeypatch):
    register(client)
    purchase=order(client)
    payment_id='provider-'+purchase['id']
    execute("UPDATE orders SET method='sbp',payment_id=? WHERE id=?", (payment_id,purchase['id']))
    verified=external_payment(purchase['id'],status='canceled',paid=False)
    async def gateway(_): return verified
    monkeypatch.setattr(providers,'get_payment',gateway)
    res=notify(client,payment_id,event='payment.canceled')
    assert res.status_code==200
    assert one('SELECT status FROM orders')['status']=='canceled'
    assert notify(client,payment_id,event='payment.succeeded').status_code==200
    assert one('SELECT status FROM orders')['status']=='canceled'


def test_sbp_history_and_diagnostics_can_find_paid_order(client, monkeypatch):
    from app import diagnostics, commerce_analytics
    register(client)
    purchase=order(client)
    execute("UPDATE orders SET method='sbp',payment_id=?,status='paid' WHERE id=?",
        ('provider-'+purchase['id'],purchase['id']))
    report=commerce_analytics.report()
    assert report['orders']['paid']==1
    assert report['orders']['confirmed_amount_by_currency']['RUB']==199
    assert report['events']['paid']==1
    monkeypatch.setattr(c,'PAYMENT_MODE','sbp')
    monkeypatch.setenv('YOOKASSA_SHOP_ID','test-id')
    monkeypatch.setenv('YOOKASSA_SECRET','secret-test')
    state=diagnostics.report(service.worker_health)
    assert state['settings']['payment_mode']=='sbp'
    assert state['settings']['payment_configured'] is True
