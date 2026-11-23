"""Scoped Shop pairing: explicit approval, isolation and session expiry."""
import base64
import hashlib
import secrets
import time

import pytest

from app import config as c
from app.db import execute, one
from test_shop import client, register
from test_flow_wireguard import _seed


BASE = '/api/flow/shop'


@pytest.fixture
def pairing(client, monkeypatch):
    register(client)
    monkeypatch.setenv('FLOW_SHOP_PAIRING_ENABLED', 'true')
    monkeypatch.setattr(c, 'SECURE_COOKIE', True)
    monkeypatch.setattr(c, 'PUBLIC_URL', 'https://shop.example')
    return client


def begin(client):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    result = client.post(BASE+'/auth/start', json={'challenge':challenge})
    assert result.status_code == 200, result.text
    info = result.json()
    assert info['approval_url'].endswith('#flow-pair='+info['code'])
    assert verifier not in result.text
    return {'code':info['code'],'verifier':verifier}


def approve(client, data):
    return client.post(BASE+'/auth/decision', json={'code':data['code'],'decision':'approve'})


def exchange(client, data):
    return client.post(BASE+'/auth/exchange', json=data)


def bearer(info):
    return {'Authorization':'Bearer '+info['access_token']}


def test_disabled_by_default_and_https_required(client, monkeypatch):
    register(client)
    monkeypatch.delenv('FLOW_SHOP_PAIRING_ENABLED', raising=False)
    assert client.get(BASE+'/capabilities').json()['enabled'] is False
    assert client.post(BASE+'/auth/start',json={'challenge':'A'*43}).status_code == 503
    monkeypatch.setenv('FLOW_SHOP_PAIRING_ENABLED','true')
    assert client.get(BASE+'/capabilities').json()['enabled'] is False
    assert one('SELECT count(*) AS n FROM flow_shop_pairings')['n'] == 0


def test_explicit_consent_and_scope(pairing):
    client = pairing
    _seed(1,'owned-wg',int(time.time())+3600)
    data = begin(client)
    assert exchange(client,data).json()['state']=='pending'
    assert client.get(BASE+'/connections').status_code==401
    wrong = {**data,'verifier':secrets.token_urlsafe(32)}
    assert exchange(client,wrong).status_code==404
    assert approve(client,data).json()=={'state':'approved'}
    result = exchange(client,data).json()
    assert result['state']=='authorized' and result['scope']=='wireguard:read'
    assert exchange(client,data).json()==result
    assert result['expires_at']<=int(time.time())+43200
    row=one('SELECT * FROM flow_shop_pairings')
    assert result['access_token'] not in str(row) and data['verifier'] not in str(row)
    assert data['code'] not in str(row)
    # Remove the browser cookie from this test client without revoking its server session.
    client.cookies.clear()
    auth=bearer(result)
    assert client.get(BASE+'/connections',headers=auth).json()['subscriptions'][0]['subscription_id']=='owned-wg'
    response=client.get(BASE+'/subscriptions/owned-wg',headers=auth)
    assert response.status_code==200
    assert response.json()['schema']=='pushok-flow-subscription'
    assert response.headers['cache-control']=='no-store'
    assert client.get('/api/me',headers=auth).status_code==401
    assert client.get('/api/admin/overview',headers=auth).status_code==401


def test_wrong_owner_and_revoke(pairing):
    client=pairing
    data=begin(client)
    assert approve(client,data).status_code==200
    result=exchange(client,data).json()
    auth=bearer(result)
    assert client.get(BASE+'/subscriptions/missing',headers=auth).status_code==404
    assert client.post(BASE+'/auth/revoke').json()=={'ok':True}
    assert client.get(BASE+'/connections',headers=auth).status_code==401
    assert exchange(client,data).status_code==404


def test_second_authorization_revokes_first_and_logout(pairing):
    client=pairing
    first=begin(client)
    assert approve(client,first).status_code==200
    old=bearer(exchange(client,first).json())
    second=begin(client)
    assert approve(client,second).status_code==200
    current=bearer(exchange(client,second).json())
    assert client.get(BASE+'/connections',headers=old).status_code==401
    assert client.get(BASE+'/connections',headers=current).status_code==200
    assert exchange(client,first).status_code==404
    assert client.post('/api/auth/logout').status_code==200
    assert client.get(BASE+'/connections',headers=current).status_code==401


def test_deny_expiry_and_validation_never_echo_secrets(pairing):
    client=pairing
    data=begin(client)
    denied=client.post(BASE+'/auth/decision',json={'code':data['code'],'decision':'deny'})
    assert denied.json()=={'state':'denied'}
    assert exchange(client,data).json()['state']=='denied'
    assert approve(client,data).status_code==409
    second=begin(client)
    execute("UPDATE flow_shop_pairings SET request_expires=1 WHERE state='pending'")
    assert exchange(client,second).status_code==410
    private='secret-invalid-input'
    response=client.post(BASE+'/auth/exchange',json={'code':private,'verifier':private})
    assert response.status_code==422 and private not in response.text


def test_no_peer_or_free_grant_is_created_when_policy_disabled(pairing):
    client=pairing
    data=begin(client)
    assert approve(client,data).status_code==200
    auth=bearer(exchange(client,data).json())
    assert client.get(BASE+'/connections',headers=auth).json()=={'subscriptions':[]}
    assert one('SELECT count(*) AS n FROM subscriptions')['n']==0
    assert one('SELECT count(*) AS n FROM orders')['n']==0
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n']==0
