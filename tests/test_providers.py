import asyncio, json
import httpx
from app import providers


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
    sub={'id':'stable-id','expires':2000000000,'protocol':'xui'}
    a=asyncio.run(providers.provision(sub))
    b=asyncio.run(providers.provision(sub))
    assert a==b=='vless://stable-id@vpn.example:443?security=tls'
    assert actions[0].endswith('/addClient') and actions[1].endswith('/updateClient/stable-id')
    assert saved[0]['expiryTime']==2000000000000
    asyncio.run(providers.disable(sub))
    assert saved[0]['enable'] is False


def test_wireguard_provider_passes_stable_id_and_expiry(monkeypatch):
    monkeypatch.setenv('WG_AGENT_URL','https://wg.example')
    monkeypatch.setenv('WG_AGENT_TOKEN','agent-secret')
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'config':'[Interface]\nPrivateKey = test'})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    sub={'id':'stable-id','expires':2000000000,'protocol':'wg'}
    assert asyncio.run(providers.provision(sub)).startswith('[Interface]')
    assert calls[0].url.path=='/peers/stable-id'
    assert json.loads(calls[0].content)['expires']==2000000000
    assert calls[0].headers['authorization']=='Bearer agent-secret'


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
