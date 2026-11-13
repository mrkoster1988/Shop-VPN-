import asyncio
import json
import uuid
import httpx
import pytest
from app import update_install, updates
from test_shop import client, admin_headers


def configure(monkeypatch, response):
    monkeypatch.setenv('PUSHOK_HOST_UPDATER_PORT','9079');monkeypatch.setenv('PUSHOK_HOST_UPDATER_TOKEN','a'*64)
    original=httpx.AsyncClient;calls=[]
    def handler(request):calls.append(request);return response
    def factory(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return original(**kwargs,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(update_install.httpx,'AsyncClient',factory)
    return calls


def test_disabled_never_calls_network(client,monkeypatch):
    monkeypatch.delenv('PUSHOK_HOST_UPDATER_TOKEN',raising=False)
    r=client.get('/api/admin/updates/operation',headers=admin_headers())
    assert r.json()=={'enabled':False,'reachable':False,'job':None}


def test_loopback_only_and_public_fields_allowlisted(client,monkeypatch):
    calls=configure(monkeypatch,httpx.Response(200,json={'enabled':True,'reachable':True,'secret':'bad','job':{'id':'test','state':'ready','token':'bad','source':'/private'}}))
    r=client.get('/api/admin/updates/operation',headers=admin_headers())
    assert r.json()['job']=={'id':'test','state':'ready'} and 'secret' not in r.text
    assert str(calls[0].url)=='http://127.0.0.1:9079/v1/status'


@pytest.mark.parametrize('status',[302,403,500])
def test_service_failure_is_sanitized(client,monkeypatch,status):
    configure(monkeypatch,httpx.Response(status,text='private-token',headers={'location':'http://evil'}))
    r=client.get('/api/admin/updates/operation',headers=admin_headers())
    assert r.json()['reachable'] is False and 'private-token' not in r.text


def test_plan_requires_fresh_selected_release(client,monkeypatch):
    calls=configure(monkeypatch,httpx.Response(202,json={'enabled':True,'reachable':True,'job':None}))
    monkeypatch.setattr(updates,'status',lambda:{'can_open_guide':False})
    data={'request_id':str(uuid.uuid4()),'tag':'v0.056'}
    assert client.post('/api/admin/updates/plan',headers=admin_headers(),json=data).status_code==409
    assert not calls
    monkeypatch.setattr(updates,'status',lambda:{'can_open_guide':True,'release':{'tag':'v0.056'}})
    assert client.post('/api/admin/updates/plan',headers=admin_headers(),json=data).status_code==200
    assert json.loads(calls[0].content)==data


def test_nonowner_cannot_call_host_service(client,monkeypatch):
    calls=configure(monkeypatch,httpx.Response(200,json={'enabled':True,'reachable':True,'job':None}))
    assert client.get('/api/admin/updates/operation').status_code==401
    from app.db import execute
    from app.security import digest
    import time
    for role in ('panel_operator','finance','support','mtproxy_manager'):
        execute('INSERT INTO staff_users(email,password,role,enabled,created,updated) VALUES (?,?,?,?,?,?)',(role+'@example.com','unused',role,1,0,0))
        from app.db import one
        uid=one('SELECT id FROM staff_users WHERE email=?',(role+'@example.com',))['id']
        execute('INSERT INTO staff_sessions(token_hash,staff_id,created,expires) VALUES (?,?,?,?)',(digest(role),uid,0,int(time.time())+600))
        client.cookies.set('staff_session',role)
        assert client.get('/api/admin/updates/operation').status_code==403
        assert client.post('/api/admin/updates/apply',json={'request_id':str(uuid.uuid4()),'plan_id':str(uuid.uuid4()),'tag':'v0.056'}).status_code==403
    assert not calls
