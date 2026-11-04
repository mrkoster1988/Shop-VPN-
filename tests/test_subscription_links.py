import asyncio,base64,time,uuid
from urllib.parse import urlsplit
import pytest
from test_devices import issued,add
from test_shop import client,register
from app import providers,service,config as c
from app.db import one,execute,init_db
from app.security import digest

@pytest.fixture
def linked(issued,monkeypatch):
    client,issue,calls=issued
    async def valid(sub):calls.append(dict(sub));return 'vless://'+sub['id']+'@vpn.example:443?security=tls'
    monkeypatch.setattr(providers,'provision',valid)
    return client,issue(),calls


def settings(client,sid):return client.get('/api/subscriptions/'+sid+'/link').json()
def change(client,sid,action,version=0,follow_rotation=False):
    return client.put('/api/subscriptions/'+sid+'/link',json={'action':action,'version':version,'follow_rotation':follow_rotation})
def exported(client,link):return client.get(urlsplit(link['url']).path)

def test_link_exports_only_primary_without_session_and_stores_no_plain_token(linked):
    client,sub,_=linked;extra=add(client,sub['id']).json();asyncio.run(service.process_devices())
    initial=settings(client,sub['id']);assert initial=={'enabled':False,'follow_rotation':False,'version':0,'url':None}
    result=change(client,sub['id'],'enable').json();token=result['url'].rsplit('/',1)[1]
    row=one('SELECT * FROM subscription_links');assert token not in str(row) and row['token_hash']==digest(token)
    assert c.decrypt(row['token_secret'])==token and len(token)==43
    init_db();assert settings(client,sub['id'])==result
    client.post('/api/auth/logout');response=exported(client,result)
    decoded=base64.b64decode(response.text).decode()
    assert response.status_code==200 and decoded=='vless://'+sub['id']+'@vpn.example:443?security=tls\n'
    assert extra['id'] not in decoded and 'email' not in decoded
    assert response.headers['cache-control']=='no-store' and response.headers['referrer-policy']=='no-referrer'
    assert response.headers['subscription-userinfo']=='expire='+str(sub['expires'])
    assert response.headers['profile-update-interval']=='12'


def test_rotate_disable_and_reenable_revoke_old_urls_without_revoking_vpn_key(linked):
    client,sub,_=linked;a=change(client,sub['id'],'enable').json();original=client.get('/api/subscriptions/'+sub['id']+'/config').json()
    b=change(client,sub['id'],'rotate',a['version']).json();assert b['url']!=a['url'] and exported(client,a).status_code==404
    assert exported(client,b).status_code==200
    disabled=change(client,sub['id'],'disable',b['version']).json();assert disabled['url'] is None and not disabled['enabled']
    assert exported(client,b).status_code==404 and one('SELECT token_hash,token_secret FROM subscription_links')=={'token_hash':None,'token_secret':None}
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()==original
    fresh=change(client,sub['id'],'enable',disabled['version']).json();assert fresh['url'] not in (a['url'],b['url'])


def test_optimistic_version_prevents_duplicate_url_changes(linked):
    client,sub,_=linked;a=change(client,sub['id'],'enable').json()
    assert change(client,sub['id'],'enable').status_code==409
    same=change(client,sub['id'],'enable',a['version'],True).json();assert same['url']==a['url'] and same['follow_rotation']
    b=change(client,sub['id'],'rotate',same['version'],True).json()
    assert change(client,sub['id'],'rotate',same['version'],True).status_code==409
    assert settings(client,sub['id'])==b

@pytest.mark.parametrize('follow',[False,True])
def test_primary_key_rotation_respects_explicit_link_policy(linked,follow):
    client,sub,_=linked;link=change(client,sub['id'],'enable',follow_rotation=follow).json();old=base64.b64decode(exported(client,link).text)
    rid=str(uuid.uuid4());assert client.post('/api/subscriptions/'+sub['id']+'/rotate',json={'request_id':rid}).status_code==201
    assert exported(client,link).status_code==404
    asyncio.run(service.process_devices());result=exported(client,link)
    assert result.status_code==(200 if follow else 404)
    if follow:assert base64.b64decode(result.text)!=old
    else:
        disabled=settings(client,sub['id']);assert disabled['version']==link['version']+1 and disabled['url'] is None
        new=change(client,sub['id'],'enable',disabled['version']).json()
        client.post('/api/subscriptions/'+sub['id']+'/rotate',json={'request_id':rid})
        assert exported(client,new).status_code==200  # An old operation replay cannot disable the new link.


