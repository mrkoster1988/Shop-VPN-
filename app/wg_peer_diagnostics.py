"""Developer-only, read-only diagnosis of a user's primary WireGuard peer.

Reading the panel never creates a key, a peer or a subscription. The result
contains no private/public keys, panel URLs, API tokens or WireGuard configs.
An existing subscription remains pinned to its original server.
"""
import asyncio
import base64
import configparser
import ipaddress
import re
import time
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException

from . import config as c, servers, service, wgdashboard
from .db import db
from .panel import PanelError


def _subscription(conn, user_id):
    user = conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone()
    if not user:
        raise HTTPException(404, 'Пользователь не найден')
    sub = conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND protocol='wg'",
                       (user_id,)).fetchone()
    return dict(sub) if sub else None


def _grant(conn, user_id):
    row = conn.execute(
        'SELECT state,attempts,next_attempt,target_expires,error FROM flow_auto_grants WHERE user_id=?',
        (user_id,)).fetchone()
    if row is None:
        return None
    return {'state': row['state'], 'attempts': row['attempts'], 'error': row['error'] if row['state'] == 'pending' else None,
            'next_attempt': row['next_attempt'] if row['state'] == 'pending' else None,
            'target_expires': row['target_expires']}


def _new_client():
    return httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False)


async def _download_existing_config(profile, private_key):
    adapter = wgdashboard.WGDashboard(profile)
    async with _new_client() as client:
        return await adapter.download_config(client, private_key)


async def _remote_peer(profile, public_key):
    adapter = wgdashboard.WGDashboard(profile)
    async with _new_client() as client:
        await adapter.check(client)
        match = await adapter.peer(client, public_key)
    if match is None:
        return 'peer_missing'
    return 'peer_restricted' if match[0] == 'configurationRestrictedPeers' else 'peer_found'


def _key_value(value):
    decoded = base64.b64decode(value, validate=True)
    # Android Key.fromBase64 rejects noncanonical padding bits as well as length.
    if len(decoded) != 32 or base64.b64encode(decoded).decode('ascii') != value:
        raise ValueError('Invalid profile key')
    return decoded


def _integer_option(value, maximum):
    # Python int accepts separators such as 14_20; the native parser does not.
    if not re.fullmatch(r'[+-]?[0-9]+', value):
        raise ValueError('Invalid numeric profile option')
    number = int(value)
    if not 0 <= number <= maximum:
        raise ValueError('Invalid numeric profile option')
    return number


def _config_fields(raw, private_key):
    """Canonical comparison values stay in memory; never include them in a report."""
    if (not isinstance(raw, str) or len(raw) > 65536 or '\x00' in raw
            or not raw.startswith('[Interface]')):
        raise ValueError('Invalid stored profile')
    validated = wgdashboard.WGDashboard.validate_config(raw, private_key)
    # The download adapter repairs a small set of malformed endpoint formats.
    # The saved profile is delivered verbatim: a repair that has not been saved
    # must not make a broken local endpoint appear to match the current export.
    if validated != raw:
        raise ValueError('Profile requires normalization')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(raw)
    interface, remote = parser['Interface'], parser['Peer']
    # An absent PSK is valid; a present but empty value is not a native key.
    psk = _key_value(remote['PresharedKey']) if 'PresharedKey' in remote else None
    # Flow uses WireGuard Android Config.parse: MTU is a nonnegative Java int;
    # listen port and keepalive are unsigned 16-bit values. Zero means unset.
    mtu = _integer_option(interface.get('MTU', '0'), 2147483647)
    listen_port = _integer_option(interface.get('ListenPort', '0'), 65535)
    keepalive = _integer_option(remote.get('PersistentKeepalive', '0'), 65535)
    endpoint = urlsplit('//' + remote['Endpoint'])
    try:
        host = str(ipaddress.ip_address(endpoint.hostname))
    except ValueError:
        host = endpoint.hostname.lower()
    return {
        'address': frozenset(str(ipaddress.ip_interface(value.strip()))
                             for value in interface['Address'].split(',')),
        'dns': tuple(value.strip().lower() for value in interface.get('DNS', '').split(',')
                     if value.strip()),
        'mtu': mtu or None,
        'listen_port': listen_port,
        'server_public_key': _key_value(remote['PublicKey']),
        # An all-zero WireGuard preshared key disables PSK, like an absent value.
        'preshared_key': None if psk == bytes(32) else psk,
        'allowed_ips': frozenset(str(ipaddress.ip_network(value.strip(), strict=False))
                                 for value in remote['AllowedIPs'].split(',')),
        'endpoint': (host, endpoint.port),
        'persistent_keepalive': keepalive,
    }


