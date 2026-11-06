import asyncio,time
import pytest
from test_shop import client,admin_headers,register
from test_devices import issued
from app import service,providers,config as c
from app.db import one,execute,init_db

@pytest.fixture
def trial_env(client,monkeypatch):
    monkeypatch.setattr(c,'BOT_TOKEN','123:trial-token');monkeypatch.setattr(c,'BOT_USERNAME','trial_test_bot');execute('UPDATE store_settings SET terms_text=? WHERE id=1',('Trial terms snapshot',))
    calls=[];messages=[]
    async def provision(sub):calls.append(dict(sub));return 'vless://'+sub['id']+'@vpn.example:443'
    async def no_payment(*args,**kwargs):raise AssertionError('Trial must not request payment')
    async def send(tg,text,**kwargs):messages.append(text)
    async def disable(sub):calls.append({**sub,'disabled':True})
    monkeypatch.setattr(providers,'disable',disable)
    monkeypatch.setattr(providers,'provision',provision);monkeypatch.setattr(providers,'stars_invoice',no_payment);monkeypatch.setattr(providers,'payment',no_payment);monkeypatch.setattr(service,'send',send)
    return client,calls,messages


def telegram(client,tg=12345):
    user=service.telegram_account(tg);response=client.get(service.ticket(user['id']),follow_redirects=False)
    assert response.status_code in (302,303,307)
    return user

def policy(client,**changes):
    before=client.get('/api/admin/trial',headers=admin_headers()).json()
    body={k:before[k] for k in ('enabled','days','max_claims','version')};body.update(changes)
    return client.put('/api/admin/trial',json=body,headers=admin_headers())

def claim(client,protocol='xui',**changes):
    offer=client.get('/api/catalog').json()['trial']
    return client.post('/api/trial',json={'protocol':protocol,'policy_version':offer['version'],'accept_terms':True,**changes})


def test_trial_disabled_and_admin_auth_by_default(trial_env):
    client,_,_=trial_env;telegram(client)
    offer=client.get('/api/catalog').json()['trial'];assert not offer['enabled'] and not offer['eligible']
    assert claim(client).status_code==409 and one('SELECT count(*) AS n FROM orders')['n']==0
    assert client.get('/api/admin/trial').status_code==401
    assert client.put('/api/admin/trial',json={'enabled':True,'days':1,'max_claims':10,'version':1}).status_code==401

@pytest.mark.parametrize('protocol',['xui','wg'])
def test_single_trial_is_real_queued_issuance_without_invoice(trial_env,protocol):
    client,calls,_=trial_env;telegram(client);assert policy(client,enabled=True).status_code==200
    result=claim(client,protocol);assert result.status_code==200,result.text
    order=result.json();assert order['kind']=='trial' and order['method']=='trial' and order['amount']==0 and order['status']=='paid' and not order['payment_url']
    sub=one('SELECT * FROM subscriptions');assert sub['state']=='pending' and sub['server_id']=='test-'+protocol and sub['device_limit']==0
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==404
    asyncio.run(service.fulfill(order['id']))
    assert len(calls)==1 and calls[0]['id']==sub['id']
    current=one('SELECT * FROM subscriptions');assert current['access_source']=='trial' and current['expires']>=time.time()+86380
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200
    assert claim(client,protocol).json()['id']==order['id']
    assert claim(client,'wg' if protocol=='xui' else 'xui').status_code==409
    asyncio.run(service.fulfill(order['id']));assert len(calls)==1
    assert policy(client).json()['used']==1


def test_trial_has_no_extra_profiles_rotation_or_subscription_url(trial_env):
    import uuid
    client,_,_=trial_env;telegram(client);policy(client,enabled=True);order=claim(client).json();asyncio.run(service.fulfill(order['id']));sid=one('SELECT id FROM subscriptions')['id']
    assert client.post('/api/subscriptions/'+sid+'/devices',json={'name':'Phone','request_id':str(uuid.uuid4())}).status_code==409
    assert client.post('/api/subscriptions/'+sid+'/rotate',json={'request_id':str(uuid.uuid4())}).status_code==409
    assert client.get('/api/subscriptions/'+sid+'/link').status_code==409
    assert client.put('/api/subscriptions/'+sid+'/link',json={'action':'enable','version':0}).status_code==409
    assert client.post('/api/admin/orders/'+order['id']+'/confirm',headers=admin_headers()).status_code==409


def test_policy_edit_does_not_reset_claim_or_change_saved_conditions(trial_env,monkeypatch):
    client,calls,_=trial_env;telegram(client);policy(client,enabled=True,days=1);first=claim(client).json()
    execute('UPDATE store_settings SET terms_text=? WHERE id=1',('Different new terms',));assert policy(client,enabled=False,days=3).status_code==200
    assert claim(client,policy_version=999,accept_terms=False).json()['id']==first['id']
    asyncio.run(service.fulfill(first['id']));assert one('SELECT days FROM orders')['days']==1
    assert one('SELECT terms_snapshot FROM trial_claims')['terms_snapshot']=='Trial terms snapshot'
    init_db();assert claim(client).json()['id']==first['id'] and len(calls)==1


