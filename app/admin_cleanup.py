"""Owner-only, deliberately conservative cleanup of Shop access and drafts.

Never erase paid orders or personal WireGuard keys before the remote client is
confirmed disabled. A failed or ambiguous panel response leaves the record in
place so retry uses the same identity.
"""
import sqlite3
import time

from fastapi import HTTPException
from . import config as c, providers, servers, service
from .db import db


def _blocked_orders(conn, user_id, protocol):
    return conn.execute(
        "SELECT 1 FROM orders WHERE user_id=? AND protocol=? "
        "AND status IN ('pending','checkout','paid') LIMIT 1",
        (user_id, protocol),
    ).fetchone() is not None


def _unfinished_rotation(conn, sid):
    primary = conn.execute(
        "SELECT 1 FROM primary_rotations WHERE subscription_id=? "
        "AND state NOT IN ('completed','canceled') LIMIT 1", (sid,),
    ).fetchone()
    device = conn.execute(
        "SELECT 1 FROM device_rotations WHERE device_id IN "
        "(SELECT id FROM devices WHERE subscription_id=?) "
        "AND state NOT IN ('completed','canceled') LIMIT 1", (sid,),
    ).fetchone()
    return bool(primary or device)


def _remote_identity(sub, *, server_profile):
    """Return whether an external identity must be revoked, or fail closed."""
    if not sub.get('server_id'):
        if sub.get('secret') or sub.get('credential_id') or sub.get('state') == 'active':
            raise HTTPException(409, 'Привязка сервера отсутствует. Удаление небезопасно.')
        return False
    if sub['protocol'] == 'wg' and server_profile.get('api') == 'wgdashboard':
        try:
            identity = service.primary_identity(sub)
        except Exception:
            raise HTTPException(409, 'Не удалось проверить идентификатор WireGuard') from None
        with db() as conn:
            key = conn.execute(
                'SELECT 1 FROM wgdashboard_keys WHERE server_id=? AND credential_id=?',
                (sub['server_id'], identity),
            ).fetchone()
        if not key:
            # Only a never-issued pending record is safe to cancel locally.
            # An expired/revoked record might still have an unmanaged remote peer.
            if sub.get('secret') or sub.get('credential_id') or sub['state'] != 'pending':
                raise HTTPException(409, 'Не найден сохранённый ключ WGDashboard. '
                                    'Сначала проверьте сервер вручную.')
            return False
    return True


async def revoke_subscription(sid: str):
    async with service.lock:
        return await revoke_locked(sid)


