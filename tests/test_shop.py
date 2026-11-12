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
    monkeypatch.setattr(providers,'available',lambda _,user_id=None:True)
    service.lock=asyncio.Lock()
    hits.clear()
    monkeypatch.setenv('TERMS_TEXT','Тестовые условия продажи')
    with TestClient(app) as test:
        for protocol, profile in [('xui',{'url':'https://panel.example','username':'test','password':'private','inbound_id':1,'link_template':'vless://{uuid}@vpn.example','flow':''}),
                                  ('wg',{'url':'https://agent.example','token':'a'*32})]:
            execute("INSERT INTO servers(id,name,protocol,state,health,settings,created) VALUES (?,?,?,'active','ok',?,0)",
                    ('test-'+protocol,'Test '+protocol,protocol,c.encrypt(json.dumps(profile))))
        yield test

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
    assert client.get('/api/admin/overview').status_code==401
    assert client.get('/api/admin/overview',headers={'Authorization':'Bearer wrong'}).status_code==401

def test_catalog_and_unconfigured_provider(client,monkeypatch):
    assert client.get('/api/catalog').json()['plans'][0]['price']==199
    monkeypatch.setattr(providers,'available',lambda _,user_id=None:False)
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
    execute('UPDATE store_settings SET terms_text=? WHERE id=1', ('Test merchant terms',))
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
        registry = shop.get('/shop/api/admin/servers', headers=headers)
        assert registry.status_code == 200
        assert len(registry.json()['servers']) == 2
        assert all('settings' not in row for row in registry.json()['servers'])
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
        users = shop.get('/shop/api/admin/users', headers=headers).json()
        uid = users['users'][0]['id']
        assert users['total'] == 1
        assert shop.get('/shop/api/admin/orders', headers=headers).json()['total'] == 0
        assert shop.get('/shop/api/admin/users/' + str(uid), headers=headers).json()['user']['email'] == 'prefix@example.com'
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
    assert client.get('/api/admin/plans').status_code==401
    register(client)
    assert client.put('/api/admin/plans/month', json=payload).status_code==401
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
    execute('UPDATE store_settings SET terms_text=? WHERE id=1', ('Merchant terms',))
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
    assert client.post('/api/admin/plans', json=payload).status_code==401
    register(client)
    assert client.post('/api/admin/plans', json=payload).status_code==401
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
    execute('UPDATE store_settings SET terms_text=? WHERE id=1', ('Merchant terms',))
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


def admin_headers():
    return {'Authorization': 'Bearer ' + c.ADMIN_TOKEN}


def add_customer(email=None, telegram_id=None, created=100):
    return execute('INSERT INTO users(email,password,telegram_id,created) VALUES (?,?,?,?)',
                   (email, 'private-password-hash', telegram_id, created))


def test_admin_users_require_admin_access(client):
    uid = add_customer('customer@example.com')
    for path in ('/api/admin/users', '/api/admin/users/' + str(uid)):
        assert client.get(path).status_code==401
        assert client.get(path, headers={'Authorization':'Bearer wrong'}).status_code==401
    register(client)
    assert client.get('/api/admin/users/' + str(uid)).status_code==401


def test_admin_user_search_email_ids_and_literal_characters(client):
    first = add_customer('alice@example.com')
    second = add_customer('literal_%@example.com')
    third = add_customer(telegram_id=987654321)
    cases = [('ALICE@', [first]), ('  alice@  ', [first]), ('987654321', [third]),
             (str(second), [second]), ('_%', [second]), ('%', [second]),
             ("' OR 1=1 --", []), ('missing@example.com', []), ('9'*80, [])]
    for query, expected in cases:
        result = client.get('/api/admin/users', params={'q':query}, headers=admin_headers())
        assert result.status_code == 200, result.text
        assert [u['id'] for u in result.json()['users']] == expected
    assert client.get('/api/admin/users', params={'q':'\\'}, headers=admin_headers()).json()['total'] == 0


