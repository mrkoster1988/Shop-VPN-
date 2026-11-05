import asyncio
import concurrent.futures
import time
import uuid
import pytest
from app import config as c, service, providers
from app.db import db, one, execute, init_db
from app.security import digest
from test_shop import client


def admin():return {'Authorization':'Bearer '+c.ADMIN_TOKEN}

def user(client):
    uid=execute('INSERT INTO users(email,created) VALUES (?,?)',(str(uuid.uuid4())+'@example.com',int(time.time())))
    login(client,uid)
    return uid


def login(client,uid):client.get(service.ticket(uid))


def policy(client,**changes):
    p=client.get('/api/admin/referrals',headers=admin()).json()
    fields={k:p[k] for k in ('enabled','days','max_invites','min_rub','min_stars','version')};fields.update(changes)
    return client.put('/api/admin/referrals',headers=admin(),json=fields)


@pytest.fixture
def campaign(client):
    owner=user(client);sid=str(uuid.uuid4())
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret,server_id,device_limit) VALUES (?,?,'xui',?,'active',?,'test-xui',6)",(sid,owner,int(time.time())+86400,c.encrypt('vless://old')))
    assert policy(client,enabled=True).status_code==200
    code=client.post('/api/referrals/code').json()['code']
    return client,owner,sid,code


def bind(client,code,**changes):
    return client.post('/api/referrals/bind',json={'code':code,'policy_version':2,'accept_terms':True,**changes})


def order(client,**changes):
    r=client.post('/api/orders',json={'plan':'month','protocol':'xui','plan_version':1,**changes});assert r.status_code==200,r.text
    return r.json()['id']


def confirm(client,oid):return client.post('/api/admin/orders/'+oid+'/confirm',headers=admin())


def paid_referral(campaign):
    client,owner,sid,code=campaign;invitee=user(client)
    assert bind(client,code).status_code==200
    oid=order(client);assert confirm(client,oid).status_code==200
    return invitee,oid


def redeem(client,sid,days=7,request=None):
    return client.post('/api/referrals/redeem',json={'subscription_id':sid,'request_id':request or str(uuid.uuid4()),'days':days})


def test_disabled_defaults_auth_and_version(client):
    assert client.get('/api/admin/referrals').status_code==401
    for path in ('code','bind','redeem'):assert client.post('/api/referrals/'+path,json={}).status_code==401
    p=client.get('/api/admin/referrals',headers=admin()).json()
    assert not p['enabled'] and (p['days'],p['used'],p['min_rub'],p['min_stars'])==(7,0,199,150)
    user(client);assert client.post('/api/referrals/code').status_code==409
    assert bind(client,'a'*24,policy_version=1).status_code==409
    assert policy(client,enabled=True).status_code==200
    assert client.put('/api/admin/referrals',headers=admin(),json={k:p[k] for k in ('enabled','days','max_invites','min_rub','min_stars','version')}).status_code==409
    assert one('SELECT count(*) AS n FROM referral_policy_changes')['n']==1
    assert client.put('/api/admin/referrals',headers={**admin(),'Origin':'https://evil.example'},json={}).status_code==403


@pytest.mark.parametrize('field,value',[('days',0),('days',31),('days',True),('max_invites',0),('min_rub',0),('min_stars',0),('enabled',1)])
def test_policy_bounds(client,field,value):assert policy(client,**{field:value}).status_code==422


def test_codes_paid_accounts_only_and_stable(campaign):
    client,owner,sid,code=campaign
    assert len(code)==24 and client.post('/api/referrals/code').json()['code']==code
    s=client.get('/api/me').json()['referrals'];assert s['url']==c.PUBLIC_URL+'/?ref='+code and s['can_share']
    user(client);assert client.post('/api/referrals/code').status_code==409
    login(client,owner);execute("UPDATE subscriptions SET access_source='trial' WHERE id=?",(sid,))
    assert client.post('/api/referrals/code').status_code==409


def test_binding_immutable_and_private(campaign):
    client,owner,sid,code=campaign
    assert bind(client,code).status_code==409  # self
    invitee=user(client)
    assert bind(client,code,accept_terms=False).status_code==422
    assert bind(client,code,policy_version=1).status_code==409
    assert bind(client,'f'*24).status_code==409
    first=bind(client,code);assert first.status_code==200
    assert first.json()['bound']=={'days':7,'min_rub':199,'min_stars':150,'policy_version':2,'created':one('SELECT created FROM referral_bindings')['created'],'rewarded':False}
    assert bind(client,code).status_code==200
    assert bind(client,'f'*24).status_code==409
    assert one('SELECT count(*) AS n FROM referral_bindings')['n']==1
    assert first.json()['balance_days']==0


