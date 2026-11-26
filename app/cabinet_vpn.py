"""Account-scoped VPN controls delegated by the authenticated MTProxy cabinet."""
import hashlib
import hmac
import os
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import flow_owner_extension, flow_presence, service, wg_peer_diagnostics
from .db import db

router = APIRouter(prefix='/api/flow')
DOMAIN = b'pushok-cabinet-vpn-management-v1:'


class Command(BaseModel):
    model_config = {'extra': 'forbid'}
    actor: UUID
    subject: UUID
    action: Literal['extend', 'shorten', 'limit', 'retry', 'revoke', 'restore', 'expire']
    subscription_id: str = Field(min_length=1, max_length=100)
    request_id: UUID
    before: int = Field(ge=0, strict=True)
    value: int = Field(ge=0, le=365, strict=True)


def authorize(body, proof, domain=DOMAIN):
    key = os.getenv('FLOW_SHOP_BRIDGE_SECRET', '')
    match = re.fullmatch(r'(\d{10}):([0-9a-f]{64})', proof)
    if not re.fullmatch('[0-9a-f]{64}', key) or not match or abs(time.time()-int(match[1])) > 45:
        raise HTTPException(403)
    signature = hmac.digest(bytes.fromhex(key), domain + match[1].encode() + b':' + body, 'sha256')
    if not hmac.compare_digest(signature.hex(), match[2]):
        raise HTTPException(403)


@router.post('/cabinet-management')
async def manage(cmd: Command, request: Request):
    body = await request.body()
    authorize(body, request.headers.get('X-Pushok-Management', ''))
    with db() as conn:
        sub = conn.execute("""SELECT s.* FROM subscriptions s JOIN flow_bridge_identities b
            ON b.shop_user_id=s.user_id WHERE b.mtproxy_uuid=? AND s.id=? AND s.protocol='wg'""",
            (str(cmd.subject), cmd.subscription_id)).fetchone()
    if not sub:
        raise HTTPException(404, 'vpn_subscription_missing')
    uid = sub['user_id']
    fingerprint = hashlib.sha256(cmd.model_dump_json().encode()).hexdigest()
    rid = str(cmd.request_id)
    if cmd.action == 'limit':
        if cmd.value > 20:
            raise HTTPException(422, 'vpn_limit_range')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT digest FROM cabinet_vpn_limits WHERE request_id=?', (rid,)).fetchone()
            if prior:
                if prior['digest'] != fingerprint:
                    raise HTTPException(409, 'vpn_changed')
            else:
                used = conn.execute("SELECT count(*) FROM devices WHERE subscription_id=? AND state!='revoked'",
                                    (sub['id'],)).fetchone()[0]
                if cmd.value < used:
                    raise HTTPException(409, 'vpn_profiles_in_use')
                if not conn.execute("""UPDATE subscriptions SET device_limit=? WHERE id=? AND user_id=?
                        AND protocol='wg' AND device_limit=?""", (cmd.value, sub['id'], uid, cmd.before)).rowcount:
                    raise HTTPException(409, 'vpn_changed')
                conn.execute('INSERT INTO cabinet_vpn_limits VALUES (?,?,?,?,?,?,?)',
                             (rid, fingerprint, str(cmd.actor), sub['id'], cmd.before, cmd.value, int(time.time())))
    elif cmd.action in ('revoke','restore','expire'):
        from . import vpn_lifecycle
        try:
            await vpn_lifecycle.change(cmd)
        except HTTPException as error:
            if error.detail == 'vpn_restore_elapsed':
                await flow_presence.invalidate()
            raise
        except Exception:
            raise HTTPException(503,"vpn_change_unconfirmed") from None
    elif cmd.action == 'retry':
        if sub['state'] != 'pending':
            raise HTTPException(409, 'vpn_changed')
        await wg_peer_diagnostics.retry_pending(uid)
    else:
        if cmd.value < 1:
            raise HTTPException(422, 'vpn_days_range')
        async with service.lock:
            direction = 'SHORTEN_ACCOUNT_WIREGUARD' if cmd.action == 'shorten' else 'EXTEND_ACCOUNT_WIREGUARD'
            reason = 'Кабинет MTProxy · ' + str(cmd.actor)
            with db() as conn:
                old = conn.execute('SELECT * FROM flow_owner_extensions WHERE request_id=?', (rid,)).fetchone()
            if old:
                if (old['user_id'] != uid or old['subscription_id'] != sub['id'] or not old['reason'].startswith('Кабинет MTProxy · ') or
                        old['before_expires'] != cmd.before or old['days'] != cmd.value or
                        (old['target_expires'] < old['before_expires']) != (cmd.action == 'shorten')):
                    raise HTTPException(409, 'vpn_changed')
                if old['status'] == 'applied':
                    return {'ok': True}
                reason = old['reason']
                target = old['target_expires']
            else:
                plan = flow_owner_extension.proposal(uid, cmd.value, cmd.action == 'shorten')
                if plan['subscription_id'] != sub['id'] or plan['before_expires'] != cmd.before:
                    raise HTTPException(409, 'vpn_changed')
                target = plan['target_expires']
            extension = flow_owner_extension.Extension(request_id=cmd.request_id, subscription_id=sub['id'],
                before_expires=cmd.before, target_expires=target, days=cmd.value, reason=reason, confirm=direction)
            await flow_owner_extension.extend(uid, extension)
    await flow_presence.invalidate()
    return {'ok': True}


@router.post('/vpn-settings/{action}',include_in_schema=False)
async def settings(action: Literal['read','access','plans','plan-create','plan-update','plan-choice'],request:Request):
    from . import flow_auto_provision as access
    from pydantic import ValidationError
    body=await request.body()
    authorize(action.encode()+b':'+body,request.headers.get('X-Pushok-Management',''),b'pushok-vpn-settings-v1:')
    try:
        if action.startswith('plan'):
            from . import vpn_plans
            try:
                result = await vpn_plans.catalog(action,body)
            except (KeyError,TypeError,ValueError):
                raise HTTPException(422,'vpn_plan_invalid') from None
            if action != 'plans':
                await flow_presence.invalidate()
            return result
        result=access.policy() if action=='read' else await access.update_policy(access.PolicyUpdate.model_validate_json(body))
    except ValidationError: raise HTTPException(422) from None
    return {k:result[k] for k in ('enabled','days','max_users','version','used','remaining')}
