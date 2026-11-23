"""Shop's authenticated Flow feed never provisions unentitled accounts."""
import time

from app import config as c
from app.db import db

from test_shop import client, register


_VALID = (
    '[Interface]\\n'
    'PrivateKey = AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=\\n'
    'Address = 10.77.0.2/32\\n'
    '[Peer]\\n'
    'PublicKey = AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=\\n'
    'Endpoint = 203.0.113.10:51820\\n'
    'AllowedIPs = 0.0.0.0/0, ::/0\\n'
)


def _seed(uid, sid, expires, state='active', secret=_VALID):
    with db() as conn:
        conn.execute(
            "INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret,access_source) "
            "VALUES (?,?,'wg',?,?,?,'purchase')",
            (sid, uid, expires, state, c.encrypt(secret)),
        )


def test_unauthorized_and_empty_account_does_not_create_peer(client):
    assert client.get('/api/flow/wireguard/connections').status_code == 401
    assert client.get('/api/flow/wireguard/subscriptions/unknown').status_code == 401
    register(client)
    assert client.get('/api/flow/wireguard/connections').json() == {'subscriptions': []}
    assert client.get('/api/flow/wireguard/subscriptions/unknown').status_code == 404
    with db() as conn:
        assert conn.execute('SELECT count(*) FROM subscriptions').fetchone()[0] == 0


def test_existing_paid_user_gets_idempotent_encrypted_feed(client):
    register(client)
    now = int(time.time())
    _seed(1, 'owned-primary', now + 86400)
    discovery = client.get('/api/flow/wireguard/connections')
    assert discovery.status_code == 200
    assert discovery.json()['subscriptions'][0]['ready'] is True
    result = client.get('/api/flow/wireguard/subscriptions/owned-primary')
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    payload = result.json()
    assert payload == {
        'schema': 'pushok-flow-subscription', 'version': 1,
        'revision': 1, 'state': 'active', 'expires_at': now + 86400,
        'profiles': [{'id': 'primary', 'name': 'Pushok WireGuard',
                      'protocol': 'wireguard', 'config': _VALID}],
    }
    assert client.get('/api/flow/wireguard/subscriptions/owned-primary').json() == payload
    with db() as conn:
        conn.execute('UPDATE subscriptions SET expires=? WHERE id=?',
                     (now + 2*86400, 'owned-primary'))
    renewed = client.get('/api/flow/wireguard/subscriptions/owned-primary').json()
    assert renewed['revision'] == 2
    assert renewed['profiles'] == payload['profiles']
    assert client.get('/api/flow/wireguard/subscriptions/owned-primary').json()['revision'] == 2


def test_wrong_owner_expiry_revocation_and_pending(client):
    register(client)
    now = int(time.time())
    _seed(1, 'owned-primary', now + 3600)
    _seed(1, 'expired-primary', now - 10, 'active')
    _seed(1, 'pending-primary', now + 3600, 'pending')
    assert client.get('/api/flow/wireguard/subscriptions/expired-primary').json()['state'] == 'expired'
    assert client.get('/api/flow/wireguard/subscriptions/pending-primary').status_code == 409
    with db() as conn:
        conn.execute("UPDATE subscriptions SET state='expired' WHERE id='owned-primary'")
    expired = client.get('/api/flow/wireguard/subscriptions/owned-primary').json()
    assert expired['profiles'] == [] and expired['state'] == 'expired'
    assert expired['revision'] == 1
    with db() as conn:
        conn.execute("UPDATE subscriptions SET state='active',expires=? WHERE id='owned-primary'", (now + 7200,))
    assert client.get('/api/flow/wireguard/subscriptions/owned-primary').json()['revision'] == 2
    client.post('/api/auth/logout')
    register(client, 'other@example.com')
    assert client.get('/api/flow/wireguard/subscriptions/owned-primary').status_code == 404
    assert client.get('/api/flow/wireguard/connections').json() == {'subscriptions': []}
