"""A developer can diagnose a stale Flow binding without exposing keys.

Releasing an *issued* WireGuard identity is always forbidden here; it would
otherwise hand the same private key to another phone.
"""
import hashlib
import time

from app import config as c, service
from app.db import one, execute
from test_flow_identity_bridge import _call, _enabled, _ticket
from test_shop import client


def owner():
    return {'Authorization': 'Bearer ' + c.ADMIN_TOKEN}


def url(uid=1):
    return f'/api/admin/users/{uid}/flow/device'


def stale(uid=1, *, created=1234567890):
    value = hashlib.sha256(('pfc_' + 'a' * 43).encode()).hexdigest()
    execute('INSERT INTO flow_bridge_devices(shop_user_id,client_hash,created) '
            'VALUES (?,?,?)', (uid, value, created))


def test_disabled_or_pending_grant_does_not_bind_phone_without_config(client, monkeypatch):
    _enabled(monkeypatch)
    service.telegram_account(98455677)
    first = _call(client, client_id='pfc_'+'a'*43)
    assert first.status_code == 200
    assert first.json() == {'status':'disabled', 'subscription_url':None}
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 0
    # The user can change their phone *before* actually receiving any peer.
    second = _call(client, client_id='pfc_'+'b'*43)
    assert second.status_code == 200
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 0
    execute("INSERT INTO subscriptions(id,user_id,protocol,state,expires,access_source) "
            "VALUES ('pending',1,'wg','pending',0,'trial')")
    third = _call(client, client_id='pfc_'+'b'*43)
    assert third.status_code == 200
    assert third.json()['status'] == 'pending'
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 0


def test_exact_409_reason_distinguishes_replay_and_identity_conflict(client, monkeypatch):
    _enabled(monkeypatch)
    service.telegram_account(98455677)
    request = _ticket()
    assert _call(client, ticket=request).status_code == 200
    used = _call(client, ticket=request)
    assert used.status_code == 409
    assert used.json()['detail'] == {'code':'FLOW_PROOF_REPLAY'}
    from test_flow_wireguard import _VALID
    execute("INSERT INTO subscriptions(id,user_id,protocol,state,expires,secret) "
            "VALUES ('active',1,'wg','active',?,?)",
            (int(time.time())+3600,c.encrypt(_VALID)))
    assert _call(client, client_id='pfc_'+'a'*43).json()['status'] == 'active'
    # A different client_id on the SAME verified account is never a conflict.
    account = _call(client, client_id='pfc_'+'z'*43)
    assert account.status_code == 200
    assert account.json()['status'] == 'active'
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 0
    identity = _call(client, ticket=_ticket(sub='12345678-1234-4234-8234-123456789abd'))
    assert identity.status_code == 409
    assert identity.json()['detail'] == {'code':'FLOW_IDENTITY_CONFLICT'}


def test_owner_only_status_and_release_unused_binding_preserve_identity(client, monkeypatch):
    _enabled(monkeypatch)
    service.telegram_account(98455677)
    _call(client)  # The signed identity mapping is persisted, but not the phone.
    stale()
    assert client.get(url()).status_code == 401
    assert client.post(url()+'/release',json={
        'binding_created':1234567890, 'confirm':'RELEASE_UNUSED_FLOW_BINDING'}).status_code == 401
    report = client.get(url(),headers=owner())
    assert report.status_code == 200
    assert report.json() == {
        'bound':True,'binding_created':1234567890,'has_wg_subscription':False,
        'can_release':True,'blocker':None,
    }
    assert 'client_hash' not in report.text and 'pfc_' not in report.text
    assert client.post(url()+'/release', headers=owner(), json={
        'binding_created':1234567889,
        'confirm':'RELEASE_UNUSED_FLOW_BINDING'}).status_code == 409
    assert client.post(url()+'/release', headers=owner(), json={
        'binding_created':1234567890,
        'confirm':'WRONG'}).status_code == 422
    result = client.post(url()+'/release',headers=owner(),json={
        'binding_created':1234567890,
        'confirm':'RELEASE_UNUSED_FLOW_BINDING'})
    assert result.status_code == 200, result.text
    assert result.json() == {'ok':True,'status':'released'}
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 0
    assert one('SELECT count(*) AS n FROM flow_bridge_identities')['n'] == 1
    assert one('SELECT count(*) AS n FROM flow_bridge_nonces')['n'] == 1
    assert client.post(url()+'/release',headers=owner(),json={
        'binding_created':1234567890,
        'confirm':'RELEASE_UNUSED_FLOW_BINDING'}).status_code == 409
    assert client.get(url(999),headers=owner()).status_code == 404


def test_active_wg_profile_blocks_release_of_bound_phone(client, monkeypatch):
    _enabled(monkeypatch)
    service.telegram_account(98455677)
    stale()
    execute("INSERT INTO subscriptions(id,user_id,protocol,state,expires,secret) "
            "VALUES ('active',1,'wg','active',?,?)",
            (int(time.time())+3600,c.encrypt('private')) )
    report = client.get(url(),headers=owner())
    assert report.json()['can_release'] is False
    assert report.json()['blocker'] == 'wg_profile_may_be_issued'
    refusal = client.post(url()+'/release',headers=owner(),json={
        'binding_created':1234567890,'confirm':'RELEASE_UNUSED_FLOW_BINDING'})
    assert refusal.status_code == 409
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 1


def test_pending_first_attempt_can_release_but_remote_attempt_blocks(client, monkeypatch):
    _enabled(monkeypatch)
    service.telegram_account(98455677)
    stale()
    execute("INSERT INTO subscriptions(id,user_id,protocol,state,expires) "
            "VALUES ('pending',1,'wg','pending',0)")
    execute("INSERT INTO flow_auto_grants(user_id,subscription_id,target_expires,policy_version,state,created,next_attempt,attempts) "
            "VALUES (1,'pending',?,1,'pending',?,0,0)",
            (int(time.time())+86400,int(time.time())))
    assert client.get(url(),headers=owner()).json()['can_release'] is True
    execute("UPDATE flow_auto_grants SET attempts=1 WHERE user_id=1")
    report=client.get(url(),headers=owner()).json()
    assert report['can_release'] is False
    assert report['blocker']=='wg_provisioning_unconfirmed'
    assert client.post(url()+'/release',headers=owner(),json={
        'binding_created':1234567890,'confirm':'RELEASE_UNUSED_FLOW_BINDING'}).status_code == 409
    assert one('SELECT count(*) AS n FROM flow_bridge_devices')['n'] == 1
