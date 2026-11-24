"""Developer-only, read-only diagnosis of a user's primary WireGuard peer.

Reading the panel never creates a key, a peer or a subscription. The result
contains no private/public keys, panel URLs, API tokens or WireGuard configs.
An existing subscription remains pinned to its original server.
"""
import time
import httpx
from fastapi import HTTPException

from . import config as c, servers, service, wgdashboard
from .db import db


def _subscription(conn, user_id):
    user = conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        raise HTTPException(404, 'Пользователь не найден')
    sub = conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND protocol='wg'",
                       (user_id,)).fetchone()
    return dict(sub) if sub else None


def _grant(conn, user_id):
    row = conn.execute(
        'SELECT state,attempts,next_attempt,target_expires FROM flow_auto_grants WHERE user_id=?',
        (user_id,)).fetchone()
    if row is None:
        return None
    return {'state': row['state'], 'attempts': row['attempts'],
            'next_attempt': row['next_attempt'] if row['state'] == 'pending' else None,
            'target_expires': row['target_expires']}


def _new_client():
    return httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False)


async def _remote_peer(profile, public_key):
    adapter = wgdashboard.WGDashboard(profile)
    async with _new_client() as client:
        await adapter.check(client)
        match = await adapter.peer(client, public_key)
    if match is None:
        return 'peer_missing'
    return 'peer_restricted' if match[0] == 'configurationRestrictedPeers' else 'peer_found'


async def inspect(user_id):
    """A safe snapshot plus a single remote read of the pinned WGDashboard node."""
    checked = int(time.time())
    with db() as conn:
        sub = _subscription(conn, user_id)
        grant = _grant(conn, user_id)
        if sub is None:
            return {'status': 'no_subscription', 'checked_at': checked,
                    'subscription_state': None, 'local_ready': False,
                    'server_name': None, 'server_state': None, 'grant': grant,
                    'can_retry': False}
        row = conn.execute('SELECT * FROM servers WHERE id=?',
                           (sub['server_id'],)).fetchone() if sub['server_id'] else None
        server = dict(row) if row else None
        policy = conn.execute('SELECT enabled FROM flow_auto_policy WHERE id=1').fetchone()
        auto_enabled = bool(policy and policy['enabled'])
        local_ready = (sub['state'] == 'active' and sub['primary_key_state'] == 'active'
                       and sub['expires'] > checked and bool(sub['secret']))
        # Never derive a new key to satisfy a status request.
        try:
            identity = service.primary_identity(sub)
        except Exception:
            identity = None
        key = conn.execute(
            'SELECT private_key FROM wgdashboard_keys WHERE server_id=? AND credential_id=?',
            (sub['server_id'], identity)).fetchone() if server and identity else None

    base = {'checked_at': checked, 'subscription_state': sub['state'],
            'local_ready': bool(local_ready), 'server_name': server['name'] if server else None,
            'server_state': server['state'] if server else None,
            'grant': grant, 'can_retry': bool(
                auto_enabled and grant and grant['state'] == 'pending'
                and sub['state'] == 'pending' and not sub['secret']
                and grant['target_expires'] > checked)}

    if not sub['server_id']:
        return {**base, 'status': 'server_unbound', 'can_retry': False}
    if not server or server['protocol'] != 'wg':
        return {**base, 'status': 'server_missing', 'can_retry': False}
    try:
        profile = servers.decode(server)
    except Exception:
        return {**base, 'status': 'server_configuration_error', 'can_retry': False}
    if profile.get('api') != 'wgdashboard':
        return {**base, 'status': 'other_backend', 'can_retry': False}
    if base['can_retry'] and (server['state'] not in ('active', 'draining')
                              or server['health'] == 'error'
                              or profile.get('schedule_expiry') is not True
                              or not profile.get('panel_timezone')
                              or (grant and grant.get('next_attempt') is None)):
        base['can_retry'] = False
    if not key:
        return {**base, 'status': 'key_not_created'}
    try:
        public_key = wgdashboard.WGDashboard.identity(c.decrypt(key['private_key']))
    except Exception:
        return {**base, 'status': 'key_unreadable', 'can_retry': False}
    try:
        status = await _remote_peer(profile, public_key)
    except Exception:
        return {**base, 'status': 'panel_unavailable'}
    return {**base, 'status': status}


async def retry_pending(user_id):
    """Wake only the *existing* queued grant. Never assign/migrate a node."""
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub = _subscription(conn, user_id)
            if not sub:
                raise HTTPException(409, 'У пользователя нет заявки WireGuard')
            grant = conn.execute(
                'SELECT state,subscription_id,target_expires FROM flow_auto_grants WHERE user_id=?',
                (user_id,)).fetchone()
            policy = conn.execute('SELECT enabled FROM flow_auto_policy WHERE id=1').fetchone()
            if (not policy or not policy['enabled'] or not grant or grant['state'] != 'pending'
                    or grant['subscription_id'] != sub['id']
                    or grant['target_expires'] <= int(time.time())
                    or sub['state'] != 'pending' or sub['secret']):
                raise HTTPException(409, 'Нет подходящей ожидающей автовыдачи')
            try:
                row, profile = servers.for_subscription(sub)
            except Exception:
                raise HTTPException(409, 'Закреплённый сервер недоступен. '
                                    'Перенос нужно выполнять отдельно.') from None
            if (row['health'] == 'error' or profile.get('api') != 'wgdashboard'
                    or profile.get('schedule_expiry') is not True
                    or not profile.get('panel_timezone')):
                raise HTTPException(409, 'Сначала проверьте API WGDashboard и срок доступа')
            # Do not clear the previously stored identity or reset attempts.
            conn.execute("UPDATE flow_auto_grants SET next_attempt=0 "
                         "WHERE user_id=? AND state='pending'", (user_id,))
    return {'status': 'queued', 'ok': True}
