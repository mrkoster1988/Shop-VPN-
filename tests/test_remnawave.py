"""Контракт Remnawave v3: локальный HTTP-стенд, без настоящей панели."""
import asyncio
import copy
import json
import httpx
import pytest
from app.panel import Remnawave, PanelError

SUB = {'id':'123e4567-e89b-42d3-a456-426614174000','expires':1900000000}
SQUAD = '123e4567-e89b-42d3-a456-426614174001'
PROFILE = {'url':'https://panel.example','token':'t'*40,'squad_uuids':[SQUAD]}


def lifecycle(*,lost=False, bad_squads=False, bad_disable=False):
    adapter = Remnawave(PROFILE)
    state = {}
    calls = []
    def handler(request):
        assert request.headers['authorization'] == 'Bearer '+PROFILE['token']
        calls.append((request.method, request.url.path))
        if request.method == 'GET' and request.url.path == '/api/users':
            data = {'users':[], 'total':0}
        elif '/by-username/' in request.url.path:
            if not state:return httpx.Response(404,json={})
            data = copy.deepcopy(state)
            if bad_squads:data['activeInternalSquads'] = []
        elif request.method == 'POST' and request.url.path == '/api/users':
            body = json.loads(request.content)
            assert body['vlessUuid'] == SUB['id'] and body['activeInternalSquads'] == [SQUAD]
            state.update(body,id=7,subscriptionUrl='https://sub.example/safe',
                         activeInternalSquads=[{'uuid':SQUAD,'name':'Customers'}])
            if lost:raise httpx.ReadTimeout('lost',request=request)
            data = state
        elif request.method == 'PATCH':
            body = json.loads(request.content)
            assert body['username'] == adapter.username(SUB['id'])
            assert 'vlessUuid' not in body and 'activeInternalSquads' not in body
            state.update(body)
            data = state
        elif request.url.path.endswith('/actions/disable'):
            state['status'] = 'DISABLED'
            data = state
        elif request.url.path == '/api/users/7':
            data = dict(state)
            if bad_disable:data['username'] = 'another-user'
        else:pytest.fail(str(request.url))
        return httpx.Response(200,json={'response':data})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if lost:
                with pytest.raises(httpx.ReadTimeout):await adapter.apply(client,SUB)
            if bad_squads:
                with pytest.raises(PanelError,match='SquadsNotConfirmed'):await adapter.apply(client,SUB)
                return
            assert await adapter.apply(client,SUB) == 'https://sub.example/safe'
            renewed = SUB | {'expires':SUB['expires']+86400}
            await adapter.apply(client,renewed)
            if bad_disable:
                with pytest.raises(PanelError,match='DisableNotConfirmed'):await adapter.apply(client,renewed,False)
                return
            await adapter.apply(client,renewed,False)
            await adapter.apply(client,renewed,False)
            assert state['status'] == 'DISABLED'
            assert calls.count(('POST','/api/users')) == 1
            assert calls.count(('POST','/api/users/7/actions/disable')) == 1
    asyncio.run(run())


def test_create_renew_disable_and_repeat():lifecycle()
def test_lost_create_response_does_not_duplicate_user():lifecycle(lost=True)
def test_squad_assignment_must_be_confirmed():lifecycle(bad_squads=True)
def test_disable_readback_must_belong_to_same_user():lifecycle(bad_disable=True)


def test_equivalent_utc_date_and_identity_validation():
    adapter = Remnawave(PROFILE)
    row = {'id':7,'username':adapter.username(SUB['id']),'vlessUuid':SUB['id'],
        'expireAt':adapter.expiry(SUB['expires']).replace('.000Z','+00:00'),
        'status':'ACTIVE','subscriptionUrl':'https://sub.example/safe','activeInternalSquads':[{'uuid':SQUAD}]}
    assert adapter.verify_identity(row,row['username'],SUB) == row['subscriptionUrl']
    with pytest.raises(PanelError):adapter.username('123e4567e89b42d3a456426614174000')
