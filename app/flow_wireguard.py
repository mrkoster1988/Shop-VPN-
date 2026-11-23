"""Authenticated WireGuard discovery and Flow v1 feed for already entitled Shop users.

This module does not create peers, payments or trial grants. The user's
Shop session is required on every request; the WGDashboard API key is never
returned. A future short-lived Flow token exchange may reuse this contract.
"""
import hashlib
import json
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


def feed_for_owned_subscription(user_id: int, subscription_id: str) -> dict:
    now = int(time.time())
    with db() as conn:
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
        else:
            state = 'revoked' if row['state'] == 'revoked' else 'expired'
            profiles = []
        expiry = max(1, int(row['expires']))
        # Revisions are per subscription, not per access token or HTTP request.
        # A changed config/expiry/state increments the revision exactly once.
        canonical = json.dumps([state, expiry, profiles], ensure_ascii=False,
                               separators=(',', ':')).encode('utf-8')
        digest = hashlib.sha256(canonical).hexdigest()
        conn.execute('BEGIN IMMEDIATE')
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