def test_budget_counts_pending_claims_and_cannot_be_reset_by_policy_edit(trial_env):
    client,_,_=trial_env;telegram(client,1);policy(client,enabled=True,max_claims=1);assert claim(client).status_code==200
    client.post('/api/auth/logout');telegram(client,2);assert claim(client).status_code==409
    assert one('SELECT count(*) AS n FROM orders')['n']==1
    policy(client,enabled=False);policy(client,enabled=True);assert claim(client).status_code==409
    assert policy(client).json()['used']==1
    policy(client,max_claims=2);assert claim(client).status_code==200


def test_server_reservation_prevents_oversubscription_without_spending_failed_claim(trial_env):
    client,_,_=trial_env;policy(client,enabled=True,max_claims=10)
    execute("UPDATE servers SET capacity=1 WHERE protocol='xui'");telegram(client,1);assert claim(client).status_code==200
    client.post('/api/auth/logout');telegram(client,2);assert claim(client).status_code==409
    assert policy(client).json()['used']==1
    assert claim(client,'wg').status_code==200


def test_email_anonymous_and_unsigned_trial_input_are_rejected(trial_env):
    client,_,_=trial_env;policy(client,enabled=True)
    assert claim(client).status_code==401
    register(client);assert claim(client).status_code==403
    assert client.get('/api/catalog').json()['trial']['reason']=='telegram_required'

@pytest.mark.parametrize('status',['pending','canceled','paid','active'])
def test_previous_order_disqualifies_account(trial_env,status):
    client,_,_=trial_env;user=telegram(client);policy(client,enabled=True)
    execute('INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status) VALUES (?,?,?,?,?,?,?,?)',('old',user['id'],'xui','month',30,199,1,status))
    assert claim(client).status_code==409 and client.get('/api/catalog').json()['trial']['reason']=='existing_customer'


def test_previous_expired_subscription_disqualifies_account(trial_env):
    client,_,_=trial_env;user=telegram(client);policy(client,enabled=True)
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state) VALUES ('old',?,'wg',1,'expired')",(user['id'],))
    assert claim(client).status_code==409


def test_stale_policy_and_missing_terms_do_not_consume_trial(trial_env):
    client,_,_=trial_env;telegram(client);policy(client,enabled=True)
    assert claim(client,policy_version=1).status_code==409
    assert claim(client,accept_terms=False).status_code==422
    assert one('SELECT count(*) AS n FROM trial_claims')['n']==0
    assert claim(client).status_code==200


def test_retry_after_provider_error_uses_same_order_key_server_and_target(trial_env,monkeypatch):
    client,calls,_=trial_env;telegram(client);policy(client,enabled=True);order=claim(client).json()
    async def fail(sub):calls.append(dict(sub));raise TimeoutError('private-response')
    monkeypatch.setattr(providers,'provision',fail);asyncio.run(service.fulfill(order['id']));target=one('SELECT target_expires FROM orders')['target_expires']
    init_db();assert claim(client).json()['id']==order['id'];execute('UPDATE orders SET next_attempt=0')
    async def good(sub):calls.append(dict(sub));return 'vless://'+sub['id']+'@vpn.example'
    monkeypatch.setattr(providers,'provision',good);asyncio.run(service.fulfill(order['id']))
    assert calls[0]['id']==calls[1]['id'] and calls[0]['server_id']==calls[1]['server_id'] and calls[1]['expires']==target
    assert one('SELECT count(*) AS n FROM trial_claims')['n']==1


def test_paid_purchase_after_trial_enables_paid_features_and_keeps_days(trial_env):
    from test_shop import order,confirm
    client,_,_=trial_env;user=telegram(client);policy(client,enabled=True);trial=claim(client).json();asyncio.run(service.fulfill(trial['id']));sub=one('SELECT * FROM subscriptions')
    # A verified paid order is seeded; no external invoice is requested by this test.
    execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status,method,currency) VALUES ('purchase',?,'xui','month',30,199,?,'paid','stars','XTR')",(user['id'],int(time.time())))
    asyncio.run(service.fulfill('purchase'));current=one('SELECT * FROM subscriptions')
    assert current['access_source']=='purchase' and current['device_limit']==4 and current['expires']==sub['expires']+30*86400
    assert current['id']==sub['id'] and claim(client).json()['id']==trial['id']


def test_trial_precedes_paid_purchase_even_when_timestamps_and_uuid_sort_collide(trial_env):
    client,calls,messages=trial_env;user=telegram(client);policy(client,enabled=True);trial=claim(client).json();created=one('SELECT created FROM orders')['created']
    execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status,method,currency) VALUES ('000-first',?,'xui','month',30,199,?,'paid','stars','XTR')",(user['id'],created))
    asyncio.run(service.fulfill('000-first'));assert one("SELECT status FROM orders WHERE id='000-first'")['status']=='paid'
    asyncio.run(service.tick());sub=one('SELECT * FROM subscriptions')
    assert sub['access_source']=='purchase' and sub['device_limit']==4 and sub['expires']>=time.time()+31*86400-20
    assert len(calls)==2 and any(text.startswith('Пробный') for text in messages)


