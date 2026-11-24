"""MTProxy one-time ticket -> Shop trial -> WireGuard managed feed, 30 days."""
import asyncio
import time

from app import flow_auto_provision as auto, providers
from app.db import one
from test_flow_identity_bridge import _enabled, _call
from test_flow_auto_provision import _node, _policy, CONFIG
from test_shop import client


def test_mtproxy_existing_account_gets_one_free_30_day_wireguard(client, monkeypatch):
    _node()
    assert _policy(client, monkeypatch, days=30, max_users=2).status_code == 200
    _enabled(monkeypatch)
    start = int(time.time())

    # A previously registered MTProxy Telegram account may not yet exist
    # in Shop. A server-verified ticket maps it without a new Flow password.
    phone = 'pfc_' + 'd' * 43
    pending = _call(client, client_id=phone)
    assert pending.status_code == 200, pending.text
    assert pending.json() == {'status': 'pending', 'subscription_url': None}
    claim = one('SELECT target_expires,state FROM flow_auto_grants')
    assert claim['state'] == 'pending'
    assert start + 30 * 86400 - 2 <= claim['target_expires'] <= int(time.time()) + 30 * 86400 + 2

    calls = []
    async def provision(subscription):
        calls.append(subscription['id'])
        return CONFIG
    monkeypatch.setattr(providers, 'provision', provision)
    assert asyncio.run(auto.process_pending()) == 1
    assert len(calls) == 1

    active = _call(client, client_id=phone)
    assert active.status_code == 200, active.text
    assert active.json()['status'] == 'active'
    url = active.json()['subscription_url']
    assert url.startswith('https://shop.example/flow/wireguard/')
    token = url.rsplit('/', 1)[-1]
    feed = client.get('/flow/wireguard/' + token)
    assert feed.status_code == 200, feed.text
    data = feed.json()
    assert data['schema'] == 'pushok-flow-subscription'
    assert data['state'] == 'active'
    assert data['expires_at'] == claim['target_expires']
    assert len(data['profiles']) == 1
    assert data['profiles'][0]['config'] == CONFIG

    # Reauthorization cannot reset the 30-day clock or create another peer.
    assert _call(client, client_id=phone).json() == active.json()
    assert asyncio.run(auto.process_pending()) == 0
    assert len(calls) == 1
    assert one('SELECT count(*) AS n FROM flow_auto_grants')['n'] == 1
    assert one('SELECT target_expires FROM flow_auto_grants')['target_expires'] == claim['target_expires']

    # A different phone is never allowed to fetch this device's private key.
    other = _call(client, client_id='pfc_' + 'e' * 43)
    assert other.status_code == 409
