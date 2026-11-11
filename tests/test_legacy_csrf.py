import asyncio
import json
import httpx
from app.panel import Legacy


def test_csrf_from_read_is_forwarded_and_base_path_preserved():
    saved=[]
    def handler(request):
        assert request.url.path.startswith('/secret/')
        if request.url.path.endswith('/login'):
            return httpx.Response(200,json={'success':True})
        if request.method=='GET':
            return httpx.Response(200,headers={'X-CSRF-Token':'session-token'},json={'success':True,'obj':{'protocol':'vless','id':7,'settings':json.dumps({'clients':saved})}})
        assert request.headers['X-CSRF-Token']=='session-token'
        from urllib.parse import parse_qs
        saved[:]=json.loads(parse_qs(request.content.decode())['settings'][0])['clients']
        return httpx.Response(200,json={'success':True})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=Legacy({'url':'https://panel.example/secret','inbound_id':7,'username':'user','password':'secret','flow':'','link_template':'vless://{uuid}@vpn.example'})
            await adapter.apply(client,{'id':'stable-id','expires':2000000000},True)
            await adapter.apply(client,{'id':'stable-id','expires':2000000100},False)
    asyncio.run(execute())
    assert len(saved)==1 and saved[0]['enable'] is False
