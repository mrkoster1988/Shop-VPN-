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
                    'can_retry': False, 'can_restore': False}
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
            'grant': grant, 'can_restore': False, 'can_retry': bool(
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
    except httpx.TimeoutException:
        return {**base, 'status': 'panel_timeout', 'can_retry': False}
    except httpx.ConnectError:
        return {**base, 'status': 'panel_connection_failed', 'can_retry': False}
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        status = ('panel_auth_failed' if code in (401, 403)
                  else 'panel_api_missing' if code in (404, 405)
                  else 'panel_unavailable')
        return {**base, 'status': status, 'can_retry': False}
    except Exception:
        # No raw server error, HTTP body, URL, API token or keys in admin JSON.
        return {**base, 'status': 'panel_unavailable', 'can_retry': False}
    base['can_restore'] = bool(
        status == 'peer_missing' and local_ready and
        server['state'] in ('active', 'draining') and server['health'] != 'error')
    return {**base, 'status': status}


async def restore_primary(user_id):
    """Explicit owner action: restore a missing peer on its ORIGINAL WGDashboard.

    Never migrate a subscription, change its key, expand the paid term or
    create a new entitlement. An incomplete remote write is retried by the
    owner with exactly the same stored key.
    """
    from . import providers
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub = _subscription(conn, user_id)
            now = int(time.time())
            if (not sub or sub['state'] != 'active'
                    or sub['primary_key_state'] != 'active'
                    or sub['expires'] <= now or not sub['secret']):
                raise HTTPException(409, 'Нужна действующая подписка с сохранённым профилем')
            unfinished = conn.execute(
                "SELECT 1 FROM orders WHERE user_id=? AND protocol='wg' "
                "AND status IN ('pending','checkout','paid') LIMIT 1",
                (user_id,)).fetchone()
            if unfinished:
                raise HTTPException(409, 'Есть незавершённый заказ WireGuard')
            row = conn.execute('SELECT * FROM servers WHERE id=?',
                               (sub['server_id'],)).fetchone()
            if (not row or row['protocol'] != 'wg'
                    or row['state'] not in ('active','draining')
                    or row['health'] == 'error'):
                raise HTTPException(409, 'Исходный узел недоступен. '
                                    'Автоматический перенос на другой сервер запрещён.')
            try:
                profile = servers.decode(dict(row))
                identity = service.primary_identity(sub)
            except Exception:
                raise HTTPException(409, 'Не удалось проверить привязку пира') from None
            if profile.get('api') != 'wgdashboard':
                raise HTTPException(409, 'Подписка закреплена не за WGDashboard')
            key = conn.execute(
                'SELECT private_key FROM wgdashboard_keys '
                'WHERE server_id=? AND credential_id=?',
                (sub['server_id'],identity)).fetchone()
            if key is None:
                raise HTTPException(409, 'Нет сохранённого ключа. Создание нового '
                                    'ключа при восстановлении запрещено.')
            try:
                public_key = wgdashboard.WGDashboard.identity(c.decrypt(key['private_key']))
            except Exception:
                raise HTTPException(409, 'Сохранённый ключ недоступен') from None

        try:
            current = await _remote_peer(profile, public_key)
        except Exception:
            raise HTTPException(502, 'Не удалось проверить peer на WGDashboard') from None
        if current == 'peer_restricted':
            raise HTTPException(409, 'Пир ограничен на панели: ручная проверка обязательна')
        if current == 'peer_found':
            return {'ok': True, 'status': 'already_present'}
        if current != 'peer_missing':
            raise HTTPException(502, 'Статус пира не подтверждён')
        try:
            config = await providers.provision({**sub, 'id': identity,
                                                'expires': sub['expires']})
        except Exception:
            raise HTTPException(502, 'WGDashboard не подтвердил повторную выдачу. '
                                'Ключ и подписка сохранены для повтора.') from None
        if (not isinstance(config, str) or len(config) > 65536
                or not config.startswith('[Interface]') or '[Peer]' not in config):
            raise HTTPException(502, 'WGDashboard вернул неподдерживаемую конфигурацию')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute(
                "UPDATE subscriptions SET secret=? WHERE id=? AND user_id=? "
                "AND protocol='wg' AND state='active' AND primary_key_state='active' "
                "AND server_id=? AND expires=? AND secret IS NOT NULL",
                (c.encrypt(config),sub['id'],user_id,sub['server_id'],sub['expires']),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, 'Состояние подписки изменилось. '
                                    'Подключение необходимо повторно проверить.')
    return {'ok': True, 'status': 'restored'}


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