def test_admin_users_pagination_is_stable_and_bounded(client):
    ids = [add_customer('user'+str(i)+'@example.com') for i in range(6)]
    first = client.get('/api/admin/users?page_size=2', headers=admin_headers()).json()
    second = client.get('/api/admin/users?page_size=2&page=2', headers=admin_headers()).json()
    third = client.get('/api/admin/users?page_size=2&page=3', headers=admin_headers()).json()
    assert first['total'] == 6
    assert [u['id'] for page in (first, second, third) for u in page['users']] == ids[::-1]
    assert client.get('/api/admin/users?page=10', headers=admin_headers()).json()['users'] == []
    assert set(first['users'][0]) == {'id','email','telegram_id','created','active_subscriptions','orders_count'}


def test_admin_user_detail_contains_only_owned_records_and_no_secrets(client):
    uid = add_customer('alice@example.com')
    other = add_customer('other@example.com')
    now = int(time.time())
    for sid, user, state, expires in [('active',uid,'active',now+86400),
            ('expired',uid,'active',now-10), ('other-private',other,'active',now+86400)]:
        protocol = 'wg' if sid == 'expired' else 'xui'
        execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires,secret) VALUES (?,?,?,?,?,?)',
                (sid,user,protocol,state,expires,'private-vpn-config'))
    for oid, user in [('own-order',uid),('other-order',other)]:
        execute('INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,currency,method,status,created,payment_id,payment_url,checkout_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (oid,user,'xui','month','На месяц',30,199,'RUB','manual','pending',100,'payment-'+oid,'https://secret-payment.example','private-checkout'))
    result = client.get('/api/admin/users/' + str(uid), headers=admin_headers())
    assert result.status_code == 200, result.text
    data = result.json()
    assert result.headers['cache-control'] == 'no-store'
    assert data['user'] == {'id':uid,'email':'alice@example.com','telegram_id':None,'created':100}
    assert {s['id']:s['access_active'] for s in data['subscriptions']} == {'active':True,'expired':False}
    assert [o['id'] for o in data['orders']] == ['own-order'] and data['orders_total'] == 1
    assert 'private-' not in result.text and 'secret-payment' not in result.text
    assert 'payment_id' not in result.text and 'payment_url' not in result.text and 'checkout_id' not in result.text
    assert all(set(s) == {'id','protocol','expires','state','device_limit','access_active'} for s in data['subscriptions'])
    summary = client.get('/api/admin/users', params={'q':'alice@'}, headers=admin_headers()).json()['users'][0]
    assert summary['active_subscriptions'] == 1 and summary['orders_count'] == 1
    execute("UPDATE subscriptions SET state='pending',expires=? WHERE id='expired'", (now+86400,))
    data = client.get('/api/admin/users/' + str(uid), headers=admin_headers()).json()
    assert next(s for s in data['subscriptions'] if s['protocol']=='wg')['access_active'] is False


def test_admin_user_empty_and_missing_profiles(client):
    uid = add_customer(telegram_id=123456789)
    data = client.get('/api/admin/users/' + str(uid), headers=admin_headers()).json()
    assert data['orders'] == [] and data['subscriptions'] == [] and data['orders_total'] == 0
    assert client.get('/api/admin/users/999999', headers=admin_headers()).status_code == 404


def test_admin_user_orders_are_paginated_without_truncation(client):
    uid = add_customer('history@example.com')
    for index in range(5):
        execute('INSERT INTO orders(id,user_id,protocol,plan,days,amount,created) VALUES (?,?,?,?,?,?,?)',
                ('order-'+str(index),uid,'wg','month',30,199,100+index))
    def page(number):
        return client.get('/api/admin/users/' + str(uid),
            params={'orders_page_size':2,'orders_page':number}, headers=admin_headers()).json()
    pages = [page(number) for number in (1,2,3)]
    assert all(p['orders_total'] == 5 for p in pages)
    assert [o['id'] for p in pages for o in p['orders']] == ['order-'+str(i) for i in range(4,-1,-1)]
    assert page(4)['orders'] == []


@pytest.mark.parametrize('path', [
    '/api/admin/users?page=0', '/api/admin/users?page_size=101', '/api/admin/users?q='+'a'*101,
    '/api/admin/users/0', '/api/admin/users/-1', '/api/admin/users/999999999999999999999999',
    '/api/admin/users/1?orders_page=0', '/api/admin/users/1?orders_page_size=101',
])
def test_admin_user_query_validation(client, path):
    assert client.get(path, headers=admin_headers()).status_code == 422


def add_admin_order(uid, oid, status='pending', method='manual', protocol='xui', created=100, payment_id=None):
    return execute('INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,currency,method,status,created,payment_id,payment_url,checkout_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (oid,uid,protocol,'month','На месяц',30,199,'XTR' if method=='stars' else 'RUB',method,status,created,
         payment_id,'https://private-payment.example','private-checkout'))


