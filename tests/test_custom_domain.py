import time
import uuid
import pytest
from fastapi.testclient import TestClient
from app import config as c,service
from app.db import execute
from app.main import create_app
from test_shop import client

@pytest.mark.parametrize('origin,prefix',[
    ('https://vpn.alpha.example','/shop'),('https://store.beta.example','/store/vpn'),
])
def test_independent_owner_domain_controls_links_cookies_and_origin(client,monkeypatch,origin,prefix):
    monkeypatch.setattr(c,'PUBLIC_URL',origin+prefix)
    monkeypatch.setattr(c,'BASE_PATH',prefix);monkeypatch.setattr(c,'COOKIE_PATH',prefix+'/')
    monkeypatch.setattr(c,'SECURE_COOKIE',True)
    with TestClient(create_app(prefix),base_url=origin) as shop:
        page=shop.get(prefix+'/')
        assert page.status_code==200 and 'content="'+prefix+'"' in page.text
        assert 'pushok.shop' not in page.text and 'href="'+prefix+'/static/style.css"' in page.text
        response=shop.post(prefix+'/api/auth/register',json={'email':'owner@example.invalid','password':'long-owner-password'},headers={'Origin':origin})
        assert response.status_code==200
        assert 'Secure' in response.headers['set-cookie'] and 'Path='+prefix+'/' in response.headers['set-cookie']
        customer=shop.get(prefix+'/api/me').json();uid=customer['id']
        execute('INSERT INTO referral_codes(user_id,code,created) VALUES (?,?,?)',(uid,'a'*24,int(time.time())))
        assert shop.get(prefix+'/api/me').json()['referrals']['url']==origin+prefix+'/?ref='+'a'*24
        sid=str(uuid.uuid4())
        execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret,server_id) VALUES (?,?,'xui',?,'active',?,'test-xui')",(sid,uid,int(time.time())+3600,c.encrypt('vless://fixture@node.example:443')))
        link=shop.put(prefix+'/api/subscriptions/'+sid+'/link',headers={'Origin':origin},json={'action':'enable','version':0}).json()
        assert link['url'].startswith(origin+prefix+'/sub/')
        assert shop.get(link['url']).status_code==200
        assert shop.post(prefix+'/api/auth/logout',json={},headers={'Origin':'https://pushok.shop'}).status_code==403
        assert shop.get(prefix+'/api/me').status_code==200
        ticket=service.ticket(uid);assert ticket.startswith(origin+prefix+'/login?ticket=')
        shop.post(prefix+'/api/auth/logout',json={},headers={'Origin':origin})
        response=shop.get(ticket,follow_redirects=False)
        assert response.status_code==303 and response.headers['location']==prefix+'/'
        assert shop.get(prefix+'/api/me').status_code==200
        assert shop.get('/api/me').status_code==404
