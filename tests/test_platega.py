import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app import config as c, platega, providers, service
from app.db import db, execute, init_db, one
from test_shop import client, register


@pytest.fixture
def merchant(client, monkeypatch):
    register(client)
    monkeypatch.setattr(c, 'PAYMENT_MODE', 'platega')
    monkeypatch.setattr(c, 'PUBLIC_URL', 'https://shop.example/shop')
    monkeypatch.setattr(c, 'SECURE_COOKIE', True)
    monkeypatch.setenv('PLATEGA_MERCHANT_ID', str(uuid4()))
    monkeypatch.setenv('PLATEGA_SECRET', 'synthetic-secret')
    return client


def test_shop_platega_checkout_and_issuance(merchant, monkeypatch):
    link_id, tx_id = str(uuid4()), str(uuid4())
    checkout_url = 'https://pay.example/pay'
    order_id = None
    attempts = []
    payment = {'id': tx_id, 'status': 'PENDING',
               'paymentDetails': {'amount': '199.00', 'currency': 'RUB'}}

    def gateway(request):
        nonlocal order_id
        assert request.url.host == 'api.payplatform.cc'
        assert request.headers['X-Secret'] == 'synthetic-secret'
        if request.method == 'POST':
            assert request.url.path == '/v2/payment-links'
            body = json.loads(request.content)
            order_id = body['orderId']
            assert request.headers['Idempotency-Key'] == body['payload'] == order_id
            assert body['minAmount'] == body['maxAmount'] == '199.00'
            assert body['currency'] == 'RUB' and body['usageMode'] == 1
            assert body['return'] == 'https://shop.example/shop/?payment=return'
            attempts.append((body, order_id))
            if len(attempts) == 1:
                raise httpx.ReadTimeout('response lost')
            return httpx.Response(201, json={'id': link_id, 'url': checkout_url})
        if request.url.path == '/v2/payment-links/' + link_id:
            return httpx.Response(200, json={'id': link_id, 'orderId': order_id, 'usageMode': 1,
                                            'active': False, 'consumedTransactionId': tx_id})
        assert request.url.path == '/transaction/' + tx_id
        return httpx.Response(200, json={'payload': order_id, **payment})

    make_client = httpx.AsyncClient
    monkeypatch.setattr(platega.httpx, 'AsyncClient',
                        lambda **options: make_client(transport=httpx.MockTransport(gateway), **options))

    body = {'plan': 'month', 'protocol': 'xui'}
    assert merchant.post('/api/orders', json=body).status_code == 422
    body['accept_terms'] = True
    assert merchant.post('/api/orders', json=body).status_code == 502
    checkout = merchant.post('/api/orders', json=body)
    assert checkout.status_code == 200, checkout.text
    assert checkout.json()['payment_url'] == checkout_url
    assert merchant.post('/api/orders', json=body).json()['id'] == order_id
    assert len(attempts) == 2 and attempts[0] == attempts[1]

    order = one('SELECT * FROM orders')
    asyncio.run(service.reconcile_payment(order))
    assert one('SELECT status FROM orders')['status'] == 'pending'
    payment['status'] = 'CONFIRMED'
    payment['paymentDetails']['amount'] = '1.00'
    with pytest.raises(platega.VerificationError):
        asyncio.run(service.reconcile_payment(order))
    assert one('SELECT status FROM orders')['status'] == 'pending'
    payment['paymentDetails']['amount'] = '199.00'
    payment['payload'] = str(uuid4())
    with pytest.raises(platega.VerificationError):
        asyncio.run(service.reconcile_payment(order))
    del payment['payload']
    asyncio.run(service.reconcile_payment(order))
    asyncio.run(service.reconcile_payment(order))
    assert one('SELECT status FROM orders')['status'] == 'paid'
    assert one("SELECT count(*) AS n FROM order_milestones WHERE event='paid'")['n'] == 1

    async def provision(sub):
        return 'vless://' + sub['id']

    monkeypatch.setattr(providers, 'provision', provision)
    asyncio.run(service.fulfill(order_id))
    assert merchant.get('/api/orders/' + order_id).json()['status'] == 'active'


def test_limits_and_credentials(merchant, monkeypatch):
    body = {'plan': 'month', 'protocol': 'xui', 'accept_terms': True}
    execute("UPDATE plans SET price=10001 WHERE id='month'")
    assert merchant.post('/api/orders', json=body).status_code == 422
    assert one('SELECT id FROM orders') is None
    execute("UPDATE plans SET price=10000 WHERE id='month'")
    monkeypatch.delenv('PLATEGA_SECRET')
    assert merchant.get('/api/catalog').json()['payment_available'] is False
    assert merchant.post('/api/orders', json=body).status_code == 503


def test_policy_migration_preserves_existing_row(client):
    with db() as conn:
        conn.execute('DROP TABLE shop_payment_policy')
        conn.execute("""CREATE TABLE shop_payment_policy (
            id INTEGER PRIMARY KEY, web_mode TEXT CHECK(web_mode IN ('inherit','manual','yookassa','sbp')),
            web_enabled INTEGER, stars_enabled INTEGER, manual_text TEXT, version INTEGER, updated INTEGER)""")
        conn.execute("INSERT INTO shop_payment_policy VALUES (1,'sbp',1,0,'existing',4,100)")
    init_db()
    assert tuple(one('SELECT * FROM shop_payment_policy').values()) == (1,'sbp',1,0,'existing',4,100)
    execute("UPDATE shop_payment_policy SET web_mode='platega'")
    init_db()
    assert one('SELECT web_mode FROM shop_payment_policy')['web_mode'] == 'platega'


def test_callback_auth_and_wakeup(merchant, monkeypatch):
    link_id = str(uuid4())

    async def create(order):
        return link_id, 'https://pay.example/pay'

    monkeypatch.setattr(platega, 'create', create)
    checkout = merchant.post('/api/orders', json={'plan': 'month', 'protocol': 'xui', 'accept_terms': True})
    order_id = checkout.json()['id']
    execute('UPDATE orders SET next_attempt=42')
    body = {'payload': order_id, 'paymentLinkId': link_id, 'status': 'CONFIRMED'}
    path = '/api/payments/platega/notify'
    assert merchant.post(path, json=body).status_code == 403
    headers = platega.credentials()
    assert merchant.post(path, headers=headers, json={**body, 'paymentLinkId': str(uuid4())}).status_code == 200
    assert one('SELECT next_attempt FROM orders')['next_attempt'] == 42
    for _ in range(2):
        assert merchant.post(path, headers=headers, json=body).status_code == 200
    assert one('SELECT status,next_attempt FROM orders') == {'status': 'pending', 'next_attempt': 0}