def test_admin_orders_require_admin_and_hide_payment_details(client):
    uid = add_customer('buyer@example.com')
    add_admin_order(uid,'private-order',method='yookassa',payment_id='private-provider-id')
    assert client.get('/api/admin/orders').status_code==401
    register(client)
    assert client.get('/api/admin/orders').status_code==401
    response = client.get('/api/admin/orders',headers=admin_headers())
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'private-provider-id' not in response.text and 'private-payment' not in response.text
    assert 'private-checkout' not in response.text and 'private-password-hash' not in response.text
    item = response.json()['orders'][0]
    assert item['can_confirm'] is False and item['can_reject'] is False
    assert not {'payment_id','payment_url','checkout_id','receipt_email'} & item.keys()


def test_admin_orders_filters_combine_and_search_buyer_or_order(client):
    alice = add_customer('alice@example.com')
    tg = add_customer(telegram_id=987654321)
    other = add_customer('other@example.com')
    for args in [(alice,'manual-pending','pending','manual','xui'),(tg,'stars-pending','pending','stars','wg'),
                 (other,'yoo-pending','pending','yookassa','xui'),(alice,'paid-row','paid','manual','wg'),
                 (tg,'active-row','active','stars','xui'),(other,'canceled-row','canceled','manual','xui')]:
        add_admin_order(*args)
    cases = [({'status':'pending'},3),({'method':'manual'},3),({'protocol':'wg'},2),
             ({'q':'ALICE@'},2),({'q':str(alice)},2),({'q':'987654321'},2),({'q':'-pending'},3),
             ({'q':'987654321','status':'pending','method':'stars','protocol':'wg'},1),
             ({'status':'paid','method':'stars'},0)]
    for params, expected in cases:
        response = client.get('/api/admin/orders',params=params,headers=admin_headers())
        assert response.status_code == 200, response.text
        assert response.json()['total'] == expected
    specific = client.get('/api/admin/orders',params={'q':'stars-pending'},headers=admin_headers()).json()
    assert [o['id'] for o in specific['orders']] == ['stars-pending']


def test_admin_order_search_treats_special_characters_literally(client):
    uid = add_customer('literal_%@example.com')
    normal = add_customer('normal@example.com')
    add_admin_order(uid,'literal-order')
    add_admin_order(normal,'normal-order')
    for query in ('_%','%','_'):
        assert client.get('/api/admin/orders',params={'q':query},headers=admin_headers()).json()['total'] == 1
    for query in ("' OR 1=1 --",'\\','9'*80):
        assert client.get('/api/admin/orders',params={'q':query},headers=admin_headers()).json()['total'] == 0


def test_admin_orders_reach_beyond_legacy_200_limit(client):
    from app.db import db
    uid = add_customer('history@example.com')
    with db() as conn:
        conn.executemany('INSERT INTO orders(id,user_id,protocol,plan,days,amount,created) VALUES (?,?,?,?,?,?,?)',
            [('bulk-'+str(i).zfill(3),uid,'wg','month',30,199,100+i) for i in range(205)])
    pages = [client.get('/api/admin/orders',params={'page':p,'page_size':100},headers=admin_headers()).json() for p in (1,2,3)]
    assert all(p['total']==205 for p in pages)
    assert [len(p['orders']) for p in pages] == [100,100,5]
    ids = [o['id'] for page in pages for o in page['orders']]
    assert len(set(ids)) == 205 and ids[0] == 'bulk-204' and ids[-1] == 'bulk-000'
    assert client.get('/api/admin/orders?page=999',headers=admin_headers()).json()['orders'] == []


