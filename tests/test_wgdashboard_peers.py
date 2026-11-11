import asyncio
import base64
import json
import httpx
import pytest
from app.wgdashboard import WGDashboard
from app.panel import PanelError

PRIVATE = base64.b64encode(bytes(range(32))).decode()
PROFILE = {'url':'https://wg.example/prefix','token':'t'*40,'configuration':'wg0'}


def run_case(*, lost=False, ignore_delete=False, restricted=False):
    adapter = WGDashboard(PROFILE)
    key = adapter.identity(PRIVATE)
    rows = []
    writes = []
    info = {'Name':'wg0','Protocol':'wg','Status':True}
    def handler(request):
        assert request.headers['wg-dashboard-apikey'] == PROFILE['token']
        endpoint = request.url.path.split('/api/')[1]
        if endpoint == 'getWireguardConfigurations': data = [info]
        elif endpoint == 'getWireguardConfigurationInfo':
            data = {'configurationInfo':info,'configurationPeers':[] if restricted else rows.copy(),
                    'configurationRestrictedPeers':rows.copy() if restricted else []}
        elif endpoint == 'addPeers/wg0':
            writes.append(endpoint)
            body = json.loads(request.content)
            assert body['public_key'] == key and body['private_key'] == PRIVATE
            rows.append({'id':key})
            if lost:raise httpx.ReadTimeout('lost',request=request)
            data = None
        elif endpoint == 'deletePeers/wg0':
            writes.append(endpoint)
            assert json.loads(request.content) == {'peers':[key]}
            if not ignore_delete:rows.clear()
            data = None
        else:pytest.fail(endpoint)
        return httpx.Response(200,json={'status':True,'data':data})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if lost:
                with pytest.raises(httpx.ReadTimeout):await adapter.ensure_peer(client,PRIVATE,'pushok-test')
            if restricted:
                rows.append({'id':key})
                with pytest.raises(PanelError,match='Restricted'):await adapter.ensure_peer(client,PRIVATE,'pushok-test')
                assert writes == []
                return
            assert await adapter.ensure_peer(client,PRIVATE,'pushok-test') == key
            assert await adapter.ensure_peer(client,PRIVATE,'pushok-test') == key
            if ignore_delete:
                with pytest.raises(PanelError,match='DeleteNotConfirmed'):await adapter.revoke_peer(client,key)
                return
            await adapter.revoke_peer(client,key)
            await adapter.revoke_peer(client,key)
            assert rows == [] and writes == ['addPeers/wg0','deletePeers/wg0']
    asyncio.run(scenario())


def test_create_retry_and_revoke_are_idempotent():run_case()
def test_lost_creation_response_reuses_public_key():run_case(lost=True)
def test_unconfirmed_deletion_is_an_error():run_case(ignore_delete=True)
def test_restricted_peer_is_not_silently_enabled():run_case(restricted=True)


@pytest.mark.parametrize('key',['bad',None,'a'*44])
def test_bad_private_key_fails_without_network(key):
    with pytest.raises(PanelError):WGDashboard.identity(key)
