"""A standalone seven-day device entitlement. It cannot open a Shop account."""
import hmac
import re
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import config, flow_auto_provision, flow_identity_bridge
from .db import db, insert_user
from .security import digest


class GuestRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    device: str = Field(min_length=64, max_length=64, pattern='^[0-9a-f]{64}$')


def claim(device, authorization, network):
    match = re.fullmatch(r'Bearer ([a-f0-9]{64})', authorization)
    if not match:
        raise HTTPException(401, 'Требуется ключ гостя')
    key = digest('flow-guest-key:' + match[1])
    device = digest('flow-guest-device:' + device)
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT user_id,secret_hash FROM flow_guest_trials WHERE device_hash=?',
                           (device,)).fetchone()
        if row:
            if not hmac.compare_digest(row['secret_hash'], key) or not row['user_id']:
                return {'status': 'unavailable', 'subscription_url': None}
            uid = row['user_id']
        else:
            setting = conn.execute('SELECT * FROM flow_auto_policy WHERE id=1').fetchone()
            if not config.SECURE_COOKIE or not setting or not setting['enabled']:
                return {'status': 'disabled', 'subscription_url': None}
            if conn.execute('SELECT 1 FROM flow_guest_trials WHERE secret_hash=?', (key,)).fetchone():
                raise HTTPException(409, 'Гостевой ключ уже использован')
            network_hash = digest('flow-guest-network:' + network)
            recent = conn.execute('SELECT count(*) FROM flow_guest_trials WHERE network_hash=? AND created>?',
                                  (network_hash, now - 3600)).fetchone()[0]
            if recent >= 10:
                raise HTTPException(429, 'Повторите позже', headers={'Retry-After': '3600'})
            used = conn.execute('SELECT count(*) FROM flow_auto_grants').fetchone()[0]
            node = flow_auto_provision._node(conn)
            if used >= setting['max_users'] or node is None:
                return {'status': 'unavailable', 'subscription_url': None}
            uid = insert_user(conn, created=now)
            conn.execute('INSERT INTO flow_guest_trials VALUES (?,?,?,?,?)',
                         (device, key, uid, network_hash, now))
            flow_auto_provision.reserve(conn, uid, node, setting, now, 7)
    return flow_identity_bridge.subscription_reply(uid, 'unavailable')