def test_admin_order_action_flags_follow_state_and_method(client, monkeypatch):
    register(client)
    created = order(client)
    def listed():
        return client.get('/api/admin/orders',params={'q':created['id']},headers=admin_headers()).json()['orders'][0]
    before = listed()
    assert before['can_confirm'] and before['can_reject'] and not before['can_retry']
    assert confirm(client,created['id']).status_code == 200
    pending = client.get('/api/admin/orders?status=pending',headers=admin_headers()).json()
    assert pending['orders'] == [] and pending['total'] == 0
    paid = listed()
    assert paid['can_retry'] and not paid['can_confirm'] and not paid['can_reject']
    async def provision(sub): return 'vless://confirmed'
    monkeypatch.setattr(providers,'provision',provision)
    asyncio.run(service.fulfill(created['id']))
    active = listed()
    assert not any(active[k] for k in ('can_confirm','can_reject','can_retry'))
    uid = one('SELECT id FROM users')['id']
    add_admin_order(uid,'stars-without-id',method='stars')
    add_admin_order(uid,'manual-with-provider-id',payment_id='provider-id')
    for oid in ('stars-without-id','manual-with-provider-id'):
        item = client.get('/api/admin/orders',params={'q':oid},headers=admin_headers()).json()['orders'][0]
        assert not item['can_confirm'] and not item['can_reject']
        assert confirm(client,oid).status_code == 409


@pytest.mark.parametrize('params', [
    {'status':'refunded'},{'method':'card'},{'protocol':'other'},{'page':0},
    {'page_size':101},{'page_size':0},{'q':'a'*101},{'page':'bad'},
])
def test_admin_order_filters_validate_inputs(client, params):
    assert client.get('/api/admin/orders',params=params,headers=admin_headers()).status_code == 422


@pytest.fixture
def registry_client(client, monkeypatch):
    from app import servers
    monkeypatch.setattr(providers,'available',servers.available)
    execute('DELETE FROM servers')
    return client


def server_body(protocol='xui', **changes):
    profile = {'url':'https://panel.example/private-path','username':'owner','password':'private-panel-password',
               'inbound_id':7,'link_template':'vless://{uuid}@vpn.example:443?security=tls','flow':''} if protocol=='xui' else {'url':'https://agent.example','token':'private-agent-token-'+'a'*32}
    return {'name':'Test server','location':'Финляндия','protocol':protocol,'settings':profile,**changes}


def server_create(client, protocol='xui', **changes):
    return client.post('/api/admin/servers',json=server_body(protocol,**changes),headers=admin_headers())


def server_patch(client, row, **changes):
    payload={key:row[key] for key in ('name','location','capacity','state','version')}
    return client.put('/api/admin/servers/'+row['id'],json={**payload,**changes},headers=admin_headers())


def server_enable(client, row, monkeypatch):
    async def probe(_): return None
    monkeypatch.setattr(providers,'probe_server',probe)
    checked=client.post('/api/admin/servers/'+row['id']+'/check',headers=admin_headers())
    assert checked.status_code==200 and checked.json()['health']=='ok'
    result=server_patch(client,checked.json(),state='active')
    assert result.status_code==200,result.text
    return result.json()


def test_server_registry_encrypted_and_hidden_from_responses(registry_client):
    client=registry_client
    response=server_create(client)
    assert response.status_code==201,response.text
    row=response.json()
    assert row['state']=='disabled' and row['health']=='unknown'
    assert not any(x in response.text for x in ('private-panel-password','private-path','owner','settings'))
    encrypted=one('SELECT settings FROM servers')['settings']
    assert 'private-panel-password' not in encrypted
    assert json.loads(c.decrypt(encrypted))['password']=='private-panel-password'
    listing=client.get('/api/admin/servers',headers=admin_headers())
    assert listing.headers['cache-control']=='no-store'
    assert 'private-panel-password' not in listing.text and 'private-path' not in listing.text
    assert one('SELECT action FROM server_changes')['action']=='created'


def test_server_registry_authentication_and_validation_redact_secrets(client):
    assert client.get('/api/admin/servers').status_code==401
    assert client.post('/api/admin/servers',json=server_body()).status_code==401
    row=server_create(client).json()
    assert client.put('/api/admin/servers/'+row['id'],json={}).status_code==401
    assert client.post('/api/admin/servers/'+row['id']+'/check').status_code==401
    invalid=server_body(settings=[{'password':'never-echo-this-password'}])
    response=client.post('/api/admin/servers',json=invalid,headers=admin_headers())
    assert response.status_code==422 and 'never-echo' not in response.text
    response=client.post('/api/admin/servers',json={**server_body(),'token':'never-echo-token'},headers=admin_headers())
    assert response.status_code==422 and 'never-echo-token' not in response.text
    assert client.post('/api/admin/servers',json=server_body(),headers={**admin_headers(),'Origin':'https://evil.example'}).status_code==403


