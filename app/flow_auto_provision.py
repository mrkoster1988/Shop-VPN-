"""Opt-in starter WireGuard access for authenticated Shop users.

Disabled by default. No MTProxy read-only token, no implicit payment,
no remote API call on an authentication request. One claim per Shop user.
"""
import logging
import os
import time
import uuid

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
    """Owner-only status, without account identities or backend API credentials."""
    with db() as conn:
        row = dict(conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone())
        used = conn.execute('SELECT count(*) FROM flow_auto_grants').fetchone()[0]
        pending = conn.execute("SELECT count(*) FROM flow_auto_grants WHERE state='pending'").fetchone()[0]
        retrying = conn.execute("SELECT count(*) FROM flow_auto_grants WHERE state='pending' AND attempts>0").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM flow_auto_grants WHERE state='active'").fetchone()[0]
        existing = conn.execute("SELECT count(*) FROM subscriptions WHERE protocol='wg'").fetchone()[0]
        ready_node = _node(conn) is not None
    enabled = bool(row['enabled'])
    mtproxy_only = os.getenv('FLOW_TRIAL_MTPROXY_ONLY', '').lower() == 'true'
    if not enabled:
        state = 'policy_disabled'
    elif not ready_node:
        state = 'wgdashboard_not_ready'
    elif retrying:
        state = 'issuance_retrying'
    elif pending:
        state = 'issuance_pending'
    else:
        state = 'ready'
    return {**row, 'enabled': enabled,
            'used': used, 'remaining': max(0, row['max_users'] - used),
            'diagnostics': {
                'state': state,
                'ready_node': ready_node,
                'pending': pending,
                'retrying': retrying,
                'active': active,
                'existing_wg_subscriptions': existing,
                'mtproxy_only': mtproxy_only,
            }}


def _node(conn):
    """Общий выбор новых узлов плюс обязательный защищённый срок WGDashboard."""
    from . import server_routing
    for row in server_routing.candidates(conn,'wg'):
        try:
            profile=servers.decode(row)
        except Exception:
            continue
        if (profile.get('api')=='wgdashboard' and profile.get('schedule_expiry') is True
                and bool(profile.get('panel_timezone'))):
            return row
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


def enqueue(user_id: int) -> str:
    """Reserve a bounded grant without network IO or duplicate paid subscriptions.

    Existing claims persist across policy changes and across multiple logins.
    Expired existing WireGuard access is NOT silently extended.
    """
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone() is None:
            raise HTTPException(401, 'Аккаунт не найден')
        grant = conn.execute('SELECT state FROM flow_auto_grants WHERE user_id=?',
                             (user_id,)).fetchone()
        if grant:
            return grant['state']
        if conn.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND protocol='wg'",
                        (user_id,)).fetchone():
            return 'existing'
        setting = conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone()
        if not setting or not setting['enabled']:
            return 'disabled'
        # This installation may restrict free Flow access to verified MTProxy
        # identities. Registration in Shop alone must not spend the pilot quota.
        if os.getenv('FLOW_TRIAL_MTPROXY_ONLY', '').lower() == 'true':
            linked = conn.execute(
                'SELECT 1 FROM flow_bridge_identities WHERE shop_user_id=?',
                (user_id,)).fetchone()
            if linked is None:
                return 'not_mtproxy'
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
        return 'pending'


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
