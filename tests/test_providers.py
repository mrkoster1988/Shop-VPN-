import asyncio, json
import httpx
import pytest
from cryptography.fernet import Fernet
from app import config as c, servers
from app.db import init_db
from app import providers


@pytest.fixture(autouse=True)
def registry(tmp_path,monkeypatch):
    monkeypatch.setattr(c,'DB',tmp_path/'providers.db')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    for key in ('XUI_URL','WG_AGENT_URL'):
        monkeypatch.delenv(key,raising=False)
    init_db()


def test_xui_add_retry_updates_same_uuid(monkeypatch):
    for k,v in {'XUI_URL':'https://panel.example/secret','XUI_USERNAME':'user','XUI_PASSWORD':'pass',
                'XUI_INBOUND_ID':'7','XUI_LINK_TEMPLATE':'vless://{uuid}@vpn.example:443?security=tls'}.items():
        monkeypatch.setenv(k,v)
    saved=[]; actions=[]
    def handler(request):
        from urllib.parse import parse_qs
        if request.url.path.endswith('/login'): return httpx.Response(200,json={'success':True})
        if request.url.path.endswith('/get/7'):
            return httpx.Response(200,json={'success':True,'obj':{'protocol':'vless','settings':json.dumps({'clients':saved})}})
        body=parse_qs(request.content.decode())
        client=json.loads(body['settings'][0])['clients'][0]
        actions.append(request.url.path)
        saved[:]=[client]
        return httpx.Response(200,json={'success':True})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    servers.import_environment()
    sub={'id':'stable-id','expires':2000000000,'protocol':'xui','server_id':'legacy-xui'}
    a=asyncio.run(providers.provision(sub))
    b=asyncio.run(providers.provision(sub))
    assert a==b=='vless://stable-id@vpn.example:443?security=tls'
    assert actions[0].endswith('/addClient') and actions[1].endswith('/updateClient/stable-id')
    assert saved[0]['expiryTime']==2000000000000
    asyncio.run(providers.disable(sub))
    assert saved[0]['enable'] is False


def test_wireguard_provider_passes_stable_id_and_expiry(monkeypatch):
    monkeypatch.setenv('WG_AGENT_URL','https://wg.example')
    monkeypatch.setenv('WG_AGENT_TOKEN','a'*32)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'config':'[Interface]\nPrivateKey = test'})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    servers.import_environment()
    sub={'id':'stable-id','expires':2000000000,'protocol':'wg','server_id':'legacy-wg'}
    assert asyncio.run(providers.provision(sub)).startswith('[Interface]')
    assert calls[0].url.path=='/peers/stable-id'
    assert json.loads(calls[0].content)['expires']==2000000000
    assert calls[0].headers['authorization']=='Bearer '+'a'*32


def test_yookassa_creation_uses_order_idempotency(monkeypatch):
    from app import config as c
    monkeypatch.setattr(c, 'PUBLIC_URL', 'https://pushok.shop/shop')
    monkeypatch.setenv('YOOKASSA_SHOP_ID','123')
    monkeypatch.setenv('YOOKASSA_SECRET','secret')
    monkeypatch.setenv('YOOKASSA_RECEIPTS','true')
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'id':'payment-id','confirmation':{'confirmation_url':'https://yookassa.example/pay'}})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    order={'id':'order-id','amount':199}
    asyncio.run(providers.payment(order,'receipt@example.com'))
    assert calls[0].headers['idempotence-key']=='order-id'
    data=json.loads(calls[0].content)
    assert data['amount']=={'value':'199.00','currency':'RUB'}
    assert data['capture'] is True and data['receipt']['customer']['email']=='receipt@example.com'
    assert data['confirmation']['return_url'] == 'https://pushok.shop/shop/?payment=return'


def test_registry_profiles_route_to_pinned_agent_without_environment_fallback(monkeypatch):
    from app.db import execute
    calls=[]
    for sid, host, token in [('a','https://agent-a.example','a'*32),('b','https://agent-b.example','b'*32)]:
        execute("INSERT INTO servers(id,name,protocol,state,settings,created) VALUES (?,?,?,'active',?,0)",
            (sid,sid,'wg',c.encrypt(json.dumps({'url':host,'token':token}))))
    monkeypatch.setenv('WG_AGENT_URL','https://wrong.example')
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'config':'[Interface]\nPrivateKey = test'})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    for sid in ('a','b'):
        asyncio.run(providers.provision({'id':'stable-'+sid,'protocol':'wg','server_id':sid,'expires':2000000000}))
    assert [request.url.host for request in calls]==['agent-a.example','agent-b.example']
    assert calls[0].headers['authorization']=='Bearer '+'a'*32
    execute("UPDATE servers SET state='disabled' WHERE id='a'")
    asyncio.run(providers.disable({'id':'stable-a','protocol':'wg','server_id':'a'}))
    assert calls[-1].method=='DELETE' and calls[-1].url.host=='agent-a.example'
    with pytest.raises(RuntimeError,match='ServerBindingMissing'):
        asyncio.run(providers.provision({'id':'unbound','protocol':'wg','expires':2000000000}))
    assert len(calls)==3