@pytest.mark.parametrize('history',['order','trial','subscription'])
def test_bind_before_any_order_or_subscription(campaign,history):
    client,owner,sid,code=campaign;uid=user(client)
    if history=='subscription':
        execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state) VALUES (?,?,'wg',0,'expired')",(str(uuid.uuid4()),uid))
    else:
        oid=order(client)
        if history=='trial':execute("UPDATE orders SET kind='trial',amount=0 WHERE id=?",(oid,))
    assert bind(client,code).status_code==409


def test_invitation_reserves_budget_and_promises_survive_edits(campaign):
    client,owner,sid,code=campaign
    assert policy(client,max_invites=1).status_code==200
    uid=user(client);assert bind(client,code,policy_version=3).status_code==200
    assert policy(client,enabled=False,days=30,min_rub=99999).status_code==200
    assert bind(client,code).status_code==200  # original replay
    oid=order(client);confirm(client,oid)
    assert one('SELECT days FROM referral_rewards')['days']==7
    user(client);assert bind(client,code,policy_version=4).status_code==409
    assert policy(client,enabled=True).status_code==200
    assert bind(client,code,policy_version=5).status_code==409


def test_expired_inviter_cannot_accept_new_bind(campaign):
    client,owner,sid,code=campaign;user(client)
    execute('UPDATE subscriptions SET expires=0 WHERE id=?',(sid,))
    assert bind(client,code).status_code==409


def test_manual_paid_once_not_on_pending_or_rejected(campaign):
    client,owner,sid,code=campaign;invitee=user(client);bind(client,code)
    oid=order(client);assert not one('SELECT * FROM referral_rewards')
    assert client.post('/api/admin/orders/'+oid+'/reject',headers=admin()).status_code==200
    assert confirm(client,oid).status_code==409
    oid=order(client);confirm(client,oid);confirm(client,oid)
    assert one('SELECT count(*) AS n FROM referral_rewards')['n']==1
    execute("UPDATE orders SET status='active' WHERE id=?",(oid,))
    next_order=order(client);confirm(client,next_order)
    assert one('SELECT order_id,days FROM referral_rewards')=={'order_id':oid,'days':7}
    login(client,owner);s=client.get('/api/me').json()['referrals']
    assert (s['earned_days'],s['balance_days'],s['invited'],s['rewarded'])==(7,7,1,1)
    assert all('invitee_id' not in h for h in s['history'])


def test_discounted_amount_below_threshold_waits_for_qualifying_payment(campaign):
    client,owner,sid,code=campaign;user(client);bind(client,code)
    oid=order(client);execute('UPDATE orders SET amount=180,discount=19 WHERE id=?',(oid,));confirm(client,oid)
    assert not one('SELECT * FROM referral_rewards')
    next_order=order(client,plan='quarter');confirm(client,next_order)
    assert one('SELECT order_id FROM referral_rewards')['order_id']==next_order


def test_trial_and_bonus_never_award(campaign):
    client,owner,sid,code=campaign;user(client);bind(client,code)
    oid=order(client);execute("UPDATE orders SET kind='trial',amount=0 WHERE id=?",(oid,));confirm(client,oid)
    assert not one('SELECT * FROM referral_rewards')
    oid=order(client);execute("UPDATE orders SET kind='referral',amount=0 WHERE id=?",(oid,));confirm(client,oid)
    assert not one('SELECT * FROM referral_rewards')


def test_yookassa_verified_and_duplicate_events(campaign,monkeypatch):
    client,owner,sid,code=campaign;user(client);bind(client,code)
    oid=order(client);execute("UPDATE orders SET method='yookassa',payment_id='pay-ref' WHERE id=?",(oid,))
    async def payment(pid):return {'id':pid,'metadata':{'order_id':oid},'amount':{'currency':'RUB','value':'199.00'},'status':'succeeded','paid':True}
    monkeypatch.setattr(providers,'get_payment',payment)
    original=one('SELECT * FROM orders WHERE id=?',(oid,))
    asyncio.run(service.reconcile_payment(original));asyncio.run(service.reconcile_payment(original))
    assert one('SELECT count(*) AS n FROM referral_rewards')['n']==1


def test_bad_payment_does_not_award(campaign,monkeypatch):
    client,owner,sid,code=campaign;user(client);bind(client,code)
    oid=order(client);execute("UPDATE orders SET method='yookassa',payment_id='pay-ref' WHERE id=?",(oid,))
    async def payment(pid):return {'id':pid,'metadata':{'order_id':oid},'amount':{'currency':'RUB','value':'1.00'},'status':'succeeded','paid':True}
    monkeypatch.setattr(providers,'get_payment',payment)
    with pytest.raises(RuntimeError):asyncio.run(service.reconcile_payment(one('SELECT * FROM orders WHERE id=?',(oid,))))
    assert not one('SELECT * FROM referral_rewards')


