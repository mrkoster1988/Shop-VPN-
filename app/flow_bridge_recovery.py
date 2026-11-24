"""Developer-only diagnostics and safe release of never-issued Flow phone binding.

This is NOT a WireGuard key reissue. Rebinding an already-issued profile
would share its private key with two devices; require a separate confirmed
server-side rotation for that case.
"""
from fastapi import HTTPException
from pydantic import BaseModel, Field

from .db import db


class ReleaseRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    binding_created: int = Field(ge=1, le=9223372036854775807, strict=True)
    confirm: str = Field(pattern=r'^RELEASE_UNUSED_FLOW_BINDING$')


def _snapshot(conn, user_id: int):
    user = conn.execute('SELECT id FROM users WHERE id=?', (user_id,)).fetchone()
    if user is None:
        raise HTTPException(404, 'Пользователь не найден')
    bound = conn.execute(
        'SELECT created FROM flow_bridge_devices WHERE shop_user_id=?',
        (user_id,)).fetchone()
    blocked = None
    rows = conn.execute(
        "SELECT id,state,secret,credential_id,primary_key_state "
        "FROM subscriptions WHERE user_id=? AND protocol='wg'",
        (user_id,)).fetchall()
    for sub in rows:
        # A pending record is safe only when its existing queue never
        # attempted a remote operation and nothing resembling a key exists.
        if (sub['state'] != 'pending' or sub['secret'] is not None
                or sub['credential_id'] is not None
                or sub['primary_key_state'] != 'active'):
            blocked = 'wg_profile_may_be_issued'
            break
        grant = conn.execute(
            'SELECT state,attempts FROM flow_auto_grants WHERE '
            'user_id=? AND subscription_id=?',
            (user_id, sub['id'])).fetchone()
        if grant is None or grant['state'] != 'pending' or grant['attempts'] != 0:
            blocked = 'wg_provisioning_unconfirmed'
            break
    return {
        'bound': bound is not None,
        'binding_created': bound['created'] if bound is not None else None,
        'has_wg_subscription': bool(rows),
        'can_release': bound is not None and blocked is None,
        'blocker': blocked,
    }


def inspect(user_id: int):
    """No client hashes, tickets, device identifiers or WireGuard secrets."""
    with db() as conn:
        return _snapshot(conn, user_id)


def release_unused(user_id: int, request: ReleaseRequest):
    """Only release a never-provisioned claim; never revoke/move a peer here.

    Call under service.lock to prevent the Shop background issuer and bridge
    exchange from racing the same account. A creation timestamp prevents
    approving a newly replaced binding from a stale admin screen.
    """
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        status = _snapshot(conn, user_id)
        if not status['bound'] or status['binding_created'] != request.binding_created:
            raise HTTPException(409, 'Привязка изменилась. Обновите карточку.')
        if not status['can_release']:
            raise HTTPException(409, 'У WireGuard возможен уже выданный ключ. '
                                'Сначала проверьте peer и выполните подтверждённый перевыпуск.')
        cursor = conn.execute(
            'DELETE FROM flow_bridge_devices WHERE shop_user_id=? AND created=?',
            (user_id, request.binding_created))
        if cursor.rowcount != 1:
            raise HTTPException(409, 'Привязка изменилась. Обновите карточку.')
    return {'ok': True, 'status': 'released'}
