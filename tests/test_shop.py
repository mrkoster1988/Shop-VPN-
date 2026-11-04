import asyncio, hashlib, hmac, json, time
from urllib.parse import urlencode
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from app import config as c, providers, service
from app.main import app, hits
from app.db import one, execute
from app.security import digest, telegram_user

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(c,'DB',tmp_path/'test.db')
    monkeypatch.setattr(c,'ADMIN_TOKEN','a'*48)
    monkeypatch.setattr(c,'PAYMENT_MODE','manual')
    monkeypatch.setattr(c,'PUBLIC_URL','http://testserver')
    monkeypatch.setattr(c,'SECURE_COOKIE',False)
    monkeypatch.setattr(c,'BOT_TOKEN','')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('BACKGROUND_TASKS','false')
    monkeypatch.setattr(providers,'available',lambda _:True)
    service.lock=asyncio.Lock()
    hits.clear()
    with TestClient(app) as test: yield test

def register(client,email='user@example.com'):
    assert client.post('/api/auth/register',json={'email':email,'password':'correct-horse-123'}).status_code==200

def order(client,protocol='xui'):
    r=client.post('/api/orders',json={'plan':'month','protocol':protocol})
    assert r.status_code==200,r.text
    return r.json()

def confirm(client,oid):
    return client.post(f'/api/admin/orders/{oid}/confirm',headers={'Authorization':'Bearer '+c.ADMIN_TOKEN})

def test_sessions_and_logout(client):
    register(client)
    cookie=client.cookies.get('session')
    assert cookie
    assert one('SELECT token FROM sessions WHERE token=?',(cookie,)) is None
    assert one('SELECT token FROM sessions WHERE token=?',(digest(cookie),))
    assert client.get('/api/me').json()['email']=='user@example.com'
    client.post('/api/auth/logout')
    assert client.get('/api/me').status_code==401

def test_password_and_duplicates(client):
    register(client)
    assert client.post('/api/auth/register',json={'email':'USER@example.com','password':'correct-horse-123'}).status_code==409
    assert client.post('/api/auth/login',json={'email':'user@example.com','password':'totally-wrong'}).status_code==401
    r=client.post('/api/auth/login',json={'email':'user@example.com','password':'correct-horse-123'})
    assert r.status_code==200
    assert 'httponly' in r.headers['set-cookie'].lower()

