import hmac
import json
import time
from uuid import uuid4

from app import cabinet_stars as shared
from app.db import execute, one
from test_shop import client
from test_wg_peer_diagnostics import setup_user


def test_paid_vpn_bridge_issues_reserves_settles_and_refunds_once(client,monkeypatch):
    setup_user(client)
    subject, tg, key = str(uuid4()), 901, 'dc'*32
    execute('UPDATE users SET telegram_id=? WHERE id=1',(tg,))
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(subject,tg,int(time.time())))
    monkeypatch.setattr(shared.mtproxy_web_login,'enabled',lambda:True)
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',key)
    link='https://t.me/$fixture-vpn'
    issued=[]
    class InvoiceClient:
        def __init__(self, **kw): assert kw['trust_env'] is False and not kw['follow_redirects']
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,content,headers):
            assert url=='https://pushok.shop/api/v1/shop/vpn-stars/invoice'
            stamp,sig=headers['X-Pushok-Management'].split(':')
            assert sig==hmac.digest(bytes.fromhex(key),shared.DOMAIN+stamp.encode()+b':invoice:'+content,'sha256').hex()
            issued.append(json.loads(content));return self
        def raise_for_status(self):pass
        def json(self):return {'url':link}
    monkeypatch.setattr(shared.httpx,'AsyncClient',InvoiceClient)
    plan=client.get('/api/catalog').json()['plans'][0]
    purchase={'plan':plan['id'],'protocol':'wg','accept_terms':True}
    response=client.post('/api/orders',json=purchase)
    assert response.status_code==200,response.text
    order=response.json(); assert order['payment_url']==link and issued==[{'order_id':order['id'],'days':plan['days'],'amount':order['amount']}]
    assert client.post('/api/orders',json=purchase).json()['id']==order['id'] and len(issued)==1
    base={'invoice_payload':'shop-vpn:'+order['id'],'currency':'XTR','total_amount':order['amount'],'telegram_user_id':tg}
    def callback(op,fields,key_override=key):
        raw=json.dumps({**base,**fields}).encode();stamp=str(int(time.time()))
        sig=hmac.digest(bytes.fromhex(key_override),shared.DOMAIN+stamp.encode()+b':'+op.encode()+b':'+raw,'sha256').hex()
        return client.post('/api/flow/cabinet-stars/'+op,content=raw,headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+sig})
    assert callback('precheckout',{'query_id':'query'},'fc'*32).status_code==403
    assert callback('precheckout',{'query_id':'query','telegram_user_id':tg+1}).json()=={'ok':False}
    assert callback('precheckout',{'query_id':'query','total_amount':1}).json()=={'ok':False}
    assert callback('precheckout',{'query_id':'query'}).json()=={'ok':True}
    event={'telegram_payment_charge_id':'fixture-charge'}
    assert callback('settle',event).json()=={'status':'applied'}
    assert callback('settle',event).json()=={'status':'already_processed'}
    assert one('SELECT status FROM orders WHERE id=?',(order['id'],))['status']=='paid'
    assert callback('settle',{'telegram_payment_charge_id':'different'}).status_code==409
    refund={**event,'occurred_at':int(time.time())}
    assert callback('refund',refund).json()==callback('refund',refund).json()=={'status':'recorded'}
    assert one('SELECT count(*) AS n FROM shop_finance_entries')['n']==1
    assert one("SELECT count(*) AS n FROM cabinet_vpn_notices WHERE kind='refund'")['n']==1
