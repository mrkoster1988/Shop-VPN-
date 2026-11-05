import asyncio,time
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_shop import client,register,admin_headers,confirm
from app import config as c,providers,service
from app.db import one,execute,db,init_db

@pytest.fixture
def buyer(client):
    register(client);return client

def coupon(client,**changes):
    body={'code':'SAVE10','percent':10,'enabled':True,'max_uses':100,'expires_at':0,'plan_id':None,'protocol':'all',**changes}
    result=client.post('/api/admin/coupons',json=body,headers=admin_headers())
    assert result.status_code==201,result.text
    return result.json()

def edit(client,row,**changes):
    body={k:row[k] for k in ('percent','enabled','max_uses','expires_at','plan_id','protocol','version')};body.update(changes)
    return client.put('/api/admin/coupons/'+row['id'],json=body,headers=admin_headers())

def quote(client,code='SAVE10',**changes):
    return client.post('/api/quote',json={'plan':'month','protocol':'xui','plan_version':1,'promo_code':code,**changes})

def purchase(client,q,**changes):
    body={'plan':q['plan'],'protocol':q['protocol'],'plan_version':q['plan_version'],'promo_code':q['promo_code'],'promo_version':q['promo_version'],'quoted_amount':q['amount'],'quote_currency':q['currency'],'accept_terms':True,**changes}
    return client.post('/api/orders',json=body)


def test_quote_and_order_use_server_integer_price_and_snapshot(buyer,monkeypatch):
    client=buyer;row=coupon(client);result=quote(client,' save10 ');assert result.status_code==200
    q=result.json();assert (q['base_amount'],q['discount'],q['amount'],q['currency'])==(199,19,180,'RUB')
    assert one('SELECT count(*) AS n FROM coupon_uses')['n']==0
    made=purchase(client,q);assert made.status_code==200,made.text
    assert made.json()['amount']==180 and made.json()['promo_code']=='SAVE10'
    listed=client.get('/api/admin/orders',headers=admin_headers()).json()['orders'][0]
    assert listed['promo_code']=='SAVE10' and listed['discount']==19
    assert one('SELECT promo_percent,base_amount,discount,amount FROM orders')=={'promo_percent':10,'base_amount':199,'discount':19,'amount':180}
    assert purchase(client,q).json()['id']==made.json()['id'] and one('SELECT count(*) AS n FROM coupon_uses')['n']==1
    calls=[]
    async def provision(sub):calls.append(sub);return 'vless://'+sub['id']+'@vpn.example'
    monkeypatch.setattr(providers,'provision',provision);confirm(client,made.json()['id']);asyncio.run(service.fulfill(made.json()['id']))
    assert calls[0]['expires']>=time.time()+30*86400-20
    assert client.get('/api/me').json()['orders'][0]['discount']==19


def test_coupon_order_replay_survives_edits_expiry_and_plan_hiding(buyer):
    client=buyer;row=coupon(client);q=quote(client).json();first=purchase(client,q).json()
    assert edit(client,row,percent=50,enabled=False,expires_at=1).status_code==200
    execute("UPDATE plans SET price=999,version=2,enabled=0 WHERE id='month'")
    assert purchase(client,q).json()['id']==first['id']
    assert client.get('/api/orders/'+first['id']).json()['amount']==180
    assert one('SELECT count(*) AS n FROM coupon_uses')['n']==1


def test_changed_coupon_or_plan_requires_new_confirmation_without_order(buyer):
    client=buyer;row=coupon(client);q=quote(client).json();edit(client,row,percent=20)
    assert purchase(client,q).status_code==409 and one('SELECT count(*) AS n FROM orders')['n']==0
    q=quote(client).json();execute("UPDATE plans SET price=200,version=2 WHERE id='month'")
    assert purchase(client,q).status_code==409 and one('SELECT count(*) AS n FROM coupon_uses')['n']==0

@pytest.mark.parametrize('changes',[{'quoted_amount':1},{'quote_currency':'XTR'},{'promo_version':None},{'quoted_amount':None}])
def test_client_cannot_forge_discount_or_skip_approval(buyer,changes):
    coupon(buyer);q=quote(buyer).json()
    assert purchase(buyer,q,**changes).status_code in (409,422)
    assert one('SELECT count(*) AS n FROM orders')['n']==0


