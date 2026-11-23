"""Opt-in starter WireGuard access for authenticated Shop users.

Disabled by default. No MTProxy read-only token, no implicit payment,
no remote API call on an authentication request. One claim per Shop user.
"""
import logging
import time
import uuid
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import config as c, providers, servers, shop_settings
from .db import db

log = logging.getLogger('pushok.flow_bootstrap')


class PolicyUpdate(BaseModel):
    model_config = {'extra': 'forbid'}
    enabled: bool = Field(strict=True)
    days: int = Field(ge=1, le=30, strict=True)
    max_users: int = Field(ge=1, le=100000, strict=True)
    version: int = Field(ge=1, strict=True)


def policy():
    with db() as conn:
        row = dict(conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone())
        used = conn.execute('SELECT count(*) FROM flow_auto_grants').fetchone()[0]
    return {**row, 'enabled': bool(row['enabled']),
            'used': used, 'remaining': max(0, row['max_users'] - used)}


def _node(conn):
    """An eligible node must revoke exported configs on its own expiry timer."""
    rows = conn.execute("""
        SELECT s.*,
          (SELECT count(*) FROM subscriptions sub
            WHERE sub.server_id=s.id AND (sub.state='pending'
              OR (sub.state='active' AND sub.expires>?))) AS occupied
        FROM servers s WHERE s.protocol='wg' AND s.state='active'
          AND s.health!='error'
        ORDER BY occupied,s.created,s.id
    """, (int(time.time()),)).fetchall()
    for row in rows:
        if row['capacity'] > 0 and row['occupied'] >= row['capacity']:
            continue
        try:
            profile = servers.decode(dict(row))
        except Exception:
            continue
        if (profile.get('api') == 'wgdashboard'
                and profile.get('schedule_expiry') is True
                and bool(profile.get('panel_timezone'))):
            return dict(row)
    return None


async def update_policy(data: PolicyUpdate):
    from . import service
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone()
            if row['version'] != data.version:
                raise HTTPException(409, 'Политика изменилась. Обновите страницу.')
            if data.enabled:
                if not c.SECURE_COOKIE:
                    raise HTTPException(409, 'Для автовыдачи требуется HTTPS.')
                if not shop_settings.terms_configured():
                    raise HTTPException(409, 'Сначала настройте условия доступа.')
                if _node(conn) is None:
                    raise HTTPException(409, 'Нужен активный WGDashboard с заданием окончания доступа.')
            if (bool(row['enabled']), row['days'], row['max_users']) != (
                    data.enabled, data.days, data.max_users):
                conn.execute('UPDATE flow_auto_policy SET enabled=?,days=?,max_users=?,'
                             'version=version+1,updated=? WHERE id=1',
                             (int(data.enabled), data.days, data.max_users, int(time.time())))
    return policy()


MAX_BATCH = 50


class BatchRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    version: int = Field(ge=1, strict=True)
    user_ids: list[Annotated[int, Field(gt=0, le=9223372036854775807, strict=True)]] = Field(
        min_length=1, max_length=MAX_BATCH)


def _open_wg_order(conn, user_id):
    return conn.execute(
        """SELECT 1 FROM orders WHERE user_id=? AND protocol='wg'
           AND status IN ('pending','checkout','paid') LIMIT 1""",
        (user_id,)).fetchone() is not None


def _reserve_locked(conn, user_id, setting, now):
    """Единый путь постановки в очередь для входа и партии владельца."""
    if not conn.in_transaction:
        raise RuntimeError('Flow grant requires transaction')
    if conn.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone() is None:
        return 'missing'
    if conn.execute('SELECT 1 FROM flow_auto_grants WHERE user_id=?', (user_id,)).fetchone():
        return 'claimed'
    if conn.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND protocol='wg'",
                    (user_id,)).fetchone():
        return 'existing'
    if _open_wg_order(conn, user_id):
        return 'order_pending'
    if not setting or not setting['enabled']:
        return 'disabled'
    if conn.execute('SELECT count(*) FROM flow_auto_grants').fetchone()[0] >= setting['max_users']:
        return 'limit'
    node = _node(conn)
    if node is None:
        return 'unavailable'
    sid = str(uuid.uuid4())
    target = now + int(setting['days']) * 86400
    conn.execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,"
                 "server_id,device_limit,access_source) VALUES (?,?,'wg',0,'pending',?,0,'trial')",
                 (sid, user_id, node['id']))
    conn.execute("INSERT INTO flow_auto_grants(user_id,subscription_id,target_expires,"
                 "policy_version,state,created,next_attempt,attempts) "
                 "VALUES (?,?,?,?,'pending',?,?,0)",
                 (user_id, sid, target, setting['version'], now, now))
    return 'queued'


