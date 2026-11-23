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
    # There is one WireGuard subscription per user. Exercise each state
    # sequentially instead of creating illegal duplicates.
    with db() as conn:
        conn.execute("UPDATE subscriptions SET expires=? WHERE id='owned-primary'", (now - 10,))
    assert client.get('/api/flow/wireguard/subscriptions/owned-primary').json()['state'] == 'expired'
    with db() as conn:
        conn.execute("UPDATE subscriptions SET state='pending',expires=? WHERE id='owned-primary'",
                     (now + 3600,))
    assert client.get('/api/flow/wireguard/subscriptions/owned-primary').status_code == 409
    with db() as conn:
        conn.execute("UPDATE subscriptions SET state='expired',expires=? WHERE id='owned-primary'",
                     (now - 10,))
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

def test_personal_link_replay_rotate_revoke_and_ownership(client):
    register(client)
    _seed(1, 'owned-primary', int(time.time())+86400)
    path='/api/flow/wireguard/subscriptions/owned-primary/link'
    assert client.get(path).json() == {'enabled': False, 'version': 0, 'url': None}
    first=client.put(path,json={'action':'enable','version':0})
    assert first.status_code == 200,first.text
    initial=first.json()
    assert initial['enabled'] is True and initial['version']==1
    assert initial['url'].startswith('http://testserver/flow/wireguard/')
    token=initial['url'].rsplit('/',1)[-1]
    assert len(token)==43
    assert client.put(path,json={'action':'enable','version':0}).json()==initial
    assert client.get('/flow/wireguard/'+token).json()['state']=='active'
    with db() as conn:
        row=conn.execute('SELECT * FROM flow_wireguard_links').fetchone()
        assert token not in row['token_hash'] and token not in row['token_secret']
    rotated=client.put(path,json={'action':'rotate','version':1})
    assert rotated.status_code==200
    updated=rotated.json()
    assert updated['version']==2 and updated['url']!=initial['url']
    assert client.get('/flow/wireguard/'+token).status_code==404
    assert client.put(path,json={'action':'rotate','version':1}).status_code==409
    new_token=updated['url'].rsplit('/',1)[-1]
    assert client.get('/flow/wireguard/'+new_token).status_code==200
    assert client.put(path,json={'action':'disable','version':2}).json()=={
        'enabled':False,'version':3,'url':None}
    assert client.get('/flow/wireguard/'+new_token).status_code==404
    assert client.put(path,json={'action':'rotate','version':3}).status_code==409
    assert client.get('/flow/wireguard/short').status_code==404
    client.post('/api/auth/logout')
    assert client.put(path,json={'action':'enable','version':3}).status_code==401
    register(client,'other@example.com')
    assert client.get(path).status_code==404
    assert client.put(path,json={'action':'enable','version':3}).status_code==404


def test_link_never_issues_for_unpaid_or_pending_user(client):
    register(client)
    assert client.put('/api/flow/wireguard/subscriptions/unknown/link',
        json={'action':'enable','version':0}).status_code==404
    _seed(1,'expired',int(time.time())-3)
    path='/api/flow/wireguard/subscriptions/expired/link'
    assert client.put(path,json={'action':'enable','version':0}).status_code==409
    with db() as conn:
        assert conn.execute('SELECT count(*) FROM flow_wireguard_links').fetchone()[0]==0


def test_flow_feed_reads_token_and_profile_in_one_write_transaction(client, monkeypatch):
    """A revision must not publish a stale snapshot after a competing renewal."""
    from contextlib import contextmanager
    from app import flow_wireguard as flow

    register(client)
    _seed(1, 'owned-primary', int(time.time()) + 86400)
    link = client.put('/api/flow/wireguard/subscriptions/owned-primary/link',
                      json={'action': 'enable', 'version': 0}).json()
    token = link['url'].rsplit('/', 1)[-1]
    real_db = flow.db
    transactions = []

    @contextmanager
    def traced_db():
        with real_db() as conn:
            statements = []
            transactions.append(statements)
            conn.set_trace_callback(lambda sql: statements.append(sql.upper()))
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(flow, 'db', traced_db)
    response = client.get('/flow/wireguard/' + token)
    assert response.status_code == 200
    assert len(transactions) == 1
    statements = transactions[0]
    assert statements[0] == 'BEGIN IMMEDIATE'
    token_read = next(i for i, s in enumerate(statements) if 'FROM FLOW_WIREGUARD_LINKS' in s)
    profile_read = next(i for i, s in enumerate(statements) if 'FROM SUBSCRIPTIONS WHERE USER_ID=' in s)
    revision_read = next(i for i, s in enumerate(statements) if 'FROM FLOW_WIREGUARD_VERSIONS' in s)
    assert 0 < token_read < profile_read < revision_read

    transactions.clear()
    response = client.get('/api/flow/wireguard/subscriptions/owned-primary')
    assert response.status_code == 200 and response.json()['revision'] == 1
    assert len(transactions) == 1 and transactions[0][0] == 'BEGIN IMMEDIATE'


def test_flow_public_feed_private_errors_and_maintenance(client, monkeypatch):
    response = client.get('/flow/wireguard/' + 'x' * 43)
    assert response.status_code == 404
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'noindex' in response.headers['x-robots-tag']
    assert 'x' * 43 not in response.text
    monkeypatch.setenv('MAINTENANCE_MODE', 'true')
    response = client.get('/flow/wireguard/' + 'x' * 43)
    assert response.status_code == 503
    assert response.headers['content-type'].startswith('application/json')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['retry-after'] == '60'


def test_flow_feed_uses_same_bounded_rate_limit_as_vless(client):
    from app.main import hits

    hits.clear()
    # Public requests, even invalid tokens, share the subscription quota.
    for _ in range(30):
        assert client.get('/sub/' + 'x' * 43).status_code == 404
        assert client.get('/flow/wireguard/' + 'x' * 43).status_code == 404
    response = client.get('/flow/wireguard/' + 'x' * 43)
    assert response.status_code == 429
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/me').status_code == 401


def test_invalid_ciphertext_does_not_publish_a_new_revision(client):
    register(client)
    _seed(1, 'owned-primary', int(time.time()) + 86400)
    path = '/api/flow/wireguard/subscriptions/owned-primary'
    assert client.get(path).json()['revision'] == 1
    with db() as conn:
        conn.execute("UPDATE subscriptions SET secret='invalid-ciphertext' WHERE id='owned-primary'")
    response = client.get(path)
    assert response.status_code == 503
    assert 'invalid-ciphertext' not in response.text
    with db() as conn:
        assert conn.execute('SELECT revision FROM flow_wireguard_versions').fetchone()[0] == 1
