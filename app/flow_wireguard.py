"""Authenticated WireGuard discovery and Flow v1 feed for already entitled Shop users.

This module does not create peers, payments or trial grants. Access requires either an owned Shop session or an enabled personal link.
The WGDashboard API key is never returned. Each feed read and revision update
is serialized with subscription and token changes.
"""
import hashlib
import json
import re
import time

from fastapi import HTTPException

from . import config as c
from .db import db


def discover(user_id: int) -> dict:
    now = int(time.time())
    with db() as conn:
        rows = conn.execute("""
            SELECT id, state, expires, primary_key_state, secret, access_source
              FROM subscriptions
             WHERE user_id=? AND protocol='wg'
             ORDER BY id
        """, (user_id,)).fetchall()
    items = []
    for row in rows:
        active = (row['state'] == 'active' and row['primary_key_state'] == 'active'
                  and row['expires'] > now and bool(row['secret']))
        items.append({
            "subscription_id": row['id'],
            "state": "active" if active else "pending" if row['state'] == 'pending'
                or row['primary_key_state'] == 'rotating' else
                "revoked" if row['state'] == 'revoked' else "expired",
            "expires_at": max(1, int(row['expires'])),
            "ready": active,
            "access_source": row['access_source'],
        })
    return {"subscriptions": items}


def _feed_locked(conn, user_id: int, subscription_id: str) -> dict:
    """Читать состояние и публиковать ревизию в одной транзакции записи."""
    if not conn.in_transaction:
        raise RuntimeError('Flow feed requires transaction')
    now = int(time.time())
    row = conn.execute("""
        SELECT id, protocol, state, expires, primary_key_state, secret
          FROM subscriptions WHERE user_id=? AND id=?
    """, (user_id, subscription_id)).fetchone()
    if row is None or row['protocol'] != 'wg':
        raise HTTPException(404, 'Подписка не найдена')
    if row['state'] == 'pending' or row['primary_key_state'] == 'rotating':
        raise HTTPException(409, 'Подключение ещё готовится')
    if row['state'] == 'active' and row['expires'] > now and row['primary_key_state'] == 'active':
        if not row['secret']:
            raise HTTPException(409, 'Подключение ещё готовится')
        try:
            config = c.decrypt(row['secret'])
        except Exception:
            raise HTTPException(503, 'Не удалось получить настройки') from None
        if not isinstance(config, str) or not config.startswith('[Interface]') or len(config) > 65536:
            raise HTTPException(503, 'Настройки не готовы')
        state = 'active'
        profiles = [{
            'id': 'primary',
            'name': 'Pushok WireGuard',
            'protocol': 'wireguard',
            'config': config,
        }]
        from . import wg_fleet
        profiles = wg_fleet.feed_profiles(conn, subscription_id, profiles, int(row['expires']))
        if wg_fleet.revocation_requested(conn, subscription_id):
            state, profiles = 'revoked', []
    else:
        state = 'revoked' if row['state'] == 'revoked' else 'expired'
        profiles = []
    expiry = max(1, int(row['expires']))
    # Revisions are per subscription, not per access token or HTTP request.
    # A changed config/expiry/state increments the revision exactly once.
    canonical = json.dumps([state, expiry, profiles], ensure_ascii=False,
                           separators=(',', ':')).encode('utf-8')
    digest = hashlib.sha256(canonical).hexdigest()
    current = conn.execute(
        'SELECT fingerprint,revision FROM flow_wireguard_versions WHERE subscription_id=?',
        (subscription_id,),
    ).fetchone()
    if current is None:
        revision = 1
        conn.execute(
            'INSERT INTO flow_wireguard_versions(subscription_id,fingerprint,revision) VALUES (?,?,?)',
            (subscription_id, digest, revision),
        )
    elif current['fingerprint'] == digest:
        revision = current['revision']
    else:
        revision = current['revision'] + 1
        if revision > 9007199254740991:
            raise HTTPException(503, 'Версия подписки недоступна')
        conn.execute(
            'UPDATE flow_wireguard_versions SET fingerprint=?,revision=? WHERE subscription_id=?',
            (digest, revision, subscription_id),
        )
    return {
        'schema': 'pushok-flow-subscription',
        'version': 1,
        'revision': revision,
        'state': state,
        'expires_at': expiry,
        'profiles': profiles,
    }


def feed_for_owned_subscription(user_id: int, subscription_id: str) -> dict:
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _feed_locked(conn, user_id, subscription_id)