@pytest.mark.parametrize('profile', [
    {'url':'http://8.8.8.8'}, {'url':'https://user:password@panel.example'},
    {'url':'https://panel.example?password=secret'}, {'url':'https://panel.example/#fragment'},
    {'inbound_id':0}, {'inbound_id':True}, {'link_template':'https://wrong.example'}, {'password':''},
])
def test_server_profiles_validate_without_echoing_input(registry_client, profile):
    body=server_body(); body['settings'].update(profile)
    response=registry_client.post('/api/admin/servers',json=body,headers=admin_headers())
    assert response.status_code==422
    assert 'private-panel-password' not in response.text
    assert one('SELECT count(*) AS n FROM servers')['n']==0


def test_server_requires_check_before_active_and_has_version_conflicts(registry_client,monkeypatch):
    row=server_create(registry_client).json()
    assert server_patch(registry_client,row,state='active').status_code==409
    active=server_enable(registry_client,row,monkeypatch)
    assert server_patch(registry_client,row,name='Stale edit').status_code==409
    assert providers.available('xui')
    assert one('SELECT count(*) AS n FROM server_changes')['n']==2
    async def broken(_): raise RuntimeError('must-not-leak-private-password')
    monkeypatch.setattr(providers,'probe_server',broken)
    failed=registry_client.post('/api/admin/servers/'+active['id']+'/check',headers=admin_headers())
    assert failed.json()['health']=='error' and 'must-not-leak' not in failed.text
    assert not providers.available('xui')


def test_servers_assign_once_and_renew_in_draining_mode(registry_client,monkeypatch):
    client=registry_client
    first=server_enable(client,server_create(client).json(),monkeypatch)
    second=server_enable(client,server_create(client,name='Second').json(),monkeypatch)
    register(client)
    calls=[]
    async def provision(sub): calls.append(dict(sub)); return 'vless://'+sub['id']
    monkeypatch.setattr(providers,'provision',provision)
    initial=order(client); confirm(client,initial['id']); asyncio.run(service.fulfill(initial['id']))
    sub=one('SELECT * FROM subscriptions'); pinned=sub['server_id']
    original=first if first['id']==pinned else second
    assert server_patch(client,original,state='draining').status_code==200
    other=second if second['id']!=pinned else first
    assert server_patch(client,other,state='disabled').status_code==200
    assert not providers.available('xui') and providers.available('xui',1)
    renewal=order(client); confirm(client,renewal['id']); asyncio.run(service.fulfill(renewal['id']))
    assert len(calls)==2 and all(call['server_id']==pinned for call in calls)
    assert one('SELECT expires FROM subscriptions')['expires']==sub['expires']+30*86400
    assert client.get('/api/catalog').json()['protocols'][0]['available'] is True
    client.post('/api/auth/logout')
    register(client,'new@example.com')
    assert client.post('/api/orders',json={'plan':'month','protocol':'xui'}).status_code==503


def test_failed_issue_retains_assignment_and_retries_same_server(registry_client,monkeypatch):
    client=registry_client
    server_enable(client,server_create(client).json(),monkeypatch)
    register(client)
    calls=[]
    async def fail(sub): calls.append(dict(sub)); raise RuntimeError('failure')
    monkeypatch.setattr(providers,'provision',fail)
    initial=order(client); confirm(client,initial['id']); asyncio.run(service.fulfill(initial['id']))
    bound=one('SELECT * FROM subscriptions')
    server_enable(client,server_create(client,name='Alternative').json(),monkeypatch)
    execute('UPDATE orders SET next_attempt=0 WHERE id=?',(initial['id'],))
    async def succeed(sub): calls.append(dict(sub)); return 'vless://same'
    monkeypatch.setattr(providers,'provision',succeed)
    asyncio.run(service.fulfill(initial['id']))
    assert calls[0]['id']==calls[1]['id'] and calls[0]['server_id']==calls[1]['server_id']==bound['server_id']
    assert calls[0]['expires']==calls[1]['expires']