def test_expired_link_resumes_after_renewal_and_is_unavailable_during_rotation(linked):
    client,sub,_=linked;link=change(client,sub['id'],'enable',follow_rotation=True).json()
    execute('UPDATE subscriptions SET expires=1');assert exported(client,link).status_code==404
    execute('UPDATE subscriptions SET expires=?',(int(time.time())+3600,));assert exported(client,link).status_code==200
    execute("UPDATE subscriptions SET primary_key_state='rotating'");assert exported(client,link).status_code==404
    execute("UPDATE subscriptions SET primary_key_state='active',state='expired'");assert exported(client,link).status_code==404


def test_link_settings_require_owner_and_origin(linked):
    client,sub,_=linked;link=change(client,sub['id'],'enable').json()
    response=client.put('/api/subscriptions/'+sub['id']+'/link',json={'action':'disable','version':1},headers={'Origin':'https://evil.example'})
    assert response.status_code==403
    me=client.get('/api/me');assert link['url'] not in me.text and one('SELECT token_secret FROM subscription_links')['token_secret'] not in me.text
    client.post('/api/auth/logout');assert client.get('/api/subscriptions/'+sub['id']+'/link').status_code==401
    register(client,'other@example.com');assert client.get('/api/subscriptions/'+sub['id']+'/link').status_code==404
    assert change(client,sub['id'],'disable',1).status_code==404

@pytest.mark.parametrize('body',[{'action':'other','version':0},{'action':'enable','version':True},{'action':'enable','version':0,'follow_rotation':'yes'},{'action':'enable','version':-1}])
def test_invalid_link_settings_are_rejected(linked,body):
    client,sub,_=linked
    assert client.put('/api/subscriptions/'+sub['id']+'/link',json=body).status_code==422
    assert one('SELECT count(*) AS n FROM subscription_links')['n']==0


def test_wg_and_inactive_profiles_do_not_create_links(issued):
    client,issue,_=issued;sub=issue('wg')
    assert client.get('/api/subscriptions/'+sub['id']+'/link').status_code==409
    assert change(client,sub['id'],'enable').status_code==409
    sub=issue('xui');execute('UPDATE subscriptions SET expires=1')
    assert change(client,sub['id'],'enable').status_code==409


def test_bad_tokens_and_malformed_config_never_echo_secrets(linked):
    client,sub,_=linked;link=change(client,sub['id'],'enable').json()
    for token in ('bad-token','x'*43):
        response=client.get('/sub/'+token);assert response.status_code==404 and token not in response.text
        assert response.headers['cache-control']=='no-store'
    execute('UPDATE subscriptions SET secret=?',(c.encrypt('private-invalid-config'),))
    response=exported(client,link);assert response.status_code==503 and 'private-invalid-config' not in response.text


def test_public_export_rate_limit_is_separate_and_has_private_headers(linked):
    from app.main import hits
    client,_,_=linked;hits.clear()
    for _ in range(60):assert client.get('/sub/'+'x'*43).status_code==404
    response=client.get('/sub/'+'x'*43);assert response.status_code==429 and response.headers['cache-control']=='no-store'
    assert client.get('/api/me').status_code==200


def test_link_under_shop_prefix(linked,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    client,sub,_=linked;monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'COOKIE_PATH','/shop/');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        shop.cookies.update(client.cookies)
        result=shop.put('/shop/api/subscriptions/'+sub['id']+'/link',json={'action':'enable','version':0},headers={'Origin':'http://testserver'})
        assert result.status_code==200 and '/shop/sub/' in result.json()['url']
        response=shop.get(urlsplit(result.json()['url']).path);assert response.status_code==200 and response.headers['cache-control']=='no-store'


def test_corrupt_encrypted_profile_returns_private_error(linked):
    client,sub,_=linked;link=change(client,sub['id'],'enable').json();execute("UPDATE subscriptions SET secret='broken-ciphertext'")
    response=exported(client,link)
    assert response.status_code==503 and response.headers['cache-control']=='no-store'
    assert 'broken-ciphertext' not in response.text and link['url'] not in response.text