def _require_owned_wg(conn, user_id: int, subscription_id: str, *, active: bool):
    row = conn.execute(
        "SELECT id,protocol,state,expires,primary_key_state,secret "
        "FROM subscriptions WHERE id=? AND user_id=?",
        (subscription_id, user_id),
    ).fetchone()
    if row is None or row['protocol'] != 'wg':
        raise HTTPException(404, 'Подписка не найдена')
    if active and (row['state'] != 'active' or row['expires'] <= time.time()
                   or row['primary_key_state'] != 'active' or not row['secret']):
        raise HTTPException(409, 'Нужна активная подписка WireGuard')
    return row


def _link_url(token: str) -> str:
    # The consumer must never see a WGDashboard admin credential.
    return c.PUBLIC_URL.rstrip('/') + '/flow/wireguard/' + token


def link_for_user(user_id: int, subscription_id: str) -> dict:
    with db() as conn:
        _require_owned_wg(conn, user_id, subscription_id, active=False)
        row = conn.execute(
            'SELECT * FROM flow_wireguard_links WHERE subscription_id=?',
            (subscription_id,),
        ).fetchone()
    if row is None:
        return {'enabled': False, 'version': 0, 'url': None}
    if not row['enabled']:
        return {'enabled': False, 'version': row['version'], 'url': None}
    try:
        raw = c.decrypt(row['token_secret'])
        from .security import digest
        if not isinstance(raw, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', raw) is None or digest(raw) != row['token_hash']:
            raise ValueError('Invalid link')
    except Exception:
        raise HTTPException(503, 'Ссылка временно недоступна') from None
    return {'enabled': True, 'version': row['version'], 'url': _link_url(raw)}


def change_link(user_id: int, subscription_id: str, action: str,
                expected_version: int) -> dict:
    # A single database transaction serializes issuance, CAS rotation and revoke.
    # Repeating 'enable' does not issue another token, even if a response was lost.
    import secrets
    from .security import digest
    if action not in ('enable', 'rotate', 'disable'):
        raise HTTPException(422, 'Неизвестное действие')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_owned_wg(conn, user_id, subscription_id, active=action != 'disable')
        row = conn.execute('SELECT * FROM flow_wireguard_links WHERE subscription_id=?',
                           (subscription_id,)).fetchone()
        version = row['version'] if row else 0
        if action == 'enable' and row and row['enabled']:
            # Same CAS replay policy as additional-device links.
            if expected_version not in (version, version - 1):
                raise HTTPException(409, 'Ссылка изменилась. Обновите кабинет.')
        else:
            if version != expected_version:
                raise HTTPException(409, 'Ссылка изменилась. Обновите кабинет.')
            if action == 'disable':
                if row and row['enabled']:
                    conn.execute('UPDATE flow_wireguard_links SET enabled=0,version=version+1 '
                                 'WHERE subscription_id=?', (subscription_id,))
            elif action in ('enable', 'rotate'):
                if action == 'rotate' and (row is None or not row['enabled']):
                    raise HTTPException(409, 'Сначала создайте ссылку')
                token = secrets.token_urlsafe(32)
                secret = c.encrypt(token)
                hashed = digest(token)
                if row is None:
                    conn.execute(
                        'INSERT INTO flow_wireguard_links'
                        '(subscription_id,token_hash,token_secret,enabled,version) '
                        'VALUES (?,?,?,1,1)', (subscription_id, hashed, secret))
                else:
                    conn.execute('UPDATE flow_wireguard_links SET '
                                 'token_hash=?,token_secret=?,enabled=1,version=version+1 '
                                 'WHERE subscription_id=?',
                                 (hashed, secret, subscription_id))
            else:
                raise HTTPException(422, 'Неизвестное действие')
    return link_for_user(user_id, subscription_id)


def feed_for_link(token: str) -> dict:
    from .security import digest
    import re
    if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
        raise HTTPException(404, 'Ссылка не найдена')
    with db() as conn:
        # Проверка токена, чтение актуального профиля и публикация revision
        # сериализованы с ротацией ссылки, продлением и отзывом в SQLite.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT l.subscription_id,s.user_id FROM flow_wireguard_links l '
            'JOIN subscriptions s ON s.id=l.subscription_id '
            'WHERE l.token_hash=? AND l.enabled=1', (digest(token),),
        ).fetchone()
        if row is None:
            raise HTTPException(404, 'Ссылка не найдена')
        return _feed_locked(conn, row['user_id'], row['subscription_id'])
