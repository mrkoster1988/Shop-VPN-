"""Shop trust boundary: signed ID approval + live browser + explicit consent."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs,urlsplit

import pytest

from app import config as c, service, pushok_identity as identity
from app.db import execute, one, init_db
from app.security import digest
from test_shop import client, register, admin_headers

BASE='/api/auth/pushok-id'
ADMIN='/api/admin/pushok-id'
SUBJECT='57c00362-45d4-4aae-8b3d-c2bc23172b71'
OTHER='01f6d68f-b242-4161-9c92-e21764a989b4'
KEY='12'*32
AUD='pushok-shop:fixture-shop-instance'


def configure(client,monkeypatch):
    monkeypatch.setattr(c,'SECURE_COOKIE',True)
    monkeypatch.setattr(c,'PUBLIC_URL','https://testserver')
    client.base_url='https://testserver'
    monkeypatch.setenv('SHOP_PUSHOK_ID_SECRET',KEY)
    monkeypatch.setenv('SHOP_PUSHOK_ID_AUDIENCE',AUD)
    monkeypatch.setenv('SHOP_PUSHOK_ID_PORTAL','https://auth.example')
    return client


def policy(client,**changes):
    old=client.get(ADMIN,headers=admin_headers()).json()
    data={k:old[k] for k in ('enabled','allow_new_accounts','version')}|changes
    result=client.put(ADMIN,headers=admin_headers(),json=data)
    assert result.status_code==200,result.text
    return result.json()


@pytest.fixture
def ready(client,monkeypatch):
    configure(client,monkeypatch)
    policy(client,enabled=True)
    return client


def start(client,mode='signin'):
    response=client.post(BASE+'/start',json={'mode':mode})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['state']=='pending'
    url=urlsplit(result['approval_url'])
    query=parse_qs(url.query)
    assert url.scheme=='https' and url.hostname=='auth.example' and query['shop_audience']==[AUD]
    assert 'session=' not in result['approval_url']
    return query['shop_request'][0]


def proof(request,subject=SUBJECT,tg=777,**changes):
    now=int(time.time())
    claims={'v':1,'iss':'pushok-id','aud':AUD,'purpose':'shop_identity','request':request,
            'sub':subject,'tg':tg,'iat':now,'exp':now+60}|changes
    raw=json.dumps(claims,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
    signature=hmac.new(bytes.fromhex(KEY),identity._DOMAIN+raw,hashlib.sha256).hexdigest()
    return 'pid1.'+base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')+'.'+signature


def approve(client,request,subject=SUBJECT,tg=777):
    return client.post(BASE+'/approve',json={'proof':proof(request,subject,tg)})


def finish(client,**changes):
    status=client.get(BASE+'/status')
    assert status.status_code==200,status.text
    data=status.json()
    assert data['state']=='approved',data
    body={'confirmation':data['confirmation'],'subject':data['subject'],'confirm':True}|changes
    return client.post(BASE+'/finish',json=body)


def local_tg(client,tg=777):
    user=service.telegram_account(tg)
    client.get(service.ticket(user['id']))
    return user


def test_disabled_by_default_and_requires_separate_https_configuration(client,monkeypatch):
    monkeypatch.delenv('SHOP_PUSHOK_ID_SECRET',raising=False)
    assert client.get('/api/catalog').json()['pushok_id']['enabled'] is False
    assert client.post(BASE+'/start',json={'mode':'signin'}).status_code==503
    assert client.get(ADMIN).status_code==401
    assert client.put(ADMIN,headers=admin_headers(),json={'enabled':True,'allow_new_accounts':False,'version':0}).status_code==409
    configure(client,monkeypatch)
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',KEY)
    assert client.put(ADMIN,headers=admin_headers(),json={'enabled':True,'allow_new_accounts':False,'version':0}).status_code==409
    monkeypatch.delenv('FLOW_SHOP_BRIDGE_SECRET',raising=False)
    result=policy(client,enabled=True)
    assert result['configured'] is True and result['central_issuer_verified'] is False
    assert KEY not in json.dumps(result)
    assert client.get('/api/catalog').json()['pushok_id']=={'enabled':True,'wallet_status':'not_connected'}


def test_signed_approval_does_not_log_in_without_browser_cookie_and_consent(ready):
    client=ready
    user=service.telegram_account(777)
    uid=user['id'];request=start(client)
    raw_cookie=client.cookies.get(identity.COOKIE)
    row=one('SELECT * FROM shop_id_requests')
    assert row['request_hash']==digest(request) and row['browser_hash']==digest(raw_cookie)
    assert raw_cookie not in str(row) and request not in str(row)
    assert client.get(BASE+'/status').json()['state']=='pending'
    signed=proof(request)
    changed=signed[:-1]+('1' if signed[-1]=='0' else '0')
    for invalid in (changed,proof(request,aud='pushok-shop:other-installation'),
                    proof(request,iss='mtproxy'),proof(request,tg=True),proof(request,purpose='wallet'),
                    proof(request,iat=int(time.time())-90,exp=int(time.time())-30)):
        assert client.post(BASE+'/approve',json={'proof':invalid}).status_code==401
    good=client.post(BASE+'/approve',json={'proof':signed})
    assert good.status_code==200 and 'set-cookie' not in good.headers
    assert client.get('/api/me').status_code==401
    status=client.get(BASE+'/status').json()
    assert status['telegram_id']==777 and status['subject']==SUBJECT
    assert client.post(BASE+'/approve',json={'proof':signed}).status_code==409
    body={'confirmation':status['confirmation'],'subject':SUBJECT,'confirm':True}
    client.cookies.clear()
    assert client.post(BASE+'/finish',json=body).status_code==409
    client.cookies.set(identity.COOKIE,raw_cookie,domain='testserver.local',path='/')
    assert client.post(BASE+'/finish',json=body|{'confirm':False}).status_code==422
    assert client.post(BASE+'/finish',json=body|{'confirm':1}).status_code==422
    assert client.post(BASE+'/finish',json=body|{'subject':OTHER}).status_code==409
    result=client.post(BASE+'/finish',json=body)
    assert result.status_code==200,result.text
    assert result.json()=={'ok':True,'linked':True,'signed_in':True,'wallet_status':'not_connected'}
    assert 'Max-Age=3600' in result.headers['set-cookie']
    assert 'HttpOnly' in result.headers['set-cookie'] and 'Secure' in result.headers['set-cookie']
    assert client.get('/api/me').json()['id']==uid
    assert client.get('/api/me').json()['pushok_id']['subject']==SUBJECT
    assert client.get('/api/admin/staff').status_code==401
    assert one('SELECT count(*) AS n FROM users')['n']==1
    assert one('SELECT count(*) AS n FROM orders')['n']==0
    assert one('SELECT count(*) AS n FROM shop_id_requests')['n']==0
    assert one('SELECT count(*) AS n FROM shop_id_sessions')['n']==1
    assert client.post(BASE+'/finish',json=body).status_code==409


def test_explicit_link_requires_same_verified_telegram_and_unchanged_local_session(ready):
    client=ready
    user=local_tg(client,777)
    original=client.cookies.get('session')
    request=start(client,'link')
    assert approve(client,request,tg=888).status_code==200
    assert finish(client).status_code==409
    assert one('SELECT count(*) AS n FROM shop_id_links')['n']==0
    request=start(client,'link')
    assert approve(client,request).status_code==200
    # Same user logging in again is a different Shop session: reject old approval.
    client.get(service.ticket(user['id']))
    assert client.cookies.get('session')!=original
    assert client.get(BASE+'/status').status_code==409
    request=start(client,'link')
    token=client.cookies.get('session')
    assert approve(client,request).status_code==200
    assert finish(client).json()['signed_in'] is False
    assert client.cookies.get('session')==token
    assert one('SELECT user_id FROM shop_id_links')['user_id']==user['id']
    assert one('SELECT count(*) AS n FROM shop_id_sessions')['n']==0


def test_email_accounts_are_never_auto_merged(ready):
    client=ready
    register(client)
    uid=client.get('/api/me').json()['id']
    assert client.post(BASE+'/start',json={'mode':'link'}).status_code==409
    assert client.post(BASE+'/start',json={'mode':'signin'}).status_code==409
    assert one('SELECT telegram_id FROM users WHERE id=?',(uid,))['telegram_id'] is None
    assert one('SELECT count(*) AS n FROM shop_id_links')['n']==0


def test_new_accounts_require_owner_opt_in_and_unique_subject_binding(ready):
    client=ready
    request=start(client)
    assert approve(client,request).status_code==200
    assert finish(client).status_code==409
    assert one('SELECT count(*) AS n FROM users')['n']==0
    policy(client,allow_new_accounts=True)
    assert client.get(BASE+'/status').json()['state']=='missing'
    request=start(client)
    assert approve(client,request).status_code==200
    assert finish(client).status_code==200
    uid=client.get('/api/me').json()['id']
    assert one('SELECT telegram_id FROM users WHERE id=?',(uid,))['telegram_id']==777
    client.post('/api/auth/logout')
    request=start(client)
    assert approve(client,request,subject=OTHER).status_code==200
    assert finish(client).status_code==409
    request=start(client)
    assert approve(client,request,tg=888).status_code==200
    assert finish(client).status_code==409
    assert one('SELECT count(*) AS n FROM users')['n']==1
    assert one('SELECT count(*) AS n FROM shop_id_links')['n']==1
    init_db()
    assert one('SELECT subject FROM shop_id_links')['subject']==SUBJECT


def test_disable_and_key_rotation_revoke_id_sessions_not_local_sessions(ready,monkeypatch):
    client=ready
    user=local_tg(client,777);normal=client.cookies.get('session')
    client.cookies.clear()
    request=start(client);assert approve(client,request).status_code==200
    assert finish(client).status_code==200
    central=client.cookies.get('session')
    assert central!=normal
    assert one('SELECT expires FROM sessions WHERE token=?',(digest(central),))['expires']<=int(time.time())+3600
    monkeypatch.setenv('SHOP_PUSHOK_ID_SECRET','34'*32)
    assert client.get('/api/me').status_code==401
    monkeypatch.setenv('SHOP_PUSHOK_ID_SECRET',KEY)
    assert client.get('/api/me').status_code==200
    policy(client,enabled=False)
    assert client.get('/api/me').status_code==401
    assert one('SELECT count(*) AS n FROM shop_id_sessions')['n']==0
    client.cookies.clear();client.cookies.set('session',normal,domain='testserver.local',path='/')
    assert client.get('/api/me').json()['id']==user['id']
    assert one('SELECT count(*) AS n FROM shop_id_links')['n']==1


def test_user_can_revoke_id_sessions_and_pending_requests(ready):
    client=ready
    service.telegram_account(777)
    request=start(client);assert approve(client,request).status_code==200
    assert finish(client).status_code==200
    result=client.post(BASE+'/revoke',json={})
    assert result.status_code==200 and result.json()=={'ok':True,'revoked':1,'signed_out':True}
    assert client.get('/api/me').status_code==401
    assert one('SELECT count(*) AS n FROM shop_id_links')['n']==1
    request=start(client)
    assert client.post(BASE+'/cancel',json={}).status_code==200
    assert approve(client,request).status_code==409


def test_expiry_and_policy_version_reject_stale_approvals(ready):
    client=ready
    request=start(client)
    execute('UPDATE shop_id_requests SET expires=1')
    assert approve(client,request).status_code==409
    assert client.get(BASE+'/status').json()['state']=='missing'
    request=start(client)
    policy(client,allow_new_accounts=True)
    assert approve(client,request).status_code==409
    stale=client.put(ADMIN,headers=admin_headers(),json={'version':1,'enabled':True,'allow_new_accounts':False})
    assert stale.status_code==409
    assert one('SELECT count(*) AS n FROM users')['n']==0


def test_invalid_proof_validation_does_not_echo_and_admin_cannot_change_trust(client):
    from test_staff_roles import bootstrap,add_role,login
    assert client.post(BASE+'/approve',json={'proof':'private-invalid-proof'}).status_code==422
    response=client.post(BASE+'/approve',json={'proof':'private-invalid-proof'})
    assert 'private-invalid-proof' not in response.text
    bootstrap(client);add_role(client,'administrator');login(client,'administrator')
    assert client.get(ADMIN).status_code==403
    assert client.put(ADMIN,json={'version':0,'enabled':False,'allow_new_accounts':False}).status_code==403
