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
