"""Legacy write acknowledgement must be followed by independently confirmed state."""
import asyncio
import copy
import json
from urllib.parse import parse_qs
import httpx
import pytest
from app.panel import Legacy, PanelError

@pytest.fixture
def fixture():
    profile={'url':'https://panel.example/private','inbound_id':7,'username':'fixture','password':'secret',
             'flow':'','link_template':'vless://{uuid}@vpn.example:443'}
    sub={'id':'8ebcd9cb-49bd-4e17-ac7c-5863d6523638','expires':2000000000}
    state={'records':[],'writes':[],'after':None,'drop':False,'inbound':None}
    def handler(request):
        if request.url.path.endswith('/login'):return httpx.Response(200,json={'success':True})
        if request.method=='GET':
            inbound=state['inbound'] if state['inbound'] is not None else {'id':7,'protocol':'vless','settings':json.dumps({'clients':state['records']})}
            return httpx.Response(200,json={'success':True,'obj':inbound})
        values=parse_qs(request.content.decode());payload=json.loads(values['settings'][0])['clients'][0]
        state['writes'].append(copy.deepcopy(payload))
        if state['after'] is not None:state['after'](state,payload)
        else:state['records'][:]=[row for row in state['records'] if row['id']!=payload['id']]+[payload]
        if state['drop']:
            state['drop']=False;raise httpx.ReadTimeout('secret upstream body',request=request)
        return httpx.Response(200,json={'success':True})
    async def call(enabled=True):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Legacy(profile).apply(client,sub,enabled)
    async def probe():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Legacy(profile).check(client)
    state['probe']=lambda:asyncio.run(probe())
    state['handler']=handler
    return state,sub,lambda enabled=True:asyncio.run(call(enabled))

def test_create_renew_revoke_verified_and_custom_fields_preserved(fixture):
    state,sub,call=fixture
    first=call();record=state['records'][0]
    record.update(email='old-customer-label',subId='old-subscription-label',flow='custom-flow',limitIp=3,totalGB=500,comment='keep')
    sub['expires']+=86400
    assert call()==first
    renewed=state['records'][0]
    assert renewed['email']=='old-customer-label' and renewed['subId']=='old-subscription-label'
    assert renewed['flow']=='custom-flow' and renewed['totalGB']==500 and renewed['limitIp']==3 and renewed['comment']=='keep'
    call(False)
    assert state['records'][0]['enable'] is False
    assert len(state['writes'])==3

@pytest.mark.parametrize('fault',['ignored','wrong_expiry','bool_expiry','wrong_enable','wrong_email','wrong_subid','duplicate','lost_uuid'])
def test_successful_ack_without_exact_state_is_not_issuance(fixture,fault):
    state,sub,call=fixture
    def after(state,payload):
        value=copy.deepcopy(payload)
        if fault=='ignored':return
        if fault=='wrong_expiry':value['expiryTime']-=1000
        if fault=='bool_expiry':value['expiryTime']=True
        if fault=='wrong_enable':value['enable']=1
        if fault=='wrong_email':value['email']='different'
        if fault=='wrong_subid':value['subId']='different'
        if fault=='lost_uuid':value['id']='different'
        state['records'][:]=[value,value] if fault=='duplicate' else [value]
    state['after']=after
    with pytest.raises(PanelError) as error:call()
    assert 'secret' not in str(error.value)
    assert len(state['writes'])==1

@pytest.mark.parametrize('key',['email','subId'])
def test_identity_collision_refuses_before_write(fixture,key):
    state,sub,call=fixture
    state['records']=[{'id':'unrelated',key:'pushok_'+sub['id'] if key=='email' else sub['id'].replace('-','')}]
    with pytest.raises(PanelError,match='Collision'):call()
    assert state['writes']==[]

@pytest.mark.parametrize('settings',[None,'{bad',{}, {'clients':None},{'clients':[None]},{'clients':[{'id':None}]}])
def test_unknown_schema_cannot_be_mistaken_for_absent_client(fixture,settings):
    state,sub,call=fixture
    state['inbound']={'id':7,'protocol':'vless','settings':settings}
    with pytest.raises(PanelError,match='Schema'):call()
    assert state['writes']==[]