def test_csrf_and_admin_boundary(client):
    register(client)
    assert client.post('/api/orders',json={'plan':'month','protocol':'xui'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/admin/overview').status_code==403
    assert client.get('/api/admin/overview',headers={'Authorization':'Bearer wrong'}).status_code==403

def test_catalog_and_unconfigured_provider(client,monkeypatch):
    assert client.get('/api/catalog').json()['plans'][0]['price']==199
    monkeypatch.setattr(providers,'available',lambda _:False)
    register(client)
    assert client.post('/api/orders',json={'plan':'month','protocol':'wg'}).status_code==503

def test_server_price_and_dedup(client):
    register(client)
    first=order(client)
    second=client.post('/api/orders',json={'plan':'month','protocol':'xui','amount':1}).json()
    assert first['id']==second['id']
    assert first['amount']==second['amount']==199
    assert one('SELECT count(*) AS n FROM orders')['n']==1

def test_ownership(client):
    register(client)
    first=order(client)
    execute('INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret) VALUES (?,?,?,?,?,?)',('private-id',1,'xui',int(time.time())+1000,'active',c.encrypt('vless://secret')))
    client.post('/api/auth/logout')
    register(client,'second@example.com')
    assert client.get('/api/orders/'+first['id']).status_code==404
    for suffix in ('config','qr','download'):
        assert client.get('/api/subscriptions/private-id/'+suffix).status_code==404

def test_confirmation_renewal_encryption_and_qr(client,monkeypatch):
    calls=[]
    async def provision(sub):
        calls.append(dict(sub)); return 'vless://'+sub['id']
    monkeypatch.setattr(providers,'provision',provision)
    register(client)
    first=order(client)
    assert confirm(client,first['id']).status_code==200
    assert confirm(client,first['id']).status_code==200
    asyncio.run(service.fulfill(first['id']))
    asyncio.run(service.fulfill(first['id']))
    assert len(calls)==1
    expiry=one('SELECT expires FROM subscriptions')['expires']
    assert expiry>time.time()+29*86400
    assert one('SELECT count(*) AS n FROM audit')['n']==1
    second=order(client); confirm(client,second['id']); asyncio.run(service.fulfill(second['id']))
    assert one('SELECT expires FROM subscriptions')['expires']==expiry+30*86400
    assert calls[0]['id']==calls[1]['id']
    assert one('SELECT secret FROM subscriptions')['secret']!='vless://'+calls[0]['id']
    sid=calls[0]['id']
    assert client.get(f'/api/subscriptions/{sid}/qr').headers['content-type']=='image/png'
    assert client.get(f'/api/subscriptions/{sid}/config').headers['cache-control']=='no-store'

def test_provider_retry_keeps_identity_and_expiry(client,monkeypatch):
    calls=[]
    async def provision(sub):
        calls.append(dict(sub))
        if len(calls)==1: raise TimeoutError('private remote response')
        return 'real-config'
    monkeypatch.setattr(providers,'provision',provision)
    register(client); first=order(client); confirm(client,first['id'])
    asyncio.run(service.fulfill(first['id']))
    failed=one('SELECT * FROM orders')
    assert failed['status']=='paid' and failed['attempts']==1 and failed['error']=='TimeoutError'
    execute('UPDATE orders SET next_attempt=0')
    asyncio.run(service.fulfill(first['id']))
    assert one('SELECT status FROM orders')['status']=='active'
    assert calls[0]['id']==calls[1]['id'] and calls[0]['expires']==calls[1]['expires']

def test_expiry(client,monkeypatch):
    register(client)
    execute('INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret) VALUES (?,?,?,?,?,?)',('expired',1,'wg',int(time.time())-1,'active',c.encrypt('private')))
    disabled=[]
    async def disable(sub): disabled.append(sub['id'])
    monkeypatch.setattr(providers,'disable',disable)
    assert client.get('/api/subscriptions/expired/download').status_code==404
    asyncio.run(service.tick())
    assert disabled==['expired'] and one('SELECT state FROM subscriptions')['state']=='expired'

def test_telegram_signature_age_and_tampering(client,monkeypatch):
    monkeypatch.setattr(c,'BOT_TOKEN','test-token')
    def signed(age=0):
        data={'auth_date':str(int(time.time())-age),'user':json.dumps({'id':12345}),'query_id':'q'}
        check='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
        key=hmac.new(b'WebAppData',b'test-token',hashlib.sha256).digest()
        data['hash']=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
        return urlencode(data)
    assert telegram_user(signed())['id']==12345
    assert client.post('/api/auth/telegram',json={'init_data':signed()}).status_code==200
    assert client.get('/api/me').json()['telegram_id']==12345
    assert client.post('/api/auth/telegram',json={'init_data':signed(4000)}).status_code==401
    assert client.post('/api/auth/telegram',json={'init_data':signed()+'x'}).status_code==401

def test_single_use_ticket(client):
    register(client); url=service.ticket(1); client.post('/api/auth/logout')
    assert client.get(url,follow_redirects=False).status_code==303
    assert client.get('/api/me').status_code==200
    assert client.get(url,follow_redirects=False).status_code==401

def test_payment_verification(client,monkeypatch):
    register(client); first=order(client)
    execute("UPDATE orders SET method='yookassa',payment_id=? WHERE id=?",('provider-id',first['id']))
    payment={'id':'provider-id','metadata':{'order_id':first['id']},'amount':{'value':'1.00','currency':'RUB'},'status':'succeeded','paid':True}
    async def get_payment(_): return payment
    monkeypatch.setattr(providers,'get_payment',get_payment)
    with pytest.raises(RuntimeError): asyncio.run(service.reconcile_payment(one('SELECT * FROM orders')))
    assert one('SELECT status FROM orders')['status']=='pending'
    payment['amount']['value']='199.00'
    asyncio.run(service.reconcile_payment(one('SELECT * FROM orders')))
    assert one('SELECT status FROM orders')['status']=='paid'
    assert confirm(client,first['id']).status_code==409

def test_cancellation(client):
    register(client); first=order(client)
    r=client.post('/api/admin/orders/'+first['id']+'/reject',headers={'Authorization':'Bearer '+c.ADMIN_TOKEN})
    assert r.status_code==200 and confirm(client,first['id']).status_code==409

def test_frontend(client):
    for path in ('/','/admin','/static/app.js','/static/style.css'): assert client.get(path).status_code==200
    assert client.get('/health').json()['status']=='ok'


def test_stars_verified_checkout_and_replay(client,monkeypatch):
    monkeypatch.setattr(c,'BOT_TOKEN','telegram-token')
    monkeypatch.setenv('TERMS_TEXT','Test merchant terms')
    user=service.telegram_account(777)
    url=service.ticket(user['id']); client.get(url)
    async def invoice(order): return 'https://t.me/$testinvoice'
    monkeypatch.setattr(providers,'stars_invoice',invoice)
    catalog=client.get('/api/catalog').json()
    assert catalog['currency']=='XTR' and catalog['plans'][0]['price']==150
    assert client.post('/api/orders',json={'plan':'month','protocol':'wg'}).status_code==422
    result=client.post('/api/orders',json={'plan':'month','protocol':'wg','accept_terms':True})
    assert result.status_code==200,result.text
    first=result.json()
    assert first['amount']==150 and first['method']=='stars' and first['currency']=='XTR'
    assert confirm(client,first['id']).status_code==409
    query={'id':'checkout-1','from':{'id':777},'invoice_payload':first['id'],'currency':'XTR','total_amount':150}
    assert not service.reserve_checkout({**query,'from':{'id':888}})
    assert not service.reserve_checkout({**query,'total_amount':1})
    assert service.reserve_checkout(query)
    assert service.reserve_checkout(query)
    assert not service.reserve_checkout({**query,'id':'checkout-2'})
    assert one('SELECT status FROM orders')['status']=='checkout'
    payment={'invoice_payload':first['id'],'currency':'XTR','total_amount':150,'telegram_payment_charge_id':'charge-1'}
    with pytest.raises(RuntimeError): service.record_stars(888,payment)
    service.record_stars(777,payment)
    assert one('SELECT status FROM orders')['status']=='paid'
    async def provision(sub): return 'wg-config'
    monkeypatch.setattr(providers,'provision',provision)
    asyncio.run(service.fulfill(first['id']))
    expiry=one('SELECT expires FROM subscriptions')['expires']
    service.record_stars(777,payment)
    assert one('SELECT status FROM orders')['status']=='active'
    assert one('SELECT expires FROM subscriptions')['expires']==expiry


def test_shop_prefix_routes_sessions_and_login(client, monkeypatch):
    from app.main import create_app
    monkeypatch.setattr(c, 'PUBLIC_URL', 'http://testserver/shop')
    monkeypatch.setattr(c, 'BASE_PATH', '/shop')
    monkeypatch.setattr(c, 'COOKIE_PATH', '/shop/')
    with TestClient(create_app('/shop')) as shop:
        response = shop.get('/shop', follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == 'http://testserver/shop/'
        for page in ('/shop/', '/shop/admin'):
            response = shop.get(page)
            assert response.status_code == 200
            assert 'content="/shop"' in response.text
            assert 'href="/shop/static/style.css"' in response.text
            assert 'href="/shop/"' in response.text
            assert '__BASE_PATH__' not in response.text
        assert shop.get('/shop/static/app.js').status_code == 200
        assert shop.get('/shop/health').json() == {'status': 'ok'}
        assert shop.get('/shop/api/catalog').status_code == 200
        headers = {'Authorization': 'Bearer ' + c.ADMIN_TOKEN, 'Origin': 'http://testserver'}
        listed = shop.get('/shop/api/admin/plans', headers=headers)
        assert listed.status_code == 200
        plan = listed.json()['plans'][0]
        payload = {k: plan[k] for k in ('name', 'days', 'price', 'stars', 'enabled', 'version')}
        assert shop.put('/shop/api/admin/plans/month', headers=headers, json=payload).status_code == 200
        created = shop.post('/shop/api/admin/plans', headers=headers,
            json={'id':'prefix-plan','name':'Новый тариф','days':2,'price':150,'stars':100})
        assert created.status_code == 201
        assert created.json()['enabled'] is False
        assert shop.get('/').status_code == 404
        assert shop.get('/api/catalog').status_code == 404
        response = shop.post('/shop/api/auth/register',
            json={'email': 'prefix@example.com', 'password': 'correct-horse-123'},
            headers={'Origin': 'http://testserver'})
        assert response.status_code == 200
        assert 'Path=/shop/' in response.headers['set-cookie']
        assert shop.get('/shop/api/me').json()['email'] == 'prefix@example.com'
        assert shop.post('/shop/api/auth/logout', json={},
            headers={'Origin': 'https://evil.example'}).status_code == 403
        response = shop.post('/shop/api/auth/logout', json={},
            headers={'Origin': 'http://testserver'})
        assert response.status_code == 200
        assert 'Path=/shop/' in response.headers['set-cookie']
        assert shop.get('/shop/api/me').status_code == 401
        url = service.ticket(one("SELECT id FROM users WHERE email='prefix@example.com'")['id'])
        assert url.startswith('http://testserver/shop/login?ticket=')
        response = shop.get(url, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/shop/'
        assert response.headers['cache-control'] == 'no-store'
        assert shop.get('/shop/api/me').status_code == 200
        assert shop.get(url).status_code == 401


def test_shop_prefix_private_downloads(client, monkeypatch):
    from app.main import create_app
    monkeypatch.setattr(c, 'BASE_PATH', '/shop')
    monkeypatch.setattr(c, 'COOKIE_PATH', '/shop/')
    with TestClient(create_app('/shop')) as shop:
        response = shop.post('/shop/api/auth/register',
            json={'email': 'config@example.com', 'password': 'correct-horse-123'})
        assert response.status_code == 200
        uid = one("SELECT id FROM users WHERE email='config@example.com'")['id']
        execute('INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret) VALUES (?,?,?,?,?,?)',
            ('prefix-config', uid, 'xui', int(time.time())+1000, 'active', c.encrypt('vless://prefix-secret')))
        for suffix in ('config', 'qr', 'download'):
            response = shop.get('/shop/api/subscriptions/prefix-config/' + suffix)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
        shop.post('/shop/api/auth/logout')
        for suffix in ('config', 'qr', 'download'):
            assert shop.get('/shop/api/subscriptions/prefix-config/' + suffix).status_code == 401


def plan_edit(client, **changes):
    headers = {'Authorization': 'Bearer ' + c.ADMIN_TOKEN}
    plan = client.get('/api/admin/plans', headers=headers).json()['plans'][0]
    fields = ('name', 'days', 'price', 'stars', 'enabled', 'version')
    return {**{key: plan[key] for key in fields}, **changes}


def save_plan(client, data, plan_id='month'):
    return client.put('/api/admin/plans/' + plan_id, json=data,
                      headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN})


def test_tariff_admin_access_and_origin(client):
    payload = plan_edit(client, price=349)
    assert client.get('/api/admin/plans').status_code == 403
    register(client)
    assert client.put('/api/admin/plans/month', json=payload).status_code == 403
    result = client.put('/api/admin/plans/month', json=payload,
        headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN, 'Origin': 'https://evil.example'})
    assert result.status_code == 403
    assert one("SELECT price FROM plans WHERE id='month'")['price'] == 199
    assert save_plan(client, payload, 'unknown').status_code == 404


def test_tariff_changes_survive_restart_and_are_audited(client):
    from app.db import init_db
    payload = plan_edit(client, name='  Персональный  ', price=349, stars=251, days=45)
    result = save_plan(client, payload)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['name'] == 'Персональный' and saved['version'] == 2
    public = client.get('/api/catalog').json()['plans'][0]
    assert (public['name'], public['price'], public['days'], public['version']) == ('Персональный', 349, 45, 2)
    init_db()
    assert client.get('/api/catalog').json()['plans'][0] == public
    audit = one('SELECT * FROM plan_changes')
    assert json.loads(audit['before_data'])['price'] == 199
    assert json.loads(audit['after_data'])['stars'] == 251


def test_tariff_edit_conflict_and_noop(client):
    before = plan_edit(client)
    assert save_plan(client, before).json()['version'] == 1
    assert one('SELECT count(*) AS n FROM plan_changes')['n'] == 0
    changed = save_plan(client, {**before, 'price': 299})
    assert changed.status_code == 200
    assert save_plan(client, {**before, 'price': 999}).status_code == 409
    assert one("SELECT price FROM plans WHERE id='month'")['price'] == 299
    assert one('SELECT count(*) AS n FROM plan_changes')['n'] == 1


@pytest.mark.parametrize('changes', [
    {'name': '   '}, {'name': 'a'*81}, {'name': 'bad\nname'}, {'days': 0}, {'days': 3651},
    {'price': 0}, {'price': 1000001}, {'price': 2.5}, {'stars': 0}, {'stars': True},
    {'enabled': 'false'}, {'version': 0}, {'id': 'other'},
])
def test_tariff_invalid_edits_cannot_change_catalog(client, changes):
    result = save_plan(client, plan_edit(client, **changes))
    assert result.status_code == 422
    assert one("SELECT price,version FROM plans WHERE id='month'") == {'price': 199, 'version': 1}
    assert one('SELECT count(*) AS n FROM plan_changes')['n'] == 0


def test_tariff_price_quote_and_existing_order_snapshot(client, monkeypatch):
    register(client)
    original = client.post('/api/orders', json={'plan': 'month', 'protocol': 'xui', 'plan_version': 1}).json()
    assert original['amount'] == 199 and original['days'] == 30
    assert save_plan(client, plan_edit(client, price=499, days=60, name='Новый тариф')).status_code == 200
    assert client.post('/api/orders', json={'plan': 'month', 'protocol': 'xui', 'plan_version': 1}).status_code == 409
    assert client.post('/api/orders', json={'plan': 'month', 'protocol': 'xui'}).status_code == 409
    assert one('SELECT count(*) AS n FROM orders')['n'] == 1
    new = client.post('/api/orders', json={'plan': 'month', 'protocol': 'xui', 'plan_version': 2, 'amount': 1}).json()
    assert (new['amount'], new['days'], new['plan_name']) == (499, 60, 'Новый тариф')
    assert new['id'] != original['id']
    assert client.post('/api/orders', json={'plan': 'month', 'protocol': 'xui', 'plan_version': 2}).json()['id'] == new['id']
    assert client.get('/api/orders/' + original['id']).json()['amount'] == 199
    async def provision(sub): return 'vless://old-order'
    monkeypatch.setattr(providers, 'provision', provision)
    assert confirm(client, original['id']).status_code == 200
    asyncio.run(service.fulfill(original['id']))
    expiry = one('SELECT expires FROM subscriptions')['expires']
    assert time.time() + 29*86400 < expiry < time.time() + 31*86400
    old = one('SELECT * FROM orders WHERE id=?', (original['id'],))
    assert (old['amount'], old['days'], old['plan_name'], old['plan_version']) == (199, 30, 'На месяц', 1)


def test_hidden_tariffs_keep_pending_orders_payable(client):
    register(client)
    original = order(client)
    assert save_plan(client, plan_edit(client, enabled=False)).status_code == 200
    assert 'month' not in [p['id'] for p in client.get('/api/catalog').json()['plans']]
    assert client.post('/api/orders', json={'plan': 'month', 'protocol': 'xui'}).status_code == 409
    assert client.get('/api/orders/' + original['id']).status_code == 200
    assert confirm(client, original['id']).status_code == 200
    for plan_id in ('quarter', 'year'):
        row = one('SELECT * FROM plans WHERE id=?', (plan_id,))
        payload = {k: row[k] for k in ('name', 'days', 'price', 'stars', 'version')}
        assert save_plan(client, {**payload, 'enabled': False}, plan_id).status_code == 200
    assert client.get('/api/catalog').json()['plans'] == []
    assert save_plan(client, plan_edit(client, enabled=True)).status_code == 200
    assert client.get('/api/catalog').json()['plans'][0]['id'] == 'month'


def test_telegram_uses_edited_stars_price(client, monkeypatch):
    monkeypatch.setattr(c, 'BOT_TOKEN', 'telegram-token')
    monkeypatch.setenv('TERMS_TEXT', 'Merchant terms')
    assert save_plan(client, plan_edit(client, stars=271, days=15)).status_code == 200
    user = service.telegram_account(123456)
    client.get(service.ticket(user['id']))
    assert client.get('/api/catalog').json()['plans'][0]['price'] == 271
    seen = []
    async def invoice(order):
        seen.append(order); return 'https://t.me/$edited-tariff'
    monkeypatch.setattr(providers, 'stars_invoice', invoice)
    result = client.post('/api/orders', json={'plan':'month', 'protocol':'wg', 'plan_version':2, 'accept_terms':True})
    assert result.status_code == 200, result.text
    assert (seen[0]['amount'], seen[0]['currency'], seen[0]['days']) == (271, 'XTR', 15)


def test_editable_tariff_migrates_legacy_orders(tmp_path, monkeypatch):
    import sqlite3
    from app.db import init_db
    path = tmp_path / 'legacy.db'
    monkeypatch.setattr(c, 'DB', path)
    with sqlite3.connect(path) as conn:
        conn.executescript('''
        CREATE TABLE orders (
          id TEXT PRIMARY KEY, user_id INTEGER, protocol TEXT, plan TEXT, days INTEGER,
          amount INTEGER, currency TEXT, method TEXT, status TEXT, created INTEGER,
          payment_id TEXT, payment_url TEXT);
        INSERT INTO orders VALUES ('old', 1, 'wg', 'month', 30, 199, 'RUB', 'yookassa',
          'pending', 12345, 'provider-charge', 'https://payment.example');
        ''')
    init_db()
    old = one("SELECT * FROM orders WHERE id='old'")
    assert (old['amount'], old['days'], old['payment_id'], old['status']) == (199, 30, 'provider-charge', 'pending')
    assert old['plan_version'] == 1 and old['plan_name'] == 'На месяц'
    init_db()
    assert one('SELECT count(*) AS n FROM plans')['n'] == 3
    assert one('SELECT count(*) AS n FROM orders')['n'] == 1


def new_plan(client, **changes):
    return client.post('/api/admin/plans', headers={'Authorization': 'Bearer ' + c.ADMIN_TOKEN},
        json={'id': 'week', 'name': 'На неделю', 'days': 7, 'price': 99, 'stars': 75, **changes})


def test_custom_tariff_starts_hidden_survives_restart_and_has_audit(client):
    from app.db import init_db
    response = new_plan(client)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created['id'] == 'week' and created['version'] == 1 and created['enabled'] is False
    assert created['position'] == 3
    assert 'week' not in [p['id'] for p in client.get('/api/catalog').json()['plans']]
    init_db()
    saved = one("SELECT * FROM plans WHERE id='week'")
    assert (saved['name'], saved['days'], saved['price'], saved['stars']) == ('На неделю', 7, 99, 75)
    audit = one("SELECT * FROM plan_changes WHERE plan_id='week'")
    assert json.loads(audit['before_data']) is None
    assert json.loads(audit['after_data'])['enabled'] == 0
    fields = ('name', 'days', 'price', 'stars', 'version')
    result = save_plan(client, {**{k: saved[k] for k in fields}, 'enabled': True}, 'week')
    assert result.status_code == 200
    assert client.get('/api/catalog').json()['plans'][-1]['id'] == 'week'
    assert new_plan(client, id='weekend').json()['position'] == 4


def test_custom_tariff_creation_requires_admin_and_same_origin(client):
    payload = {'id': 'day', 'name': 'На день', 'days': 1, 'price': 300, 'stars': 200}
    assert client.post('/api/admin/plans', json=payload).status_code == 403
    register(client)
    assert client.post('/api/admin/plans', json=payload).status_code == 403
    assert client.post('/api/admin/plans', json=payload,
        headers={'Authorization':'Bearer '+c.ADMIN_TOKEN, 'Origin':'https://evil.example'}).status_code == 403
    assert one('SELECT count(*) AS n FROM plans')['n'] == 3


def test_custom_tariff_duplicate_does_not_replace_existing(client):
    assert new_plan(client).status_code == 201
    assert new_plan(client, price=1).status_code == 409
    assert new_plan(client, id='month', price=1).status_code == 409
    assert one("SELECT price FROM plans WHERE id='week'")['price'] == 99
    assert one("SELECT price FROM plans WHERE id='month'")['price'] == 199
    assert one('SELECT count(*) AS n FROM plan_changes')['n'] == 1


@pytest.mark.parametrize('changes', [
    {'id': '../admin'}, {'id': 'UPPER'}, {'id': 'two words'}, {'id': 'a'*33}, {'id': ''},
    {'id': '1day'}, {'id': 'день'}, {'name': '  '}, {'price': -1}, {'days': 1.5},
    {'enabled': 1}, {'version': 99},
])
def test_custom_tariff_creation_rejects_invalid_fields(client, changes):
    assert new_plan(client, **changes).status_code == 422
    assert one('SELECT count(*) AS n FROM plans')['n'] == 3
    assert one('SELECT count(*) AS n FROM plan_changes')['n'] == 0


def test_custom_tariff_can_be_purchased_and_fulfilled(client, monkeypatch):
    assert new_plan(client, id='day', name='На день', days=1, price=300, enabled=True).status_code == 201
    register(client)
    request = {'plan':'day', 'protocol':'wg', 'plan_version':1}
    result = client.post('/api/orders', json=request)
    assert result.status_code == 200, result.text
    created = result.json()
    assert (created['amount'], created['days'], created['plan_name']) == (300, 1, 'На день')
    assert client.post('/api/orders', json=request).json()['id'] == created['id']
    async def provision(sub): return 'wireguard-day-config'
    monkeypatch.setattr(providers, 'provision', provision)
    assert confirm(client, created['id']).status_code == 200
    asyncio.run(service.fulfill(created['id']))
    expiry = one('SELECT expires FROM subscriptions')['expires']
    assert time.time()+23*3600 < expiry < time.time()+25*3600


def test_custom_tariff_stars_uses_its_own_price(client, monkeypatch):
    assert new_plan(client, enabled=True).status_code == 201
    monkeypatch.setattr(c, 'BOT_TOKEN', 'telegram-token')
    monkeypatch.setenv('TERMS_TEXT', 'Merchant terms')
    user = service.telegram_account(87654)
    client.get(service.ticket(user['id']))
    assert client.get('/api/catalog').json()['plans'][-1]['price'] == 75
    seen = []
    async def invoice(order):
        seen.append(order); return 'https://t.me/$custom'
    monkeypatch.setattr(providers, 'stars_invoice', invoice)
    result = client.post('/api/orders', json={'plan':'week','protocol':'xui','accept_terms':True,'plan_version':1})
    assert result.status_code == 200, result.text
    assert (seen[0]['plan'], seen[0]['days'], seen[0]['amount'], seen[0]['currency']) == ('week', 7, 75, 'XTR')
