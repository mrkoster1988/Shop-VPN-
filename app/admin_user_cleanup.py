"""Owner-only removal of unused accounts without losing business history."""
import hashlib
import json
import sqlite3

from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from .db import db, reserve_deleted_user_id


class DeleteUser(BaseModel):
    model_config = {'extra': 'forbid'}
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm: Literal['DELETE_UNUSED_CUSTOMER']


# All business/history references must be retained, even if no access is active.
_BLOCKERS = (
    ('orders', 'user_id', 'Заказы'),
    ('subscriptions', 'user_id', 'Подписки'),
    ('support_cases', 'user_id', 'Обращения в поддержку'),
    ('ads_requests', 'user_id', 'Рекламные заявки'),
    ('coupon_uses', 'user_id', 'Использованные промокоды'),
    ('referral_codes', 'user_id', 'Реферальный код'),
    ('referral_bindings', 'invitee_id', 'Пригласивший пользователь'),
    ('referral_bindings', 'inviter_id', 'Приглашённые пользователи'),
    ('referral_rewards', 'invitee_id', 'Реферальные начисления'),
    ('referral_rewards', 'inviter_id', 'Реферальные награды'),
    ('referral_redemptions', 'user_id', 'Использованные бонусы'),
    ('trial_claims', 'user_id', 'Пробные активации'),
    ('flow_auto_grants', 'user_id', 'История автовыдачи'),
    ('flow_owner_extensions', 'user_id', 'История продлений'),
    ('admin_subscription_revocations', 'user_id', 'История отзыва подписок'),
    ('shop_id_links', 'user_id', 'Привязка Pushok ID'),
    ('flow_bridge_identities', 'shop_user_id', 'Связь с аккаунтом Flow'),
    ('flow_bridge_devices', 'shop_user_id', 'История привязки Flow'),
)


def _snapshot(conn, uid):
    user = conn.execute('SELECT id,email,telegram_id,created FROM users WHERE id=?',
                        (uid,)).fetchone()
    if user is None:
        raise HTTPException(404, 'Пользователь не найден')
    blockers = []
    for table, column, label in _BLOCKERS:
        count = conn.execute(f'SELECT count(*) FROM {table} WHERE {column}=?',
                             (uid,)).fetchone()[0]
        if count:
            blockers.append({'label': label, 'count': count})
    tg = user['telegram_id']
    if tg is not None:
        count = conn.execute('SELECT count(*) FROM partner_events WHERE telegram_id=?',
                             (tg,)).fetchone()[0]
        if count:
            blockers.append({'label': 'Партнёрская история', 'count': count})
    ephemeral = {
        label: conn.execute(sql, args).fetchone()[0]
        for label, sql, args in (
            ('sessions', 'SELECT count(*) FROM sessions WHERE user_id=?', (uid,)),
            ('tickets', 'SELECT count(*) FROM tickets WHERE user_id=?', (uid,)),
            ('password_resets', 'SELECT count(*) FROM password_reset_tokens WHERE user_id=?', (uid,)),
            ('id_sessions', 'SELECT count(*) FROM shop_id_sessions WHERE user_id=?', (uid,)),
            ('id_requests', 'SELECT count(*) FROM shop_id_requests WHERE local_user_id=? OR telegram_id=?',
             (uid, tg)),
        )
    }
    # Include identity and counts, not passwords, session tokens or private keys.
    fingerprint = hashlib.sha256(json.dumps(
        [dict(user), blockers, ephemeral], sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()
    return {'user_id': uid, 'can_delete': not blockers, 'blockers': blockers,
            'auth_records': ephemeral, 'revision': fingerprint}


def preview(uid):
    with db() as conn:
        conn.execute('BEGIN')
        return _snapshot(conn, uid)


def remove(uid, command: DeleteUser):
    """Called under the existing service lock; no panel calls or cascade of sales."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        snapshot = _snapshot(conn, uid)
        if snapshot['revision'] != command.revision:
            raise HTTPException(409, 'Данные изменились. Повторите проверку удаления.')
        if not snapshot['can_delete']:
            raise HTTPException(409, 'У пользователя есть связанные данные. История сохраняется.')
        tg = conn.execute('SELECT telegram_id FROM users WHERE id=?', (uid,)).fetchone()[0]
        # Never allow a delayed operation for a deleted account to target a new one.
        reserve_deleted_user_id(conn, uid)
        conn.execute('DELETE FROM shop_id_requests WHERE local_user_id=? OR telegram_id=?', (uid, tg))
        conn.execute('DELETE FROM shop_id_sessions WHERE user_id=?', (uid,))
        for table in ('sessions', 'tickets', 'password_reset_tokens'):
            conn.execute('DELETE FROM '+table+' WHERE user_id=?', (uid,))
        try:
            conn.execute('DELETE FROM users WHERE id=?', (uid,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Обнаружены связанные записи. Удаление отменено.') from None
    return {'ok': True, 'status': 'deleted', 'user_id': uid}