def enqueue(user_id: int) -> str:
    """Идемпотентная выдача при входе, без сетевых вызовов и списаний."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        setting = conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone()
        result = _reserve_locked(conn, user_id, setting, int(time.time()))
        if result == 'missing':
            raise HTTPException(401, 'Аккаунт не найден')
        if result == 'claimed':
            return conn.execute('SELECT state FROM flow_auto_grants WHERE user_id=?',
                                (user_id,)).fetchone()['state']
        return 'pending' if result == 'queued' else result


_ELIGIBLE = """FROM users u
    WHERE NOT EXISTS (SELECT 1 FROM flow_auto_grants g WHERE g.user_id=u.id)
      AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id=u.id AND s.protocol='wg')
      AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.id AND o.protocol='wg'
                      AND o.status IN ('pending','checkout','paid'))"""


def batch_preview() -> dict:
    """Снимок без изменения БД и без вызовов WGDashboard; только для владельца."""
    with db() as conn:
        conn.execute('BEGIN')
        setting = conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone()
        used = conn.execute('SELECT count(*) FROM flow_auto_grants').fetchone()[0]
        remaining = max(0, setting['max_users'] - used)
        eligible = conn.execute('SELECT count(*) ' + _ELIGIBLE).fetchone()[0]
        node_available = _node(conn) is not None if setting['enabled'] else False
        candidates = []
        if setting['enabled'] and node_available and remaining:
            candidates = [r['id'] for r in conn.execute(
                'SELECT u.id ' + _ELIGIBLE + ' ORDER BY u.id LIMIT ?',
                (min(MAX_BATCH, remaining),)).fetchall()]
        states = {r['state']: r['n'] for r in conn.execute(
            'SELECT state,count(*) AS n FROM flow_auto_grants GROUP BY state')}
    return {'enabled': bool(setting['enabled']), 'version': setting['version'],
            'days': setting['days'], 'max_users': setting['max_users'],
            'eligible': eligible, 'used': used, 'remaining': remaining,
            'node_available': node_available, 'candidate_ids': candidates,
            'pending': states.get('pending', 0), 'active': states.get('active', 0),
            'expired': states.get('expired', 0), 'superseded': states.get('superseded', 0)}


async def enqueue_batch(data: BatchRequest) -> dict:
    """Ставит в очередь только явно выбранные ID (не более 50 за один запрос).

    Повтор того же тела не выбирает новых пользователей. Проверки права,
    квоты и свободного узла выполняются заново внутри транзакции.
    """
    from . import service

    if len(set(data.user_ids)) != len(data.user_ids):
        raise HTTPException(422, 'В партии не должно быть повторяющихся аккаунтов')
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            setting = conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone()
            if setting['version'] != data.version:
                raise HTTPException(409, 'Политика изменилась. Обновите список.')
            if not setting['enabled']:
                raise HTTPException(409, 'Автовыдача отключена владельцем')
            if not c.SECURE_COOKIE:
                raise HTTPException(409, 'Для автовыдачи требуется HTTPS.')
            if not shop_settings.terms_configured():
                raise HTTPException(409, 'Сначала настройте условия доступа.')
            now = int(time.time())
            outcomes = {}
            for user_id in data.user_ids:
                result = _reserve_locked(conn, user_id, setting, now)
                outcomes[result] = outcomes.get(result, 0) + 1
    return {'requested': len(data.user_ids), 'queued': outcomes.pop('queued', 0),
            'skipped': outcomes}


def after_login(user_id: int):
    """Authentication succeeds even when auxiliary grant storage is unavailable."""
    try:
        return enqueue(user_id)
    except Exception as exc:
        log.warning('Auto WireGuard enqueue unavailable: %s', type(exc).__name__)
        return 'unavailable'


async def process_pending(max_jobs: int = 5) -> int:
    """Use the same credential ID on every retry; never log user configs."""
    from . import service
    completed = 0
    async with service.lock:
        now = int(time.time())
        with db() as conn:
            row = conn.execute('SELECT enabled FROM flow_auto_policy WHERE id=1').fetchone()
            if not row or not row['enabled']:
                return 0
            jobs = [dict(r) for r in conn.execute(
                "SELECT * FROM flow_auto_grants WHERE state='pending' AND next_attempt<=? "
                "ORDER BY created,user_id LIMIT ?", (now, min(20, max(1, max_jobs)))
            ).fetchall()]
        for job in jobs:
            sid = job['subscription_id']
            try:
                with db() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    claim = conn.execute(
                        'SELECT state FROM flow_auto_grants WHERE user_id=?',
                        (job['user_id'],)).fetchone()
                    row = conn.execute(
                        "SELECT * FROM subscriptions WHERE id=? AND user_id=? AND protocol='wg'",
                        (sid, job['user_id'])).fetchone()
                    if claim is None or claim['state'] != 'pending' or row is None:
                        continue
                    if row['state'] != 'pending' or row['secret'] is not None:
                        conn.execute("UPDATE flow_auto_grants SET state='superseded' WHERE user_id=?",
                                     (job['user_id'],))
                        continue
                    if job['target_expires'] <= int(time.time()):
                        conn.execute("UPDATE flow_auto_grants SET state='expired' WHERE user_id=?",
                                     (job['user_id'],))
                        conn.execute("UPDATE subscriptions SET state='expired',expires=? "
                                     "WHERE id=? AND state='pending'",
                                     (job['target_expires'], sid))
                        continue
                    sub = dict(row)
                    # Reject a panel switch to an unprotected backend mid-queue.
                    _, profile = servers.for_subscription(sub)
                    if (profile.get('api') != 'wgdashboard'
                            or profile.get('schedule_expiry') is not True
                            or not profile.get('panel_timezone')):
                        raise RuntimeError('WireGuardExpiryNotConfigured')
                config = await providers.provision({
                    **sub, 'id': service.primary_identity(sub),
                    'expires': job['target_expires']})
                if (not isinstance(config, str) or len(config) > 65536
                        or not config.startswith('[Interface]') or '[Peer]' not in config):
                    raise RuntimeError('WireGuardConfigInvalid')
                encrypted = c.encrypt(config)
                with db() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    current = conn.execute('SELECT enabled FROM flow_auto_policy WHERE id=1').fetchone()
                    claim = conn.execute(
                        'SELECT state FROM flow_auto_grants WHERE user_id=?',
                        (job['user_id'],)).fetchone()
                    existing = conn.execute(
                        'SELECT state,secret FROM subscriptions WHERE id=?',
                        (sid,)).fetchone()
                    if (not current or not current['enabled'] or claim is None or claim['state'] != 'pending'
                            or existing is None or existing['state'] != 'pending'
                            or existing['secret'] is not None):
                        raise RuntimeError('WireGuardGrantChanged')
                    conn.execute("UPDATE subscriptions SET state='active',expires=?,secret=?,"
                                 "credential_id=?,primary_key_state='active',access_source='trial'"
                                 " WHERE id=?",
                                 (job['target_expires'], encrypted, c.encrypt(sid), sid))
                    conn.execute("UPDATE flow_auto_grants SET state='active' WHERE user_id=?",
                                 (job['user_id'],))
                completed += 1
            except Exception as exc:
                attempts = int(job['attempts']) + 1
                delay = min(1800, 10 * (2 ** min(attempts, 8)))
                with db() as conn:
                    conn.execute("UPDATE flow_auto_grants SET attempts=?,next_attempt=? "
                                 "WHERE user_id=? AND state='pending'",
                                 (attempts, int(time.time()) + delay, job['user_id']))
                log.warning('Auto WireGuard grant retry: %s', type(exc).__name__)
    return completed
