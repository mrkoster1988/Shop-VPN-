"""Short-lived MTProxy identity proof for one Shop WireGuard primary device.

Only a server-verified Telegram ID may identify a Shop account. No MTProxy
session, Shop cookie, WGDashboard token or administrative permission is shared.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import config as c, flow_auto_provision, flow_wireguard
from .db import db

_KEY = re.compile(r'[a-f0-9]{64}')
_TICKET = re.compile(r'psb1\.([A-Za-z0-9_-]{20,720})\.([a-f0-9]{64})')
_CLIENT = re.compile(r'pfc_[A-Za-z0-9_-]{43}')
_JTI = re.compile(r'[A-Za-z0-9_-]{32}')
_DOMAIN = b'pushok-flow-shop-v1\0'
_MAX_TG = 2**52
_HEADERS = {'Cache-Control':'private, no-store', 'Pragma':'no-cache',
            'Referrer-Policy':'no-referrer', 'X-Robots-Tag':'noindex, nofollow, noarchive'}


class ExchangeRequest(BaseModel):
    model_config = {'extra':'forbid'}
    ticket: str = Field(min_length=100, max_length=900)
    client_id: str = Field(min_length=47, max_length=47)


def headers():
    return _HEADERS.copy()


def _invalid():
    raise HTTPException(401, 'Подтверждение личности недействительно')


def _decode(encoded):
    if len(encoded) % 4 == 1:
        _invalid()
    try:
        raw = base64.b64decode(encoded+'='*((-len(encoded))%4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        _invalid()
    if base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') != encoded:
        _invalid()
    return raw


def verify(ticket, *, now=None):
    match = _TICKET.fullmatch(ticket) if isinstance(ticket, str) else None
    if not match:
        _invalid()
    key = os.getenv('FLOW_SHOP_BRIDGE_SECRET', '')
    if not _KEY.fullmatch(key):
        raise HTTPException(503, 'Связь с Pushok ID ещё не настроена')
    payload = _decode(match.group(1))
    if len(payload) > 650:
        _invalid()
    expected = hmac.new(bytes.fromhex(key), _DOMAIN+payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(match.group(2), expected):
        _invalid()
    def unique(pairs):
        result = {}
        for name,value in pairs:
            if name in result:
                raise ValueError('duplicate')
            result[name] = value
        return result
    try:
        claims = json.loads(payload.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
        if type(claims) is not dict or set(claims) != {
                'v','iss','aud','sub','tg','iat','exp','jti'}:
            _invalid()
        if type(claims['v']) is not int or claims['v'] != 1 or claims['iss'] != 'mtproxy' or claims['aud'] != 'pushok-shop':
            _invalid()
        if type(claims['sub']) is not str or str(UUID(claims['sub'])) != claims['sub']:
            _invalid()
        if type(claims['tg']) is not int or not 0 < claims['tg'] < _MAX_TG:
            _invalid()
        issued,expiry = claims['iat'],claims['exp']
        stamp = int(time.time()) if now is None else now
        if (type(issued) is not int or type(expiry) is not int or
                type(stamp) is not int or expiry != issued + 60 or
                not stamp-60 <= issued <= stamp+10 or expiry <= stamp):
            _invalid()
        if type(claims['jti']) is not str or not _JTI.fullmatch(claims['jti']):
            _invalid()
        if json.dumps(claims, ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode('ascii') != payload:
            _invalid()
    except (UnicodeError, ValueError, TypeError, OverflowError):
        _invalid()
    return {'subject':claims['sub'], 'telegram_id':claims['tg'],
            'expires':expiry, 'nonce_hash':hashlib.sha256(claims['jti'].encode('ascii')).hexdigest()}


def _bind(proof, client_id):
    if not isinstance(client_id,str) or not _CLIENT.fullmatch(client_id):
        raise HTTPException(422, 'Недопустимое устройство')
    if not c.SECURE_COOKIE or not c.PUBLIC_URL.startswith('https://'):
        raise HTTPException(503, 'Личный доступ требует HTTPS')
    now=int(time.time())
    client_hash=hashlib.sha256(client_id.encode('ascii')).hexdigest()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM flow_bridge_nonces WHERE expires<=?', (now,))
        # Bound the number of unexpired proofs even if another service leaks signed tokens.
        if conn.execute('SELECT count(*) FROM flow_bridge_nonces').fetchone()[0] >= 50000:
            raise HTTPException(503, 'Сервис временно перегружен')
        if conn.execute('SELECT 1 FROM flow_bridge_nonces WHERE nonce_hash=?', (proof['nonce_hash'],)).fetchone():
            raise HTTPException(409, {'code':'FLOW_PROOF_REPLAY'})
        linked=conn.execute('SELECT * FROM flow_bridge_identities WHERE mtproxy_uuid=?',
                            (proof['subject'],)).fetchone()
        other=conn.execute('SELECT * FROM flow_bridge_identities WHERE telegram_id=?',
                           (proof['telegram_id'],)).fetchone()
        if ((linked is not None and linked['telegram_id'] != proof['telegram_id'])
                or (other is not None and other['mtproxy_uuid'] != proof['subject'])):
            raise HTTPException(409, {'code':'FLOW_IDENTITY_CONFLICT'})
        row=conn.execute('SELECT id FROM users WHERE telegram_id=?', (proof['telegram_id'],)).fetchone()
        if row is None:
            conn.execute('INSERT INTO users(telegram_id,created) VALUES (?,?)',
                         (proof['telegram_id'],now))
            row=conn.execute('SELECT id FROM users WHERE telegram_id=?', (proof['telegram_id'],)).fetchone()
        uid=row['id']
        if linked is not None and linked['shop_user_id'] != uid:
            raise HTTPException(409, {'code':'FLOW_IDENTITY_CONFLICT'})
        if linked is None:
            conn.execute('INSERT INTO flow_bridge_identities(mtproxy_uuid,telegram_id,shop_user_id,created) VALUES (?,?,?,?)',
                         (proof['subject'], proof['telegram_id'],uid,now))
        device=conn.execute('SELECT client_hash FROM flow_bridge_devices WHERE shop_user_id=?',(uid,)).fetchone()
        if device is not None and device['client_hash'] != client_hash:
            raise HTTPException(409, {'code':'FLOW_DEVICE_BOUND'})
        if device is None:
            # Never lock a phone to a Shop identity before Shop has actually
            # issued an active WireGuard profile. A disabled/pending first
            # claim must not prevent using a phone when issuance is ready.
            ready=conn.execute(
                "SELECT 1 FROM subscriptions WHERE user_id=? AND protocol='wg' "
                "AND state='active' AND expires>? AND primary_key_state='active' "
                "AND secret IS NOT NULL LIMIT 1", (uid,now)).fetchone()
            if ready is not None:
                conn.execute('INSERT INTO flow_bridge_devices(shop_user_id,client_hash,created) VALUES (?,?,?)',
                             (uid,client_hash,now))
        conn.execute('INSERT INTO flow_bridge_nonces(nonce_hash,expires,created) VALUES (?,?,?)',
                     (proof['nonce_hash'],proof['expires'],now))
        return uid


def exchange(ticket, client_id):
    """Synchronous because profile creation is queued, never done in a request."""
    proof=verify(ticket)
    uid=_bind(proof,client_id)
    queued=flow_auto_provision.after_login(uid)
    discovered=flow_wireguard.discover(uid)['subscriptions']
    if not discovered:
        # The owner has not configured a free or paid entitlement.
        return {'status':'disabled' if queued == 'disabled' else 'unavailable',
                'subscription_url':None}
    subscription=discovered[0]
    state=subscription['state']
    if state!='active':
        return {'status':state if state in ('pending','expired','revoked') else 'unavailable',
                'subscription_url':None}
    sid=subscription['subscription_id']
    link=flow_wireguard.link_for_user(uid,sid)
    if not link['enabled']:
        if link['version'] != 0:
            # Respect the user's explicit disable; do not recreate a bearer link.
            return {'status':'link_disabled','subscription_url':None}
        link=flow_wireguard.change_link(uid,sid,'enable',0)
    if not link['enabled'] or not isinstance(link['url'],str) or not link['url'].startswith('https://'):
        raise HTTPException(503, 'Личная ссылка временно недоступна')
    return {'status':'active','subscription_url':link['url']}