async def revoke_locked(sid, request_id=None):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        found = conn.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
        if not found:
            raise HTTPException(404, 'Подписка не найдена')
        sub = dict(found)
        if conn.execute("SELECT 1 FROM vpn_access_changes WHERE subscription_id=? AND action='tariff' AND applied IS NULL",(sid,)).fetchone():
            raise HTTPException(409,'vpn_pending_operation')
        if conn.execute("SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'",
                        (sid,)).fetchone():
            raise HTTPException(409, 'Есть незавершённое продление WireGuard. Сначала выполните сверку.')
        if request_id is None and _blocked_orders(conn, sub['user_id'], sub['protocol']):
            raise HTTPException(409, 'Есть незавершённый заказ этого протокола. '
                                'Сначала завершите оплату или разберите заказ.')
        if _unfinished_rotation(conn, sid):
            raise HTTPException(409, 'Идёт смена ключей. Дождитесь завершения или отмены.')
        devices = [dict(row) for row in conn.execute(
            'SELECT * FROM devices WHERE subscription_id=? ORDER BY id', (sid,),
        )]
        confirmed = conn.execute(
            'SELECT 1 FROM admin_subscription_revocations WHERE subscription_id=?',
            (sid,),
        ).fetchone()
        if confirmed and sub['state'] == 'revoked' and all(
                row['state'] == 'revoked' for row in devices):
            if request_id:
                conn.execute('UPDATE vpn_access_changes SET applied=? WHERE request_id=?', (int(time.time()), request_id))
            return {'ok': True, 'status': 'revoked'}

    profile = None
    if sub['server_id']:
        try:
            _, profile = servers.for_subscription(sub, revoking=True)
        except Exception:
            raise HTTPException(409, 'Нельзя найти прежнюю привязку сервера. '
                                'Запись сохранена до проверки узла.') from None
    try:
        if sub['protocol'] == 'wg':
            from . import wg_fleet
            await wg_fleet.revoke_placements(sub, include_primary=False)
        if profile is None:
            _remote_identity(sub, server_profile={})
            if any(d.get('secret') or d.get('credential_id') or
                   d['state'] == 'active' for d in devices):
                raise HTTPException(409, 'Не найден сервер для дополнительных пиров. '
                                    'Сначала восстановите привязку.')
        else:
            if _remote_identity(sub, server_profile=profile):
                await providers.disable({**sub, 'id': service.primary_identity(sub)})
            for device in devices:
                if device['state'] == 'revoked':
                    continue
                remote = {**sub, 'id': service.device_identity(device),
                          'secret': device.get('secret'), 'credential_id': device.get('credential_id'),
                          'state': device['state']}
                if _remote_identity(remote, server_profile=profile):
                    await providers.disable({**sub, 'id': service.device_identity(device)})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, 'Панель не подтвердила отзыв подключения. '
                            'Запись сохранена для безопасного повтора.') from None

    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if request_id is None and _blocked_orders(conn, sub['user_id'], sub['protocol']):
            raise HTTPException(409, 'Во время удаления появился незавершённый заказ.')
        conn.execute("UPDATE subscriptions SET state='revoked',expires=MIN(expires,?),"
                     "secret=NULL,primary_key_state='revoked',free_auto_renew=0,"
                     "free_max_renewals=0 WHERE id=?", (now, sid))
        conn.execute("UPDATE devices SET state='revoked',expires=0,secret=NULL,"
                     "next_attempt=0,error=NULL WHERE subscription_id=?", (sid,))
        if sub['protocol']=='wg':
            from . import wg_fleet
            wg_fleet.confirmed_primary_revocation(conn,sid)
        conn.execute("UPDATE flow_auto_grants SET state='superseded' "
                     "WHERE subscription_id=?", (sid,))
        conn.execute('DELETE FROM flow_wireguard_links WHERE subscription_id=?', (sid,))
        conn.execute('DELETE FROM flow_wireguard_device_links WHERE device_id IN '
                     '(SELECT id FROM devices WHERE subscription_id=?)', (sid,))
        conn.execute('UPDATE subscription_links SET enabled=0,token_hash=NULL,'
                     'token_secret=NULL WHERE subscription_id=?', (sid,))
        conn.execute("""INSERT OR REPLACE INTO admin_subscription_revocations(
            subscription_id,user_id,protocol,server_id,revoked_at)
            VALUES (?,?,?,?,?)""",
            (sid, sub['user_id'], sub['protocol'], sub['server_id'], now))
        if request_id:
            conn.execute('UPDATE vpn_access_changes SET applied=? WHERE request_id=?',(now,request_id))
    return {'ok': True, 'status': 'revoked'}