def test_full_price_and_discount_orders_are_not_reused_for_each_other(buyer):
    coupon(buyer);full=buyer.post('/api/orders',json={'plan':'month','protocol':'xui'}).json();q=quote(buyer).json();reduced=purchase(buyer,q).json()
    assert reduced['id']!=full['id'] and reduced['amount']==180
    assert buyer.post('/api/orders',json={'plan':'month','protocol':'xui'}).json()['id']==full['id']
    assert full['amount']==199

@pytest.mark.parametrize('settings,quote_args',[({'enabled':False},{}),({'expires_at':1},{}),({'plan_id':'year'},{}),({'protocol':'wg'},{})])
def test_disabled_expired_or_wrong_scope_cannot_be_used(buyer,settings,quote_args):
    coupon(buyer,**settings);assert quote(buyer,**quote_args).status_code==409
    assert one('SELECT count(*) AS n FROM orders')['n']==0


def test_global_limit_is_reserved_atomically_across_accounts(buyer):
    client=buyer;coupon(client,max_uses=1);q1=quote(client).json();cookie1=client.cookies.get('session')
    client.post('/api/auth/logout');register(client,'second@example.com');cookie2=client.cookies.get('session');q2=quote(client).json()
    # Create another live session for the first account after logging it out.
    client.post('/api/auth/login',json={'email':'user@example.com','password':'correct-horse-123'});cookie1=client.cookies.get('session')
    def request(item):
        q,cookie=item
        body={'plan':q['plan'],'protocol':q['protocol'],'plan_version':q['plan_version'],'promo_code':q['promo_code'],'promo_version':q['promo_version'],'quoted_amount':q['amount'],'quote_currency':q['currency']}
        return client.post('/api/orders',json=body,headers={'Cookie':'session='+cookie})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(request,[(q1,cookie1),(q2,cookie2)]))
    assert sorted(r.status_code for r in results)==[200,409]
    assert one('SELECT count(*) AS n FROM coupon_uses')['n']==1 and one('SELECT count(*) AS n FROM orders')['n']==1


def test_canceled_order_does_not_reset_account_or_global_use(buyer):
    row=coupon(buyer);q=quote(buyer).json();order=purchase(buyer,q).json()
    assert buyer.post('/api/admin/orders/'+order['id']+'/reject',headers=admin_headers()).status_code==200
    assert purchase(buyer,q).status_code==409 and quote(buyer).status_code==409
    assert edit(buyer,row,max_uses=1000).json()['used']==1


def test_invoice_failure_retries_same_coupon_order_at_saved_amount(buyer,monkeypatch):
    monkeypatch.setattr(c,'PAYMENT_MODE','yookassa');coupon(buyer);q=quote(buyer).json();calls=[]
    async def payment(order,email):
        calls.append(dict(order))
        if len(calls)==1:raise TimeoutError('private-upstream')
        return {'id':'payment-id','confirmation':{'confirmation_url':'https://pay.example'}}
    monkeypatch.setattr(providers,'payment',payment)
    assert purchase(buyer,q).status_code==502
    result=purchase(buyer,q);assert result.status_code==200
    assert calls[0]['id']==calls[1]['id'] and calls[1]['amount']==180
    assert one('SELECT count(*) AS n FROM coupon_uses')['n']==1


def test_stars_invoice_uses_discount_and_completed_replay_never_reissues(client,monkeypatch):
    monkeypatch.setattr(c,'BOT_TOKEN','123:test');execute('UPDATE store_settings SET terms_text=? WHERE id=1',('Terms',))
    user=service.telegram_account(123);client.get(service.ticket(user['id']),follow_redirects=False)
    row=coupon(client);q=quote(client).json();assert q['currency']=='XTR' and q['amount']==135 and q['discount']==15
    calls=[]
    async def invoice(order):calls.append(dict(order));return 'https://t.me/invoice'
    monkeypatch.setattr(providers,'stars_invoice',invoice);result=purchase(client,q);assert result.status_code==200 and calls[0]['amount']==135
    execute("UPDATE orders SET status='active',payment_url=NULL")
    assert purchase(client,q).json()['status']=='active' and len(calls)==1


def test_small_price_never_becomes_free(buyer):
    execute("UPDATE plans SET price=1 WHERE id='month'");coupon(buyer,percent=90)
    q=quote(buyer).json();assert q['amount']==1 and q['discount']==0
    assert purchase(buyer,q).json()['amount']==1


