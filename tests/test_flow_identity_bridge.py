"""Cross-product proof accepts only the owner's managed WireGuard access."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from uuid import UUID

from app import config as c, flow_identity_bridge as bridge
from app.db import db, one, execute
from test_shop import client, register
from test_flow_wireguard import _VALID


KEY = '1a' * 32
ONE = '12345678-1234-4234-8234-123456789abc'
TWO = '12345678-1234-4234-8234-123456789abd'
PATH='/api/flow/bridge/exchange'


def _ticket(sub=ONE, tg=98455677, stamp=None, aud='pushok-shop', iat_delta=0, secret=KEY, jti=None):
    stamp=int(time.time()) if stamp is None else stamp
    value = {
        'v':1, 'iss':'mtproxy', 'aud':aud, 'sub':sub, 'tg':tg,
        'iat':stamp+iat_delta, 'exp':stamp+iat_delta+60,
        'jti':jti or base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b'=').decode(),
    }
    raw=json.dumps(value,ensure_ascii=True,separators=(',', ':'),sort_keys=True).encode('ascii')
    mac=hmac.new(bytes.fromhex(secret),b'pushok-flow-shop-v1\0'+raw,hashlib.sha256).hexdigest()
    return 'psb1.'+base64.urlsafe_b64encode(raw).rstrip(b'=').decode()+'.'+mac


def _enabled(monkeypatch):
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',KEY)
    monkeypatch.setattr(c,'PUBLIC_URL','https://shop.example')
    monkeypatch.setattr(c,'SECURE_COOKIE',True)


def _call(client, ticket=None, client_id=None):
    client_id=client_id or 'pfc_'+'a'*43
    return client.post(PATH,json={'ticket':ticket or _ticket(),'client_id':client_id})


def test_missing_secret_no_account_no_peer_and_malformed_input(client, monkeypatch):
    monkeypatch.delenv('FLOW_SHOP_BRIDGE_SECRET',raising=False)
    _enabled(monkeypatch)
    monkeypatch.delenv('FLOW_SHOP_BRIDGE_SECRET',raising=False)
    result=_call(client)
    assert result.status_code==503
    assert one('SELECT count(*) AS n FROM users')['n']==0
    assert client.post(PATH,json={'ticket':'bogus','client_id':'pfc_'+'a'*43}).status_code==422
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',KEY)
    for token in [_ticket(aud='other-service'),
                  _ticket(secret='2b'*32),_ticket(iat_delta=-300),
                  _ticket(iat_delta=120),
                  _ticket()[:-1]+'0']:
        assert _call(client,token).status_code==401
    assert one('SELECT count(*) AS n FROM users')['n']==0


def test_existing_shop_telegram_user_no_free_grant_default_and_replay(client, monkeypatch):
    _enabled(monkeypatch)
    # Shop's verified Telegram login is the authoritative binding, never username.
    from app import service
    user=service.telegram_account(98455677)
    assert user['id']==1
    request=_ticket()
    first=_call(client,request)
    assert first.status_code==200,first.text
    assert first.json()=={'status':'disabled','subscription_url':None}
    assert first.headers['cache-control']=='private, no-store'
    assert _call(client,request).status_code==409
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n']==1
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n']==0
    assert one('SELECT count(*) AS n FROM flow_bridge_identities')['n']==1


def test_issued_peer_is_returned_only_to_bound_phone(client,monkeypatch):
    _enabled(monkeypatch)
    # Simulate a previously purchased/provisioned WireGuard entitlement.
    from app import service
    service.telegram_account(98455677)
    expires=int(time.time())+86400
    with db() as conn:
        conn.execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret,access_source) "
                     "VALUES ('owned',1,'wg',?,'active',?,'purchase')",
                     (expires,c.encrypt(_VALID)))
    phone='pfc_'+'x'*43
    first=_call(client,client_id=phone)
    assert first.status_code==200,first.text
    res=first.json()
    assert res['status']=='active'
    assert res['subscription_url'].startswith('https://shop.example/flow/wireguard/')
    assert len(res)==2
    assert 'PrivateKey' not in first.text and 'token' not in first.text
    assert _call(client,client_id=phone).json()==res
    assert _call(client,client_id='pfc_'+'y'*43).status_code==409
    assert _call(client,_ticket(TWO,98455677),client_id=phone).status_code==409
    assert _call(client,_ticket(ONE,98455678),client_id=phone).status_code==409
    # The personal bearer endpoint itself returns only its device's feed.
    token=res['subscription_url'].rsplit('/',1)[-1]
    feed=client.get('/flow/wireguard/'+token)
    assert feed.status_code==200 and feed.json()['schema']=='pushok-flow-subscription'
    assert feed.json()['profiles'][0]['config']==_VALID


def test_pending_and_server_expiry_then_link_respects_manual_disable(client,monkeypatch):
    _enabled(monkeypatch)
    from app import service
    service.telegram_account(98455677)
    with db() as conn:
        conn.execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,access_source) "
                     "VALUES ('owned',1,'wg',0,'pending','trial')")
    assert _call(client).json()=={'status':'pending','subscription_url':None}
    with db() as conn:
        conn.execute("UPDATE subscriptions SET state='active',expires=?,secret=?,primary_key_state='active' WHERE id='owned'",
                     (int(time.time())+500,c.encrypt(_VALID)))
    assert _call(client).json()['status']=='active'
    assert bridge.exchange(_ticket(), 'pfc_'+'a'*43)['status']=='active'
    disabled=client.put('/api/flow/wireguard/subscriptions/owned/link',
        json={'action':'disable','version':1})
    # Without Shop session this route is 401; emulate an owner-managed disable via DB.
    assert disabled.status_code==401
    with db() as conn:
        conn.execute('UPDATE flow_wireguard_links SET enabled=0,version=version+1')
    assert _call(client).json()=={'status':'link_disabled','subscription_url':None}
    with db() as conn:
        conn.execute("UPDATE subscriptions SET state='expired',expires=1 WHERE id='owned'")
    assert _call(client).json()=={'status':'expired','subscription_url':None}


def test_bridge_never_merge_by_email_or_username(client,monkeypatch):
    _enabled(monkeypatch)
    register(client)
    assert one('SELECT telegram_id FROM users WHERE id=1')['telegram_id'] is None
    response=_call(client)
    assert response.status_code==200
    with db() as conn:
        people=conn.execute('SELECT id,email,telegram_id FROM users ORDER BY id').fetchall()
    assert len(people)==2
    assert people[0]['email']=='user@example.com' and people[0]['telegram_id'] is None
    assert people[1]['email'] is None and people[1]['telegram_id']==98455677
    assert _call(client).status_code==200


def test_failed_token_cannot_pollute_global_binds_and_no_login_cookie_needed(client,monkeypatch):
    _enabled(monkeypatch)
    expired=_ticket(iat_delta=-1000)
    assert _call(client,expired).status_code==401
    assert one('SELECT count(*) AS n FROM flow_bridge_nonces')['n']==0
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n']==0
    wrong=_ticket()
    assert _call(client,wrong,client_id='pfc_'+'z'*42).status_code==422
    ok=_call(client,wrong)
    assert ok.status_code==200
    assert one('SELECT count(*) AS n FROM flow_bridge_nonces')['n']==1