def purge_blocker(conn, sub):
    """A customer-safe reason why this subscription cannot be purged."""
    sid = sub['id']
    if conn.execute("SELECT 1 FROM wg_fleet_placements WHERE subscription_id=? AND state!='revoked'",(sid,)).fetchone():
        return 'Не все подключения на основном и резервных серверах подтверждённо отозваны.'
    if sub['state'] != 'revoked' or not conn.execute(
            'SELECT 1 FROM admin_subscription_revocations WHERE subscription_id=?',
            (sid,)).fetchone():
        return 'Сначала нужно отозвать подключение на сервере.'
    if _blocked_orders(conn, sub['user_id'], sub['protocol']):
        return 'Есть незавершённые заказы этого протокола.'
    if conn.execute('SELECT 1 FROM flow_auto_grants WHERE subscription_id=?',
                    (sid,)).fetchone():
        return 'История бесплатной автовыдачи сохраняется для защиты лимитов.'
    if conn.execute('SELECT 1 FROM referral_redemptions WHERE subscription_id=?',
                    (sid,)).fetchone():
        return 'Подписка связана с реферальной историей.'
    if conn.execute('SELECT 1 FROM primary_rotations WHERE subscription_id=?',
                    (sid,)).fetchone() or conn.execute(
                    'SELECT 1 FROM device_rotations WHERE device_id IN '
                    '(SELECT id FROM devices WHERE subscription_id=?)',
                    (sid,)).fetchone():
        return 'Нужно сохранить историю смены ключей.'
    if conn.execute("SELECT 1 FROM devices WHERE subscription_id=? "
                    "AND state!='revoked'", (sid,)).fetchone():
        return 'Не все дополнительные подключения отозваны.'
    return None


