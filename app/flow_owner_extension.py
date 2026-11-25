"""Audited owner-only extension of an existing account WireGuard entitlement.

The remote WGDashboard deletion schedule is confirmed BEFORE the local
subscription is extended. Never silently change nodes, create an entitlement,
touch billing, or claim success after an unconfirmed remote operation.
"""
import time
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from . import config as c, providers, servers, service, wgdashboard
from .db import db


class Extension(BaseModel):
    model_config = {'extra': 'forbid'}
    request_id: UUID
    subscription_id: str = Field(min_length=1, max_length=100)
    before_expires: int = Field(ge=0, strict=True)
    target_expires: int = Field(ge=1, strict=True)
    days: int = Field(ge=1, le=365, strict=True)
    reason: str = Field(min_length=5, max_length=240)
    confirm: Literal['EXTEND_ACCOUNT_WIREGUARD']


def _require_stored_key(conn, server_id, identity):
    """A renewal cannot replace a previously issued private key."""
    try:
        if str(UUID(identity)) != identity:
            raise ValueError('Invalid identity')
        row = conn.execute(
            'SELECT private_key FROM wgdashboard_keys WHERE server_id=? AND credential_id=?',
            (server_id, identity)).fetchone()
        if row is None:
            raise ValueError('Missing key')
        wgdashboard.WGDashboard.identity(c.decrypt(row['private_key']))
    except Exception:
        raise HTTPException(409, 'Не найден или повреждён сохранённый ключ WireGuard. '
                            'Сначала проверьте пир; продление не создаёт новый ключ.') from None


def _devices(conn, sub):
    # Do not race a device rotation or an unfinished issue/revoke job.
    if conn.execute(
        "SELECT 1 FROM devices WHERE subscription_id=? AND state NOT IN ('active','revoked')",
        (sub['id'],)).fetchone() or conn.execute(
        "SELECT 1 FROM device_rotations WHERE device_id IN "
        "(SELECT id FROM devices WHERE subscription_id=?) "
        "AND state NOT IN ('completed','canceled')",
        (sub['id'],)).fetchone():
        raise HTTPException(409, 'Есть незавершённая операция с дополнительным профилем.')
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM devices WHERE subscription_id=? AND state='active' ORDER BY id",
        (sub['id'],))]
    for device in rows:
        if not device['secret']:
            raise HTTPException(409, 'Дополнительный профиль не имеет сохранённого конфига.')
        try:
            identity = service.device_identity(device)
        except Exception:
            raise HTTPException(409, 'Повреждён идентификатор дополнительного профиля.') from None
        _require_stored_key(conn, sub['server_id'], identity)
    return rows


def _subscription(conn, user_id):
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND protocol='wg'",
        (user_id,)).fetchone()
    if row is None:
        if conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone() is None:
            raise HTTPException(404, 'Пользователь не найден')
        raise HTTPException(404, 'У аккаунта нет WireGuard-подписки')
    sub = dict(row)
    if (sub['state'] not in ('active', 'expired') or
            sub['primary_key_state'] != 'active' or
            not sub['secret'] or not sub['server_id']):
        raise HTTPException(409, 'Для продления нужен сохранённый WireGuard-ключ. Проверьте подписку и пир.')
    if conn.execute(
        "SELECT 1 FROM orders WHERE user_id=? AND protocol='wg' "
        "AND status IN ('pending','checkout','paid') LIMIT 1",
        (user_id,)).fetchone() is not None:
        raise HTTPException(409, 'Есть незавершённый заказ WireGuard; сначала разберите его.')
    if conn.execute(
        "SELECT 1 FROM primary_rotations WHERE subscription_id=? "
        "AND state NOT IN ('completed','canceled') LIMIT 1",
        (sub['id'],)).fetchone() is not None:
        raise HTTPException(409, 'Основной WireGuard-ключ перевыпускается.')
    server = conn.execute("SELECT * FROM servers WHERE id=?", (sub['server_id'],)).fetchone()
    if (server is None or server['protocol'] != 'wg' or
            server['state'] not in ('active', 'draining') or server['health'] == 'error'):
        raise HTTPException(409, 'Исходный WGDashboard недоступен; миграция не выполняется.')
    try:
        profile = servers.decode(dict(server))
    except Exception:
        raise HTTPException(409, 'Не удалось проверить конфигурацию исходного WGDashboard') from None
    if (profile.get('api') != 'wgdashboard' or
            profile.get('schedule_expiry') is not True or
            not profile.get('panel_timezone')):
        raise HTTPException(409, 'Нужен WGDashboard с управлением сроком отключения peer.')
    try:
        identity = service.primary_identity(sub)
    except Exception:
        raise HTTPException(409, 'Повреждён идентификатор основного профиля.') from None
    _require_stored_key(conn, sub['server_id'], identity)
    _devices(conn, sub)
    return sub


def pending(user_id):
    """Owner-only recovery of the durable request, even after browser reload."""
    with db() as conn:
        if conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone() is None:
            raise HTTPException(404, 'Пользователь не найден')
        row = conn.execute(
            "SELECT request_id,subscription_id,before_expires,target_expires,days,reason "
            "FROM flow_owner_extensions WHERE user_id=? AND status='pending'",
            (user_id,)).fetchone()
        if row is None:
            return {'request': None}
        return {'request': {**dict(row), 'confirm': 'EXTEND_ACCOUNT_WIREGUARD'}}