def test_stars_verifies_owner_and_amount(campaign):
    client,owner,sid,code=campaign;uid=user(client);bind(client,code)
    oid=order(client);execute('UPDATE users SET telegram_id=12345 WHERE id=?',(uid,));execute("UPDATE orders SET method='stars',currency='XTR',amount=150 WHERE id=?",(oid,))
    pay={'invoice_payload':oid,'total_amount':150,'currency':'XTR','telegram_payment_charge_id':'stars-ref'}
    with pytest.raises(RuntimeError):service.record_stars(9999,pay)
    with pytest.raises(RuntimeError):service.record_stars(12345,{**pay,'total_amount':149})
    assert not one('SELECT * FROM referral_rewards')
    service.record_stars(12345,pay);service.record_stars(12345,pay)
    assert one('SELECT count(*) AS n FROM referral_rewards')['n']==1


def test_atomic_payment_award_rolls_back_on_failure(campaign,monkeypatch):
    client,owner,sid,code=campaign;user(client);bind(client,code);oid=order(client)
    from app import referrals
    def fail(conn,oid):raise RuntimeError('disk failure')
    monkeypatch.setattr(referrals,'award_locked',fail)
    with pytest.raises(RuntimeError):confirm(client,oid)
    assert one('SELECT status FROM orders WHERE id=?',(oid,))['status']=='pending'


def test_redemption_queue_snapshot_retry_and_privileges(campaign,monkeypatch):
    client,owner,sid,code=campaign;paid_referral(campaign);login(client,owner)
    before=one('SELECT expires FROM subscriptions WHERE id=?',(sid,))['expires']
    request=str(uuid.uuid4());r=redeem(client,sid,5,request);assert r.status_code==200,r.text
    oid=r.json()['id'];assert r.json()['amount']==0 and r.json()['kind']=='referral' and r.json()['method']=='referral'
    assert redeem(client,sid,5,request).json()['id']==oid
    assert redeem(client,sid,4,request).status_code==409
    assert redeem(client,sid,3).status_code==409
    assert client.get('/api/me').json()['referrals']['balance_days']==2
    assert confirm(client,oid).status_code==409
    calls=[]
    async def fail(sub):calls.append(sub['expires']);raise RuntimeError('offline')
    monkeypatch.setattr(providers,'provision',fail);asyncio.run(service.fulfill(oid))
    target=one('SELECT target_expires FROM orders WHERE id=?',(oid,))['target_expires'];assert target==before+5*86400
    execute('UPDATE subscriptions SET device_limit=2 WHERE id=?',(sid,))
    async def success(sub):calls.append(sub['expires']);return 'vless://renewed'
    monkeypatch.setattr(providers,'provision',success);execute('UPDATE orders SET next_attempt=0 WHERE id=?',(oid,))
    asyncio.run(service.fulfill(oid));asyncio.run(service.fulfill(oid))
    assert calls==[target,target]
    assert one('SELECT expires,device_limit,access_source FROM subscriptions WHERE id=?',(sid,))=={'expires':target,'device_limit':2,'access_source':'purchase'}
    assert client.get('/api/me').json()['referrals']['balance_days']==2


def test_redemption_owner_subscription_eligibility_and_replay_after_expiry(campaign):
    client,owner,sid,code=campaign;paid_referral(campaign);login(client,owner)
    assert redeem(client,str(uuid.uuid4())).status_code==404
    for field,value in [('access_source','trial'),('state','expired'),('expires',0)]:
        execute('UPDATE subscriptions SET '+field+'=? WHERE id=?',(value,sid))
        assert redeem(client,sid).status_code==409
        execute("UPDATE subscriptions SET access_source='purchase',state='active',expires=? WHERE id=?",(int(time.time())+86400,sid))
    request=str(uuid.uuid4());r=redeem(client,sid,7,request);assert r.status_code==200
    execute('UPDATE subscriptions SET expires=0 WHERE id=?',(sid,))
    assert redeem(client,sid,7,request).json()['id']==r.json()['id']
    user(client);assert redeem(client,sid).status_code==404