def test_admin_authorization_origin_identity_and_audit(buyer):
    assert buyer.get('/api/admin/coupons').status_code==401
    assert buyer.post('/api/admin/coupons',json={'code':'SAVE10','percent':10}).status_code==401
    assert buyer.post('/api/admin/coupons',headers={**admin_headers(),'Origin':'https://evil.example'},json={'code':'SAVE10','percent':10}).status_code==403
    row=coupon(buyer,code='save10');assert row['code']=='SAVE10'
    assert buyer.post('/api/admin/coupons',headers=admin_headers(),json={'code':'SaVe10','percent':20}).status_code==409
    changed=edit(buyer,row,percent=15).json();assert changed['version']==2
    assert edit(buyer,row,percent=30).status_code==409
    assert one('SELECT count(*) AS n FROM coupon_changes')['n']==2
    init_db();assert buyer.get('/api/admin/coupons',headers=admin_headers()).json()['coupons'][0]['percent']==15

@pytest.mark.parametrize('body',[{'code':'A','percent':10},{'code':'BAD CODE','percent':10},{'code':'SAVE10','percent':0},{'code':'SAVE10','percent':100},{'code':'SAVE10','percent':True},{'code':'SAVE10','percent':10,'max_uses':0},{'code':'SAVE10','percent':10,'plan_id':'missing'}])
def test_invalid_campaign_creates_no_coupon(buyer,body):
    assert buyer.post('/api/admin/coupons',headers=admin_headers(),json=body).status_code==422
    assert one('SELECT count(*) AS n FROM coupons')['n']==0


def test_quote_requires_login_and_unknown_code_is_safe(client):
    assert quote(client).status_code==401;register(client);assert quote(client).status_code==409


def test_old_orders_migrate_as_full_price_without_coupon(buyer):
    result=buyer.post('/api/orders',json={'plan':'month','protocol':'xui'}).json()
    with db() as conn:
        for name in ('base_amount','discount','promo_id','promo_code','promo_version','promo_percent'):conn.execute('ALTER TABLE orders DROP COLUMN '+name)
    init_db();row=one('SELECT * FROM orders');assert row['base_amount']==row['amount']==199 and row['discount']==0 and row['promo_code'] is None
    assert buyer.get('/api/orders/'+result['id']).json()['amount']==199


def test_promo_routes_under_shop_prefix(buyer,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    row=coupon(buyer);monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'COOKIE_PATH','/shop/');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        shop.cookies.update(buyer.cookies)
        q=shop.post('/shop/api/quote',json={'plan':'month','protocol':'xui','plan_version':1,'promo_code':'SAVE10'}).json()
        result=shop.post('/shop/api/orders',json={'plan':'month','protocol':'xui','plan_version':1,'promo_code':'SAVE10','promo_version':q['promo_version'],'quoted_amount':q['amount'],'quote_currency':'RUB'})
        assert result.status_code==200 and result.json()['amount']==180
        assert shop.get('/shop/api/admin/coupons',headers=admin_headers()).json()['coupons'][0]['used']==1


def test_discounted_yookassa_request_and_receipt_use_saved_total(buyer,monkeypatch):
    import json,httpx
    monkeypatch.setattr(c,'PAYMENT_MODE','yookassa');monkeypatch.setenv('YOOKASSA_SHOP_ID','123');monkeypatch.setenv('YOOKASSA_SECRET','test-secret');monkeypatch.setenv('YOOKASSA_RECEIPTS','true')
    coupon(buyer);q=quote(buyer).json();requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'id':'payment-id','confirmation':{'confirmation_url':'https://pay.example'}})
    cls=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    result=purchase(buyer,q);assert result.status_code==200
    body=json.loads(requests[0].content)
    assert body['amount']=={'value':'180.00','currency':'RUB'} and body['receipt']['items'][0]['amount']==body['amount']
    assert requests[0].headers['idempotence-key']==result.json()['id']

def test_partner_offer_percent_is_configured_by_owner_and_snapshot_survives_change(buyer):
    client=buyer;row=coupon(client,code='PARTNER15',percent=15)
    result=quote(client,'PARTNER15');assert result.status_code==200
    offer=result.json();assert (offer['promo_percent'],offer['discount'],offer['amount'])==(15,29,170)
    placed=purchase(client,offer);assert placed.status_code==200 and placed.json()['amount']==170
    changed=edit(client,row,percent=20)
    assert changed.status_code==200 and changed.json()['percent']==20
    saved=client.get('/api/orders/'+placed.json()['id']).json()
    assert saved['amount']==170 and saved['promo_percent']==15