def test_enabling_requires_real_bot_settings_and_policy_versions(trial_env,monkeypatch):
    client,_,_=trial_env;monkeypatch.setattr(c,'BOT_TOKEN','')
    assert policy(client,enabled=True).status_code==409
    monkeypatch.setattr(c,'BOT_TOKEN','123:test');assert policy(client,enabled=True).status_code==200
    assert client.put('/api/admin/trial',json={'enabled':False,'days':1,'max_claims':100,'version':1},headers=admin_headers()).status_code==409
    assert one('SELECT count(*) AS n FROM trial_policy_changes')['n']==1

@pytest.mark.parametrize('changes',[{'days':0},{'days':4},{'max_claims':0},{'max_claims':True},{'enabled':'yes'}])
def test_trial_policy_validation_is_strict(trial_env,changes):
    client,_,_=trial_env;assert policy(client,**changes).status_code==422


def test_expiry_does_not_allow_another_trial(trial_env):
    client,calls,_=trial_env;telegram(client);policy(client,enabled=True);original=claim(client).json();asyncio.run(service.fulfill(original['id']))
    execute('UPDATE subscriptions SET expires=1');asyncio.run(service.tick())
    assert one('SELECT state FROM subscriptions')['state']=='expired'
    assert calls[-1]['disabled'] and client.get('/api/subscriptions/'+calls[-1]['id']+'/config').status_code==404
    assert claim(client).json()['id']==original['id'] and one('SELECT count(*) AS n FROM trial_claims')['n']==1


def test_missing_capacity_leaves_no_partial_claim(trial_env):
    client,_,_=trial_env;telegram(client);policy(client,enabled=True);execute("UPDATE servers SET state='disabled'")
    assert claim(client).status_code==409
    for table in ('trial_claims','subscriptions','orders'):assert one('SELECT count(*) AS n FROM '+table)['n']==0
    execute("UPDATE servers SET state='active'");assert claim(client).status_code==200


def test_double_submit_returns_one_claim(trial_env):
    from concurrent.futures import ThreadPoolExecutor
    client,_,_=trial_env;telegram(client);policy(client,enabled=True)
    version=client.get('/api/catalog').json()['trial']['version']
    def request(_):return client.post('/api/trial',json={'protocol':'xui','policy_version':version,'accept_terms':True})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(request,range(2)))
    assert all(r.status_code==200 for r in results) and results[0].json()['id']==results[1].json()['id']
    assert one('SELECT count(*) AS n FROM trial_claims')['n']==1


def test_trial_cannot_be_processed_as_stars_payment(trial_env):
    client,_,_=trial_env;user=telegram(client);policy(client,enabled=True);result=claim(client).json()
    query={'id':'fake','from':{'id':user['telegram_id']},'invoice_payload':result['id'],'currency':'XTR','total_amount':0}
    assert service.reserve_checkout(query) is False
    with pytest.raises(RuntimeError):service.record_stars(user['telegram_id'],{**query,'telegram_payment_charge_id':'charge'})


def test_old_database_marks_existing_orders_and_access_as_purchase(issued):
    from app.db import db
    client,issue,_=issued;sub=issue();original=client.get('/api/subscriptions/'+sub['id']+'/config').json()
    with db() as conn:
        # В старой схеме не было журнала этапов и его триггеров.
        for trigger in ('order_paid_insert','order_paid_update','order_issued_insert','order_issued_update'):
            conn.execute('DROP TRIGGER IF EXISTS '+trigger)
        conn.execute('DROP TABLE IF EXISTS order_milestones')
        conn.execute('DROP TABLE IF EXISTS order_history_state')
        conn.execute('ALTER TABLE orders DROP COLUMN kind');conn.execute('ALTER TABLE subscriptions DROP COLUMN access_source')
    init_db();init_db()
    assert one('SELECT kind FROM orders')['kind']=='purchase' and one('SELECT access_source FROM subscriptions')['access_source']=='purchase'
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()==original
    assert client.get('/api/admin/trial',headers=admin_headers()).json()['enabled'] is False


def test_trial_under_shop_prefix(trial_env,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    client,_,_=trial_env;telegram(client);policy(client,enabled=True)
    monkeypatch.setattr(c,'BASE_PATH','/shop');monkeypatch.setattr(c,'COOKIE_PATH','/shop/');monkeypatch.setattr(c,'PUBLIC_URL','http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        shop.cookies.update(client.cookies)
        version=shop.get('/shop/api/catalog').json()['trial']['version']
        result=shop.post('/shop/api/trial',json={'protocol':'xui','policy_version':version,'accept_terms':True},headers={'Origin':'http://testserver'})
        assert result.status_code==200
        assert shop.get('/shop/api/admin/orders?method=trial',headers=admin_headers()).json()['total']==1
        assert shop.get('/shop/api/admin/trial',headers=admin_headers()).json()['used']==1
