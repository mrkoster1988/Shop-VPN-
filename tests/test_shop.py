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