def test_duplicate_uuid_refuses_before_write(fixture):
    state,sub,call=fixture
    state['records']=[{'id':sub['id']},{'id':sub['id']}]
    with pytest.raises(PanelError,match='Ambiguous'):call()
    assert state['writes']==[]

@pytest.mark.parametrize('inbound_id',[8,True,'7'])
def test_wrong_inbound_refuses_before_write(fixture,inbound_id):
    state,sub,call=fixture
    state['inbound']={'id':inbound_id,'protocol':'vless','settings':{'clients':[]}}
    with pytest.raises(PanelError,match='Inbound'):call()
    assert state['writes']==[]

def test_timeout_after_remote_commit_retries_same_uuid(fixture):
    state,sub,call=fixture;state['drop']=True
    with pytest.raises(httpx.ReadTimeout):call()
    assert len(state['records'])==1
    call()
    assert len(state['records'])==1
    assert state['writes'][0]['id']==state['writes'][1]['id']==sub['id']

def test_absent_revoke_does_not_create_and_removed_revoke_is_confirmed(fixture):
    state,sub,call=fixture
    assert call(False)=='' and state['writes']==[]
    call();state['after']=lambda state,payload:state['records'].clear()
    assert call(False)=='' and state['records']==[]

def test_ignored_revoke_is_not_success(fixture):
    state,sub,call=fixture;call()
    state['after']=lambda *args:None
    with pytest.raises(PanelError,match='NotConfirmed'):call(False)
    assert state['records'][0]['enable'] is True

from test_shop import client, register, order, confirm

def test_unconfirmed_panel_write_keeps_paid_order_in_retry_queue(client,fixture,monkeypatch):
    from app import providers, service, config as c
    from app.db import execute, one
    state,_,_=fixture
    profile={'url':'https://panel.example/private','inbound_id':7,'username':'fixture','password':'secret',
             'flow':'','link_template':'vless://{uuid}@vpn.example:443'}
    execute("UPDATE servers SET settings=? WHERE id='test-xui'",(c.encrypt(json.dumps(profile)),))
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(state['handler']),**kw))
    state['after']=lambda *args:None
    register(client);purchase=order(client);assert confirm(client,purchase['id']).status_code==200
    asyncio.run(service.tick())
    pending=one('SELECT * FROM orders WHERE id=?',(purchase['id'],))
    sub=one('SELECT * FROM subscriptions')
    assert pending['status']=='paid' and pending['attempts']==1 and pending['error']=='PanelError'
    assert sub['state']=='pending' and sub['secret'] is None
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==404
    state['after']=None
    execute('UPDATE orders SET next_attempt=0 WHERE id=?',(purchase['id'],))
    asyncio.run(service.tick())
    active=one('SELECT * FROM orders WHERE id=?',(purchase['id'],))
    ready=one('SELECT * FROM subscriptions')
    assert active['status']=='active' and ready['expires']==pending['target_expires']
    assert active['target_expires']==pending['target_expires']
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200
    assert len(state['records'])==1 and len({p['id'] for p in state['writes']})==1

def test_expiry_is_not_marked_revoked_until_panel_confirms(client,fixture,monkeypatch):
    from app import providers, service, config as c
    from app.db import execute, one
    state,_,_=fixture
    profile={'url':'https://panel.example/private','inbound_id':7,'username':'fixture','password':'secret',
             'flow':'','link_template':'vless://{uuid}@vpn.example:443'}
    execute("UPDATE servers SET settings=? WHERE id='test-xui'",(c.encrypt(json.dumps(profile)),))
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(state['handler']),**kw))
    register(client);purchase=order(client);confirm(client,purchase['id']);asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions')['state']=='active'
    execute('UPDATE subscriptions SET expires=1')
    state['after']=lambda *args:None
    asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions')['state']=='active'
    assert state['records'][0]['enable'] is True
    state['after']=None
    asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions')['state']=='expired'
    assert state['records'][0]['enable'] is False


def test_health_probe_refuses_unknown_schema_without_remote_write(fixture):
    state,_,_=fixture
    state['inbound']={'id':7,'protocol':'vless','settings':{'clients':None}}
    with pytest.raises(PanelError,match='Schema'):state['probe']()
    assert state['writes']==[]