async def _compare_profile(sub, profile, private_key):
    """Presence alone cannot establish that Flow's stored profile is still current."""
    result = {'profile_status': 'local_invalid', 'profile_differences': []}
    if not sub['secret']:
        return {**result, 'profile_status': 'local_missing'}
    try:
        local = _config_fields(c.decrypt(sub['secret']), private_key)
    except PanelError as exc:
        if exc.args == ('WGDashboardConfigPrivateKey',):
            result['profile_status'] = 'local_key_mismatch'
        return result
    except Exception:
        return result
    try:
        # Bound the entire export check, including the adapter's read-only
        # interface/peer rechecks, rather than multiplying per-request timeouts.
        async with asyncio.timeout(12):
            current = await _download_existing_config(profile, private_key)
        remote = _config_fields(current, private_key)
    except Exception:
        # Failed reads or invalid exports never imply a matching profile. Raw
        # exceptions can contain panel credentials, endpoint addresses or keys.
        return {**result, 'profile_status': 'unavailable'}
    differences = [field for field in local if local[field] != remote[field]]
    return {'profile_status': 'differs_from_panel' if differences else 'matches_panel',
            'profile_differences': differences}


async def inspect(user_id):
    """Read-only presence and profile comparison on the pinned WGDashboard node."""
    checked = int(time.time())
    with db() as conn:
        sub = _subscription(conn, user_id)
        grant = _grant(conn, user_id)
        if sub is None:
            return {'status': 'no_subscription', 'checked_at': checked,
                    'subscription_state': None, 'local_ready': False,
                    'server_name': None, 'server_state': None, 'grant': grant,
                    'can_retry': False, 'can_restore': False,
                    'profile_status': 'not_checked', 'profile_differences': []}
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
            'profile_status': 'not_checked', 'profile_differences': [],
            'grant': grant, 'can_restore': False, 'can_retry': bool(
                auto_enabled and grant and grant['state'] == 'pending'
                and sub['state'] == 'pending' and not sub['secret']
                and grant['target_expires'] > checked)}

    if not sub['server_id']:
        return {**base, 'status': 'server_unbound', 'can_retry': False}
    if not server or server['protocol'] != 'wg':
        return {**base, 'status': 'server_missing', 'can_retry': False}
    try:
        profile = servers.subscription_profile(server, sub)
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
        private_key = c.decrypt(key['private_key'])
        public_key = wgdashboard.WGDashboard.identity(private_key)
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
        status in ('peer_missing','peer_found') and local_ready and
        server['state'] in ('active', 'draining') and server['health'] != 'error')
    if status == 'peer_found':
        base.update(await _compare_profile(sub, profile, private_key))
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
                profile = servers.subscription_profile(dict(row), sub)
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
                private_key = c.decrypt(key['private_key'])
                public_key = wgdashboard.WGDashboard.identity(private_key)
            except Exception:
                raise HTTPException(409, 'Сохранённый ключ недоступен') from None

        try:
            current = await _remote_peer(profile, public_key)
        except Exception:
            raise HTTPException(502, 'Не удалось проверить peer на WGDashboard') from None
        if current == 'peer_restricted':
            raise HTTPException(409, 'Пир ограничен на панели: ручная проверка обязательна')
        if current not in ('peer_found','peer_missing'):
            raise HTTPException(502, 'Статус пира не подтверждён')
        try:
            config = (await _download_existing_config(profile, private_key)
                      if current == 'peer_found' else
                      await providers.provision({**sub, 'id': identity,
                                                  'expires': sub['expires']}))
        except Exception:
            raise HTTPException(502, 'WGDashboard не подтвердил повторную выдачу. '
                                'Ключ и подписка сохранены для повтора.') from None
        if (not isinstance(config, str) or len(config) > 65536
                or not config.startswith('[Interface]') or '[Peer]' not in config):
            raise HTTPException(502, 'WGDashboard вернул неподдерживаемую конфигурацию')
        if current == 'peer_found':
            try:
                if c.decrypt(sub['secret']) == config:
                    return {'ok': True, 'status': 'already_present'}
            except Exception:
                pass
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
    return {'ok': True, 'status': 'restored' if current == 'peer_missing' else 'refreshed'}


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
