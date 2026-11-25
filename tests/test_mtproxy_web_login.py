"""Browser and native grants have separate purposes and consume independent credentials."""
import hmac
from app import config as c
from app.db import one
from test_flow_identity_bridge import _ticket, KEY, ONE
from test_shop import client


def test_mtproxy_web_signin_binds_origin_browser_and_existing_telegram(client, monkeypatch):
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET', KEY)
    monkeypatch.setenv('FLOW_TRIAL_MTPROXY_ONLY', 'true')
    monkeypatch.setattr(c, 'PUBLIC_URL', 'https://testserver/shop')
    monkeypatch.setattr(c, 'SECURE_COOKIE', True)
    client.base_url = 'https://testserver'
    route = '/api/auth/mtproxy'
    headers = {'Origin': 'https://testserver'}
    assert client.get('/api/catalog').json()['mtproxy_login']
    assert client.post(route+'/start', headers={'Origin':'https://other.example'}).status_code == 403
    assert client.post(route+'/start', headers=headers).status_code == 200
    challenge = client.get(route+'/challenge').json()['challenge']
    browser = hmac.digest(bytes.fromhex(KEY), b'pushok-shop-web-login-v1\0' + challenge.encode(), 'sha256')
    web_ticket = _ticket(sub=ONE, tg=12345, secret=browser.hex())
    assert client.post('/api/flow/bridge/exchange', json={'ticket':web_ticket}).status_code == 401
    assert client.post(route+'/finish', json={'ticket':_ticket()}, headers=headers).status_code == 401
    assert client.post(route+'/start', headers=headers).status_code == 200
    assert client.post(route+'/finish', json={'ticket':web_ticket}, headers=headers).status_code == 401
    challenge = client.get(route+'/challenge').json()['challenge']
    browser = hmac.digest(bytes.fromhex(KEY), b'pushok-shop-web-login-v1\0' + challenge.encode(), 'sha256')
    web_ticket = _ticket(sub=ONE, tg=12345, secret=browser.hex())
    assert client.post(route+'/finish', json={'ticket':web_ticket}, headers=headers).status_code == 200
    assert client.get('/api/me').json()['telegram_id'] == 12345
    assert client.get(route+'/challenge').status_code == 409
    assert client.post(route+'/finish', json={'ticket':web_ticket}, headers=headers).status_code == 409
    assert one('SELECT count(*) AS n FROM users')['n'] == 1
    assert one('SELECT count(*) AS n FROM orders')['n'] == 0