def purge_subscription(sid: str):
    """Purge only after our confirmed revoke; retain payment and referral history."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Подписка не найдена')
        sub = dict(row)
        reason = purge_blocker(conn, sub)
        if reason:
            raise HTTPException(409, reason)
        if sub['state'] != 'revoked' or not conn.execute(
                'SELECT 1 FROM admin_subscription_revocations WHERE subscription_id=?',
                (sid,)).fetchone():
            raise HTTPException(409, 'Сначала отзовите пиры кнопкой «Отозвать доступ».')
        if _blocked_orders(conn, sub['user_id'], sub['protocol']):
            raise HTTPException(409, 'Незавершённые заказы не позволяют удалить запись.')
        if conn.execute('SELECT 1 FROM flow_auto_grants WHERE subscription_id=?',
                        (sid,)).fetchone():
            raise HTTPException(409, 'Это подписка автовыдачи. История активации '
                                'сохраняется; доступ уже можно отозвать.')
        if conn.execute('SELECT 1 FROM referral_redemptions WHERE subscription_id=?',
                        (sid,)).fetchone():
            raise HTTPException(409, 'Подписка связана с реферальной историей.')
        if conn.execute('SELECT 1 FROM primary_rotations WHERE subscription_id=?',
                        (sid,)).fetchone() or conn.execute(
                        'SELECT 1 FROM device_rotations WHERE device_id IN '
                        '(SELECT id FROM devices WHERE subscription_id=?)',
                        (sid,)).fetchone():
            raise HTTPException(409, 'Есть история смены ключей; запись нужно сохранить.')
        devices = conn.execute(
            'SELECT * FROM devices WHERE subscription_id=?', (sid,),
        ).fetchall()
        if any(device['state'] != 'revoked' for device in devices):
            raise HTTPException(409, 'Не все дополнительные пиры отозваны.')
        conn.execute('DELETE FROM flow_wireguard_device_links WHERE device_id IN '
                     '(SELECT id FROM devices WHERE subscription_id=?)', (sid,))
        conn.execute('DELETE FROM flow_wireguard_device_versions WHERE device_id IN '
                     '(SELECT id FROM devices WHERE subscription_id=?)', (sid,))
        conn.execute('DELETE FROM devices WHERE subscription_id=?', (sid,))
        conn.execute('DELETE FROM subscription_links WHERE subscription_id=?', (sid,))
        conn.execute('DELETE FROM flow_wireguard_links WHERE subscription_id=?', (sid,))
        conn.execute('DELETE FROM flow_wireguard_versions WHERE subscription_id=?', (sid,))
        # The successful remote revoke is recorded before any persisted key
        # is erased. If a historical identity cannot be decrypted, do not purge.
        try:
            identities = [service.primary_identity(sub)] + [
                service.device_identity(dict(device)) for device in devices]
        except Exception:
            raise HTTPException(409, 'Не удалось проверить сохранённые идентификаторы '
                                'пиров. Запись оставлена для ручной проверки.') from None
        if sub['server_id']:
            for identity in identities:
                conn.execute('DELETE FROM wgdashboard_expirations '
                             'WHERE server_id=? AND credential_id=?',
                             (sub['server_id'], identity))
                conn.execute('DELETE FROM wgdashboard_keys '
                             'WHERE server_id=? AND credential_id=?',
                             (sub['server_id'], identity))
        try:
            conn.execute('DELETE FROM subscriptions WHERE id=?', (sid,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Запись связана с другими данными. '
                                'Для защиты истории оставлена в архиве.') from None
    return {'ok': True, 'status': 'deleted'}


async def remove_subscription(sid: str):
    """One owner action: revoke on the pinned panel, then purge if allowed.

    Financial records, grant history and key-rotation history stay intact.
    A retained revoked record is not reported as a successful physical deletion.
    """
    with db() as conn:
        if conn.execute("SELECT 1 FROM subscriptions WHERE id=?", (sid,)).fetchone() is None:
            if conn.execute("SELECT 1 FROM admin_subscription_revocations WHERE subscription_id=?",
                            (sid,)).fetchone():
                return {'ok': True, 'status': 'deleted'}
            raise HTTPException(404, 'Подписка не найдена')
    await revoke_subscription(sid)
    async with service.lock:
        with db() as conn:
            row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
            if row is None:
                return {'ok': True, 'status': 'deleted'}
            if row['state'] != 'revoked':
                raise HTTPException(409, 'Состояние подписки изменилось. Обновите карточку.')
            reason = purge_blocker(conn, dict(row))
        if reason:
            return {'ok': True, 'status': 'revoked_retained', 'reason': reason}
        try:
            return purge_subscription(sid)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return {'ok': True, 'status': 'revoked_retained', 'reason': exc.detail}


def delete_unused_plan(plan_id: str):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        plan = conn.execute('SELECT id,enabled FROM plans WHERE id=?', (plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(404, 'Тариф не найден')
        if plan_id in {item['id'] for item in c.PLANS}:
            raise HTTPException(409, 'Базовый тариф можно скрыть, но нельзя удалить.')
        if plan['enabled']:
            raise HTTPException(409, 'Сначала отключите тариф от новых продаж.')
        if conn.execute('SELECT 1 FROM orders WHERE plan=? LIMIT 1', (plan_id,)).fetchone():
            raise HTTPException(409, 'У тарифа есть заказы. Скрывайте его вместо удаления.')
        if conn.execute('SELECT 1 FROM coupons WHERE plan_id=? LIMIT 1', (plan_id,)).fetchone():
            raise HTTPException(409, 'Тариф используется промокодами.')
        conn.execute('DELETE FROM plan_changes WHERE plan_id=?', (plan_id,))
        conn.execute('DELETE FROM plans WHERE id=?', (plan_id,))
    return {'ok': True, 'status': 'deleted'}


def delete_unused_server(sid: str):
    """Only an empty, disabled server may be removed from the registry."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT id,state FROM servers WHERE id=?', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Сервер не найден')
        if row['state'] != 'disabled':
            raise HTTPException(409, 'Сначала выключите сервер.')
        if conn.execute('SELECT 1 FROM subscriptions WHERE server_id=?', (sid,)).fetchone():
            raise HTTPException(409, 'К серверу привязаны подписки. Сначала разберите их.')
        for table in ('wgdashboard_keys','wgdashboard_expirations',
                      'tuic_credentials','hysteria2_credentials'):
            if conn.execute('SELECT 1 FROM '+table+' WHERE server_id=?', (sid,)).fetchone():
                raise HTTPException(409, 'В сервере хранятся ключи или задания. '
                                    'Нужно проверить их отзыв.')
        conn.execute('DELETE FROM server_changes WHERE server_id=?', (sid,))
        conn.execute('DELETE FROM server_probe_history WHERE server_id=?', (sid,))
        conn.execute('DELETE FROM server_routing_nodes WHERE server_id=?', (sid,))
        try:
            conn.execute('DELETE FROM servers WHERE id=?', (sid,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Сервер связан с другими данными.') from None
    return {'ok': True, 'status': 'deleted'}