@pytest.fixture
def clients_panel(monkeypatch):
    from app.db import execute
    from app import panel
    profile = servers.validated('xui',{'api':'clients','url':'https://panel.example/secret','token':'t'*48,
        'inbound_id':7,'link_template':'vless://{uuid}@vpn.example:443','flow':'xtls-rprx-vision'})
    execute("INSERT INTO servers(id,name,protocol,state,settings,created) VALUES ('modern','modern','xui','active',?,0)",
            (c.encrypt(json.dumps(profile)),))
    state={'record':None,'calls':[], 'writes':[], 'drop_response':False, 'override':None}
    sub={'id':'8ebcd9cb-49bd-4e17-ac7c-5863d6523638','protocol':'xui','server_id':'modern','expires':2000000000}
    def handler(request):
        assert request.headers['authorization']=='Bearer '+'t'*48
        state['calls'].append(request)
        if state['override']:
            answer=state['override'](request)
            if answer is not None: return answer
        path=request.url.path
        if path.endswith('/inbounds/get/7'):
            return httpx.Response(200,json={'success':True,'obj':{'protocol':'vless'}})
        if path.endswith('/clients/list/paged'):
            items=[] if state['record'] is None else [{'email':state['record']['email']}]
            return httpx.Response(200,json={'success':True,'obj':{'items':items,'filtered':len(items)}})
        if '/clients/get/' in path:
            return httpx.Response(200,json={'success':True,'obj':{'client':state['record'],'inboundIds':[7]}})
        if request.method=='POST':
            data=json.loads(request.content)
            state['writes'].append(request)
            data=data['client'] if path.endswith('/add') else data
            record={k:v for k,v in data.items() if k!='id'}
            record.update(id=42,uuid=data['id'])
            state['record']=record
            if state['drop_response']:
                state['drop_response']=False
                raise httpx.ReadTimeout('sensitive upstream response',request=request)
            return httpx.Response(200,json={'success':True})
        raise AssertionError(path)
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    return state,sub,panel


def test_clients_adapter_create_renew_disable_and_preserve_settings(clients_panel):
    state,sub,_=clients_panel
    first=asyncio.run(providers.provision(sub))
    assert first=='vless://'+sub['id']+'@vpn.example:443'
    data=json.loads(state['writes'][0].content)
    assert data['inboundIds']==[7] and data['client']['id']==sub['id']
    state['record'].update(limitIp=3,totalGB=500,comment='keep me',group='existing',limitHwid=2,
        reverse=json.dumps({'tag':'reverse-tag'}),clientHostRuleIds=[4])
    sub['expires']+=86400
    assert asyncio.run(providers.provision(sub))==first
    updated=json.loads(state['writes'][1].content)
    assert updated['id']==sub['id'] and updated['expiryTime']==sub['expires']*1000
    assert updated['comment']=='keep me' and updated['totalGB']==500 and updated['clientHostRuleIds']==[4]
    assert updated['reverse']=={'tag':'reverse-tag'} and 'uuid' not in updated
    asyncio.run(providers.disable(sub))
    assert state['record']['enable'] is False
    assert not any('/login' in str(r.url) or 'addClient' in str(r.url) for r in state['calls'])


def test_clients_adapter_timeout_retries_existing_identity(clients_panel):
    state,sub,_=clients_panel
    state['drop_response']=True
    with pytest.raises(httpx.ReadTimeout): asyncio.run(providers.provision(sub))
    assert state['record']['uuid']==sub['id']
    asyncio.run(providers.provision(sub))
    assert len(state['writes'])==2
    assert state['writes'][0].url.path.endswith('/add')
    assert '/update/pushok_' in state['writes'][1].url.path


@pytest.mark.parametrize('fault',['auth','redirect','unknown_schema','collision','wrong_uuid','other_inbound','write_false','not_applied','lookup_false'])
def test_clients_adapter_fails_closed_without_fallback(clients_panel,fault):
    state,sub,panel=clients_panel
    if fault in ('collision','wrong_uuid','other_inbound'):
        asyncio.run(providers.provision(sub))
        state['writes'].clear()
    def override(request):
        path=request.url.path
        if fault=='auth': return httpx.Response(401,json={'secret':'upstream-secret'})
        if fault=='redirect': return httpx.Response(302,headers={'location':'https://wrong.example'})
        if fault=='unknown_schema' and '/list/paged' in path: return httpx.Response(200,json={'success':True,'obj':[]})
        if fault=='lookup_false' and '/list/paged' in path: return httpx.Response(200,json={'success':False,'msg':'upstream-secret'})
        if fault=='collision' and '/list/paged' in path: return httpx.Response(200,json={'success':True,'obj':{'items':[],'filtered':201}})
        if fault in ('wrong_uuid','other_inbound') and '/clients/get/' in path:
            record={**state['record'],'uuid':'different'} if fault=='wrong_uuid' else state['record']
            return httpx.Response(200,json={'success':True,'obj':{'client':record,'inboundIds':[7,8] if fault=='other_inbound' else [7]}})
        if fault in ('write_false','not_applied') and request.method=='POST':
            return httpx.Response(200,json={'success':fault=='not_applied','msg':'upstream-secret'})
    state['override']=override
    with pytest.raises((panel.PanelError,httpx.HTTPStatusError)) as error:
        asyncio.run(providers.provision(sub))
    assert 'upstream-secret' not in str(error.value)
    assert state['writes']==[]
    assert not any('/login' in str(r.url) or '/inbounds/addClient' in str(r.url) for r in state['calls'])


def test_clients_probe_only_reads_and_absent_revocation_does_not_create(clients_panel):
    from app.db import one
    state,sub,_=clients_panel
    asyncio.run(providers.probe_server(one("SELECT * FROM servers WHERE id='modern'")))
    assert all(r.method=='GET' for r in state['calls'])
    asyncio.run(providers.disable(sub))
    assert state['writes']==[]