def test_concurrent_redemption_no_negative_balance(campaign):
    client,owner,sid,code=campaign;paid_referral(campaign);login(client,owner)
    from app import referrals
    actor=one('SELECT * FROM users WHERE id=?',(owner,))
    def spend(_):
        try:return referrals.redeem(actor,referrals.Redeem(subscription_id=sid,request_id=str(uuid.uuid4()),days=7))
        except Exception as exc:return exc.status_code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(spend,range(2)))
    assert results.count(409)==1 and one('SELECT count(*) AS n FROM referral_redemptions')['n']==1
    assert client.get('/api/me').json()['referrals']['balance_days']==0


def test_concurrent_last_invitation(campaign):
    client,owner,sid,code=campaign;policy(client,max_invites=1)
    ids=[user(client),user(client)]
    from app import referrals
    def accept(uid):
        try:referrals.bind({'id':uid},referrals.Bind(code=code,policy_version=3,accept_terms=True));return 200
        except Exception as exc:return exc.status_code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(accept,ids))
    assert sorted(results)==[200,409]


def test_additive_init_preserves_policy_bindings_and_ledger(campaign):
    client,owner,sid,code=campaign;paid_referral(campaign);login(client,owner);redeem(client,sid,3)
    init_db();init_db()
    r=client.get('/api/me').json()['referrals'];assert (r['balance_days'],r['earned_days'],r['spent_days'])==(4,7,3)
    assert r['code']==code and r['policy']['version']==2


def test_bonus_and_purchase_share_serial_queue(campaign,monkeypatch):
    client,owner,sid,code=campaign;paid_referral(campaign);login(client,owner)
    bonus=redeem(client,sid).json()['id'];purchase=order(client)
    execute('UPDATE orders SET created=10 WHERE id=?',(bonus,));execute('UPDATE orders SET created=20 WHERE id=?',(purchase,));confirm(client,purchase)
    initial=one('SELECT expires FROM subscriptions WHERE id=?',(sid,))['expires']
    async def offline(sub):raise RuntimeError('offline')
    monkeypatch.setattr(providers,'provision',offline);asyncio.run(service.tick())
    assert one('SELECT target_expires FROM orders WHERE id=?',(purchase,))['target_expires'] is None
    assert one('SELECT target_expires FROM orders WHERE id=?',(bonus,))['target_expires']==initial+7*86400
    async def online(sub):return 'vless://ok'
    monkeypatch.setattr(providers,'provision',online);execute("UPDATE orders SET next_attempt=0 WHERE status='paid'")
    asyncio.run(service.tick())
    assert one('SELECT expires,device_limit FROM subscriptions WHERE id=?',(sid,))=={'expires':initial+37*86400,'device_limit':4}
    assert one("SELECT count(*) AS n FROM orders WHERE user_id=? AND status='paid'",(owner,))['n']==0


def test_bonus_extends_existing_devices(campaign,monkeypatch):
    client,owner,sid,code=campaign;paid_referral(campaign);login(client,owner)
    device=str(uuid.uuid4());execute("INSERT INTO devices(id,subscription_id,request_id,name,state,expires,secret,credential_id,created) VALUES (?,?,?,'Phone','active',?,?,?,0)",(device,sid,str(uuid.uuid4()),int(time.time())+86400,c.encrypt('old'),c.encrypt(device)))
    oid=redeem(client,sid,2).json()['id'];calls=[]
    async def provision(sub):calls.append(sub);return 'vless://new'
    monkeypatch.setattr(providers,'provision',provision);asyncio.run(service.fulfill(oid))
    assert len(calls)==2 and calls[0]['expires']==calls[1]['expires']
    assert one('SELECT expires FROM devices WHERE id=?',(device,))['expires']==calls[0]['expires']
    assert c.decrypt(one('SELECT secret FROM devices WHERE id=?',(device,))['secret'])=='vless://new'


def test_pending_before_program_no_backfill(client):
    uid=user(client);oid=order(client);confirm(client,oid)
    assert policy(client,enabled=True).status_code==200
    assert not one('SELECT * FROM referral_rewards')
    assert not client.get('/api/me').json()['referrals']['can_bind']


def test_disable_does_not_block_spending_earned_days(campaign):
    client,owner,sid,code=campaign;paid_referral(campaign);policy(client,enabled=False);login(client,owner)
    assert redeem(client,sid).status_code==200


def test_referrals_shop_prefix(campaign,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    client,owner,sid,code=campaign;cookie=client.cookies.get('session')
    monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    prefixed=TestClient(create_app('/shop'),cookies={'session':cookie})
    data=prefixed.get('/shop/api/me').json()['referrals']
    assert data['url']=='http://testserver/shop/?ref='+code
    assert prefixed.post('/shop/api/referrals/code').status_code==200
    assert prefixed.get('/shop/').status_code==200