def test_server_endpoint_cannot_change_with_bound_subscriptions(registry_client,monkeypatch):
    client=registry_client
    row=server_enable(client,server_create(client).json(),monkeypatch)
    uid=add_customer('bound@example.com')
    execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires,server_id) VALUES (?,?,?,?,?,?)',('bound',uid,'xui','active',int(time.time())+1000,row['id']))
    changed=server_body()['settings']; changed['url']='https://other.example'
    assert server_patch(client,row,state='disabled',settings=changed).status_code==409
    same=server_body()['settings']; same['password']='rotated-private-password'
    result=server_patch(client,row,state='disabled',settings=same)
    assert result.status_code==200 and result.json()['health']=='unknown'
    assert one('SELECT server_id FROM subscriptions')['server_id']==row['id']
    assert 'rotated-private-password' not in result.text


def test_server_capacity_blocks_only_new_assignments(registry_client,monkeypatch):
    client=registry_client
    row=server_enable(client,server_create(client,capacity=1).json(),monkeypatch)
    uid=add_customer('occupied@example.com')
    execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires,server_id) VALUES (?,?,?,?,?,?)',('occupied',uid,'xui','pending',0,row['id']))
    assert not providers.available('xui') and providers.available('xui',uid)
    view=client.get('/api/admin/servers',headers=admin_headers()).json()['servers'][0]
    assert view['occupied']==1 and view['bound_subscriptions']==1
    execute("UPDATE subscriptions SET state='expired' WHERE id='occupied'")
    assert providers.available('xui')


def test_legacy_server_import_pins_existing_subscriptions_once(tmp_path,monkeypatch):
    from app import servers
    from app.db import init_db
    monkeypatch.setattr(c,'DB',tmp_path/'legacy-servers.db')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    profile=server_body()['settings']
    for name,key in [('XUI_URL','url'),('XUI_USERNAME','username'),('XUI_PASSWORD','password'),('XUI_LINK_TEMPLATE','link_template')]: monkeypatch.setenv(name,profile[key])
    monkeypatch.setenv('XUI_INBOUND_ID','7'); monkeypatch.delenv('WG_AGENT_URL',raising=False)
    init_db(); uid=add_customer('legacy@example.com')
    execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires,secret) VALUES (?,?,?,?,?,?)',('old-key',uid,'xui','active',2000000000,c.encrypt('old-vless-config')))
    servers.import_environment()
    assert one('SELECT server_id FROM subscriptions')['server_id']=='legacy-xui'
    monkeypatch.setenv('XUI_URL','https://changed.example')
    servers.import_environment()
    row=one("SELECT * FROM servers WHERE id='legacy-xui'")
    assert servers.decode(row)['url']==profile['url']
    assert c.decrypt(one('SELECT secret FROM subscriptions')['secret'])=='old-vless-config'


def test_unbound_legacy_subscription_never_moves_to_new_server(registry_client,monkeypatch):
    client=registry_client
    server_enable(client,server_create(client).json(),monkeypatch)
    register(client)
    created=order(client); confirm(client,created['id'])
    execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires,secret) VALUES (?,?,?,?,?,?)',('unbound',1,'xui','active',2000000000,c.encrypt('old-config')))
    calls=[]
    async def provision(sub): calls.append(sub); return 'wrong'
    monkeypatch.setattr(providers,'provision',provision)
    asyncio.run(service.fulfill(created['id']))
    assert not calls
    assert one('SELECT status,error FROM orders')=={'status':'paid','error':'RuntimeError'}
    assert one('SELECT server_id FROM subscriptions')['server_id'] is None
    assert providers.available('xui',1) is False
    assert client.get('/api/admin/servers',headers=admin_headers()).json()['unassigned_subscriptions']==1


def test_server_probe_does_not_follow_redirects_or_disclose_errors(registry_client,monkeypatch):
    import httpx
    row=server_create(registry_client).json(); calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'location':'https://leak.example'})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kwargs:cls(transport=httpx.MockTransport(handler),**kwargs))
    result=registry_client.post('/api/admin/servers/'+row['id']+'/check',headers=admin_headers())
    assert result.json()['health']=='error' and result.json()['error_code']=='ProviderCheckFailed'
    assert len(calls)==1 and calls[0].url.host=='panel.example'
    assert 'private-panel-password' not in result.text and 'leak.example' not in result.text


