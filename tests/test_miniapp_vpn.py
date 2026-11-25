import hashlib
import hmac
import json
import time
from uuid import uuid4

from app import cabinet_stars, config, miniapp_vpn
from app.db import one, execute
from test_shop import client


def test_cabinet_checkout_is_private_and_replay_safe(client,monkeypatch):
    secret='6a'*32
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',secret)
    monkeypatch.setattr(config,'SECURE_COOKIE',True)
    monkeypatch.setattr(config,'PUBLIC_URL','https://pushok.shop/shop')
    monkeypatch.setattr(cabinet_stars.mtproxy_web_login,'enabled',lambda:True)
    issued=[]
    async def invoice(order):
        issued.append(order['id'])
        return 'https://t.me/$fixture'
    monkeypatch.setattr(cabinet_stars,'invoice',invoice)
    identity={'telegram_id':1234,'subject':str(uuid4())}
    def post(action,add=None,who=identity,nonce=None,valid=True):
        body=json.dumps({**who,'nonce':nonce or str(uuid4()),**(add or {})}).encode()
        stamp=str(int(time.time()))
        signature=hmac.digest(bytes.fromhex(secret),miniapp_vpn.DOMAIN+stamp.encode()+b':'+action.encode()+b':'+body,'sha256').hex()
        return client.post('/api/flow/miniapp-vpn/'+action,content=body,headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+(signature if valid else '0'*64)})
    assert post('plans',valid=False).status_code==403
    nonce=str(uuid4())
    catalog=post('plans',nonce=nonce).json()
    assert post('plans',nonce=nonce).status_code==409
    assert catalog['available'] and catalog['terms'] and all(p['amount']>0 for p in catalog['plans'])
    p=catalog['plans'][0]
    choice={'plan':p['id'],'plan_version':p['version'],'amount':p['amount'],'policy_version':catalog['policy_version'],'accept_terms':True,'request_id':str(uuid4())}
    assert post('checkout',{'choice':{**choice,'amount':p['amount']+1}}).status_code==409
    order=post('checkout',{'choice':choice}).json()
    assert order['payment_url']=='https://t.me/$fixture' and order['currency']=='XTR'
    assert post('checkout',{'choice':choice}).json()['id']==order['id'] and issued==[order['id']]
    assert post('status',{'order_id':order['id']},who={'telegram_id':5678,'subject':str(uuid4())}).status_code==404
    execute("UPDATE orders SET status='active' WHERE id=?",(order['id'],))
    assert post('checkout',{'choice':choice}).json()['status']=='active'
    assert post('status',{'order_id':order['id']}).json()['status']=='active'
    assert one('SELECT count(*) AS n FROM orders')['n']==1