def proposal(user_id, days):
    if type(days) is not int or not 1 <= days <= 365:
        raise HTTPException(422, 'Срок продления: от 1 до 365 дней')
    with db() as conn:
        sub = _subscription(conn, user_id)
        blocked = conn.execute(
            "SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'",
            (sub['id'],)).fetchone()
        if blocked:
            raise HTTPException(409, 'Предыдущее продление не завершено; повторите его запрос.')
        new_expiry = max(int(time.time()), int(sub['expires'])) + days * 86400
        return {
            'subscription_id': sub['id'],
            'before_expires': int(sub['expires']),
            'target_expires': new_expiry,
            'days': days,
        }


async def extend(user_id, command: Extension, staff_id=None):
    """Call under service.lock. A reused request_id repeats the SAME target."""
    rid = str(command.request_id)
    reason = command.reason.strip()
    if len(reason) < 5:
        raise HTTPException(422, 'Укажите причину продления')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        sub = _subscription(conn, user_id)
        if sub['id'] != command.subscription_id:
            raise HTTPException(409, 'Подписка аккаунта изменилась')
        entry = conn.execute('SELECT * FROM flow_owner_extensions WHERE request_id=?',
                             (rid,)).fetchone()
        if entry is not None:
            if (entry['user_id'] != user_id or entry['subscription_id'] != sub['id'] or
                    entry['before_expires'] != command.before_expires or
                    entry['target_expires'] != command.target_expires or
                    entry['days'] != command.days or entry['reason'] != reason):
                raise HTTPException(409, 'Идентификатор продления уже использован с другими данными')
            if entry['status'] == 'applied' and sub['expires'] == command.target_expires:
                return {'ok': True, 'status': 'extended', 'expires_at': command.target_expires}
        else:
            if int(sub['expires']) != command.before_expires:
                raise HTTPException(409, 'Срок подписки изменился. Пересчитайте продление.')
            base = max(int(time.time()), command.before_expires)
            # The owner must first obtain a short-lived server-side plan.
            expected = base + command.days * 86400
            if abs(command.target_expires - expected) > 300:
                raise HTTPException(409, 'План продления устарел. Обновите карточку.')
            if conn.execute(
                "SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'",
                (sub['id'],)).fetchone():
                raise HTTPException(409, 'Предыдущее продление ещё не подтверждено.')
            now = int(time.time())
            conn.execute(
                "INSERT INTO flow_owner_extensions(request_id,user_id,subscription_id,"
                "before_expires,target_expires,days,reason,status,created,updated,staff_id) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?,?)",
                (rid, user_id, sub['id'], command.before_expires, command.target_expires,
                 command.days, reason, now, now, staff_id))
        if sub['expires'] != command.before_expires:
            raise HTTPException(409, 'Текущий срок подписки отличается от исходного')
        identity = service.primary_identity(sub)
        devices = _devices(conn, sub)

    # The existing stored WG key is reused and only its schedule updated.
    # Any failure leaves the durable request pending for an identical retry.
    try:
        config = await providers.provision({**sub, 'id': identity,
                                            'expires': command.target_expires,
                                            'require_existing_wg_key': True})
        if (not isinstance(config, str) or len(config) > 65536 or
                not config.lstrip().startswith('[Interface]') or '[Peer]' not in config):
            raise ValueError('UnconfirmedWireGuardProfile')
        encrypted = c.encrypt(config)
        device_secrets = []
        for device in devices:
            value = await providers.provision({
                **sub, 'id': service.device_identity(device),
                'expires': command.target_expires, 'require_existing_wg_key': True})
            if (not isinstance(value, str) or len(value) > 65536 or
                    not value.lstrip().startswith('[Interface]') or '[Peer]' not in value):
                raise ValueError('UnconfirmedWireGuardDevice')
            device_secrets.append((c.encrypt(value), command.target_expires, device['id']))
    except Exception:
        raise HTTPException(502, 'WGDashboard не подтвердил продление. '
                                'Срок не изменён; повторите тот же запрос.') from None

    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute(
            "SELECT expires,state FROM subscriptions WHERE id=? AND user_id=?",
            (sub['id'], user_id)).fetchone()
        if current is None or current['expires'] != command.before_expires or current['state'] not in ('active', 'expired'):
            raise HTTPException(409, 'Состояние подписки изменилось. Нужна сверка с WGDashboard.')
        if [d['id'] for d in _devices(conn, sub)] != [d['id'] for d in devices]:
            raise HTTPException(409, 'Состав дополнительных профилей изменился. Нужна сверка.')
        conn.executemany(
            "UPDATE devices SET secret=?,expires=?,error=NULL WHERE id=? AND state='active'",
            device_secrets)
        conn.execute(
            "UPDATE subscriptions SET expires=?,state='active',secret=?,primary_key_state='active' "
            "WHERE id=? AND user_id=? AND expires=?",
            (command.target_expires, encrypted, sub['id'], user_id, command.before_expires))
        # Existing one-time grant remains ONE claim; no additional quota or
        # payment is created. Owner extension is recorded separately.
        conn.execute(
            "UPDATE flow_auto_grants SET target_expires=?,state='active' "
            "WHERE user_id=? AND subscription_id=? AND state IN ('active','expired')",
            (command.target_expires, user_id, sub['id']))
        conn.execute(
            "UPDATE flow_owner_extensions SET status='applied',updated=? "
            "WHERE request_id=? AND status='pending'", (int(time.time()), rid))
    return {'ok': True, 'status': 'extended', 'expires_at': command.target_expires}