def test_server_probe_cannot_mark_changed_profile_healthy(registry_client,monkeypatch):
    row=server_create(registry_client).json()
    async def changing(_): execute('UPDATE servers SET version=version+1 WHERE id=?',(row['id'],))
    monkeypatch.setattr(providers,'probe_server',changing)
    response=registry_client.post('/api/admin/servers/'+row['id']+'/check',headers=admin_headers())
    assert response.status_code==409
    assert one('SELECT health FROM servers')['health']=='unknown'


def test_wireguard_server_validates_token_and_allows_private_http(registry_client):
    body=server_body('wg'); body['settings']['token']='short-private-token'
    response=registry_client.post('/api/admin/servers',json=body,headers=admin_headers())
    assert response.status_code==422 and 'short-private-token' not in response.text
    body['settings']={'url':'http://127.0.0.1:8081','token':'a'*32}
    response=registry_client.post('/api/admin/servers',json=body,headers=admin_headers())
    assert response.status_code==201 and response.json()['protocol']=='wg'
    assert providers.available('wg') is False


def test_legacy_import_missing_profile_rolls_back_and_can_be_retried(tmp_path,monkeypatch):
    from app import servers
    from app.db import init_db
    monkeypatch.setattr(c,'DB',tmp_path/'missing-profile.db')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.delenv('XUI_URL',raising=False); monkeypatch.delenv('WG_AGENT_URL',raising=False)
    init_db(); uid=add_customer('old@example.com')
    execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires) VALUES (?,?,?,?,?)',('old',uid,'xui','active',2000000000))
    with pytest.raises(RuntimeError,match='Original provider settings'):
        servers.import_environment()
    assert one("SELECT value FROM meta WHERE key='server_registry_initialized'") is None
    assert one('SELECT server_id FROM subscriptions')['server_id'] is None


def test_clients_profile_api_redacts_credentials_and_preserves_mode(registry_client,monkeypatch):
    client=registry_client
    settings={'api':'clients','url':'https://panel.example/private-api-path','token':'private-api-token-'+'a'*32,
              'inbound_id':7,'link_template':'vless://{uuid}@vpn.example:443'}
    created=server_create(client,settings=settings)
    assert created.status_code==201,created.text
    row=created.json()
    assert row['profile']=='clients_vless' and row['state']=='disabled'
    assert all(secret not in created.text for secret in ('private-api-path','private-api-token','settings'))
    assert 'private-api-token' not in one('SELECT settings FROM servers')['settings']
    row=server_enable(client,row,monkeypatch)
    result=server_patch(client,row,name='Updated modern panel')
    assert result.status_code==200 and result.json()['profile']=='clients_vless'
    uid=add_customer()
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id) VALUES ('pinned-modern',?,'xui',0,'pending',?)",(uid,row['id']))
    switched=server_patch(client,result.json(),state='disabled',settings=server_body()['settings'])
    assert switched.status_code==409


@pytest.mark.parametrize('change',[{'api':'unknown'},{'token':'short-secret'},{'token':'a'*32+'\n'}, {'username':'no-mixed-auth'}])
def test_clients_invalid_profile_is_rejected_without_echo(registry_client,change):
    settings={'api':'clients','url':'https://panel.example','token':'private-api-token-'+'a'*32,
              'inbound_id':7,'link_template':'vless://{uuid}@vpn.example:443',**change}
    response=server_create(registry_client,settings=settings)
    assert response.status_code==422
    assert 'private-api-token' not in response.text and 'short-secret' not in response.text


@pytest.mark.parametrize('base', ['', '/shop'])
def test_icon_sprite_served_at_configured_prefix(client, monkeypatch, base):
    import re
    import xml.etree.ElementTree as ET
    from app.main import create_app
    monkeypatch.setattr(c, 'BASE_PATH', base)
    with TestClient(create_app(base)) as shop:
        page = shop.get(base + '/')
        assert page.status_code == 200
        assert '__BASE_PATH__' not in page.text
        uses = re.findall(r'<use href="([^"]+)"', page.text)
        assert len(uses) == 6
        sprite = shop.get(base + '/static/pushok-icons.svg')
        assert sprite.status_code == 200
        assert sprite.headers['content-type'].startswith('image/svg+xml')
        ids = {element.get('id') for element in ET.fromstring(sprite.content).iter()}
        for use in uses:
            url, symbol = use.split('#')
            assert url == base + '/static/pushok-icons.svg'
            assert symbol in ids
