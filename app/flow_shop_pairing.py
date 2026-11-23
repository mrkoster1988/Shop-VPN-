"""Shop Flow pairing. Separate, revocable, read-only access; never a Shop cookie."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import config as c, flow_wireguard, flow_auto_provision
from .db import db
from .security import current_user, digest

router = APIRouter(prefix='/api/flow/shop')
REQUEST_TTL = 300
SESSION_TTL = 43200
MAX_PENDING = 1000
SCOPE = 'wireguard:read'


class Start(BaseModel):
    model_config = {'extra': 'forbid'}
    challenge: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', min_length=43, max_length=43)


class Decision(BaseModel):
    model_config = {'extra': 'forbid'}
    code: str = Field(pattern=r'^[a-f0-9]{16}$', min_length=16, max_length=16)
    decision: Literal['approve', 'deny']


class Exchange(BaseModel):
    model_config = {'extra': 'forbid'}
    code: str = Field(pattern=r'^[a-f0-9]{16}$', min_length=16, max_length=16)
    verifier: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', min_length=43, max_length=43)


def enabled():
    return (os.getenv('FLOW_SHOP_PAIRING_ENABLED', 'false') == 'true'
            and c.SECURE_COOKIE and c.PUBLIC_URL.startswith('https://'))


def require_enabled():
    if not enabled():
        raise HTTPException(503, 'Вход Pushok Flow через Shop пока недоступен')


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _token(code, verifier):
    # Детерминированный повтор обмена без хранения сырого токена.
    return 'psf_' + _b64(hmac.new(verifier.encode('ascii'),
        ('pushok-shop-flow-session-v1:' + code).encode('ascii'), hashlib.sha256).digest())


def _session(conn, request, now):
    raw = request.headers.get('authorization', '')
    if not re.fullmatch(r'Bearer psf_[A-Za-z0-9_-]{43}', raw):
        raise HTTPException(401, 'Требуется вход Pushok Flow')
    row = conn.execute("""SELECT p.user_id,p.code_hash,p.token_expires
        FROM flow_shop_pairings p JOIN sessions s ON s.token=p.browser_session
        AND s.user_id=p.user_id WHERE p.token_hash=? AND p.state='exchanged'
        AND p.token_expires>? AND s.expires>?""",
        (digest(raw[7:]), now, now)).fetchone()
    if not row:
        raise HTTPException(401, 'Сеанс Pushok Flow истёк или отозван')
    return row


@router.get('/capabilities')
async def capabilities():
    return {'enabled': enabled(), 'protocol': 1, 'scope': SCOPE,
            'request_ttl': REQUEST_TTL, 'session_ttl': SESSION_TTL, 'poll_interval': 5}


@router.post('/auth/start')
async def start(data: Start):
    require_enabled()
    now = int(time.time())
    code = secrets.token_hex(8)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("""DELETE FROM flow_shop_pairings WHERE
            (state!='exchanged' AND request_expires<=?) OR
            (state='exchanged' AND token_expires<=?)""", (now, now))
        if conn.execute("""SELECT count(*) FROM flow_shop_pairings
            WHERE state IN ('pending','approved')""").fetchone()[0] >= MAX_PENDING:
            raise HTTPException(429, 'Слишком много запросов входа')
        if conn.execute('SELECT 1 FROM flow_shop_pairings WHERE challenge=?',
                        (data.challenge,)).fetchone():
            raise HTTPException(409, 'Создайте новый запрос входа')
        conn.execute("""INSERT INTO flow_shop_pairings
            (code_hash,challenge,state,created,request_expires)
            VALUES (?,?,'pending',?,?)""", (digest(code), data.challenge, now, now+REQUEST_TTL))
    return {'code': code, 'expires_at': now+REQUEST_TTL,
            'approval_url': c.PUBLIC_URL.rstrip('/')+'/?view=account#flow-pair='+code,
            'poll_interval': 5}


@router.post('/auth/decision')
async def decision(data: Decision, request: Request, user=Depends(current_user)):
    require_enabled()
    now = int(time.time())
    session_hash = digest(request.cookies.get('session', ''))
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        browser = conn.execute('SELECT 1 FROM sessions WHERE token=? AND user_id=? AND expires>?',
                               (session_hash, user['id'], now)).fetchone()
        if not browser:
            raise HTTPException(401, 'Войдите в личный кабинет')
        row = conn.execute("""SELECT state,user_id,browser_session,request_expires
             FROM flow_shop_pairings WHERE code_hash=?""", (digest(data.code),)).fetchone()
        if not row or row['request_expires']<=now:
            raise HTTPException(410, 'Запрос входа истёк или недоступен')
        state = 'approved' if data.decision=='approve' else 'denied'
        if row['state']!='pending':
            if row['state']==state and row['user_id']==user['id'] and row['browser_session']==session_hash:
                return {'state': state}
            raise HTTPException(409, 'Запрос уже обработан')
        if state=='approved':
            # A newer explicit approval supersedes an older unexchanged request.
            conn.execute("DELETE FROM flow_shop_pairings WHERE user_id=? AND state='approved'",
                         (user['id'],))
        conn.execute("""UPDATE flow_shop_pairings SET state=?,user_id=?,browser_session=?
            WHERE code_hash=?""", (state, user['id'], session_hash, digest(data.code)))
    # Подготовка только по существующей политике владельца, без списаний и удалённых вызовов.
    if state=='approved':
        flow_auto_provision.after_login(user['id'])
    return {'state': state}


@router.post('/auth/exchange')
async def exchange(data: Exchange):
    require_enabled()
    now = int(time.time())
    challenge = _b64(hashlib.sha256(data.verifier.encode('ascii')).digest())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM flow_shop_pairings WHERE code_hash=?',
                           (digest(data.code),)).fetchone()
        if not row or not hmac.compare_digest(row['challenge'], challenge):
            raise HTTPException(404, 'Запрос входа не найден')
        if row['request_expires']<=now:
            raise HTTPException(410, 'Запрос входа истёк')
        if row['state'] in ('pending','denied'):
            return {'state': row['state'], 'retry_after': 5 if row['state']=='pending' else 0}
        browser = conn.execute('SELECT expires FROM sessions WHERE token=? AND user_id=? AND expires>?',
                               (row['browser_session'], row['user_id'], now)).fetchone()
        if not browser:
            raise HTTPException(401, 'Сеанс кабинета истёк')
        token = _token(data.code, data.verifier)
        hashed = digest(token)
        if row['state']=='exchanged':
            if row['token_expires']<=now or not hmac.compare_digest(row['token_hash'], hashed):
                raise HTTPException(401, 'Сеанс отозван')
            expiry = row['token_expires']
        else:
            expiry = min(now+SESSION_TTL, browser['expires'])
            # В первой версии только один активный сеанс Flow на аккаунт.
            # Это не ротация WireGuard-ключа и не отключение ранее скачанного конфига.
            conn.execute("DELETE FROM flow_shop_pairings WHERE user_id=? AND state='exchanged'",
                         (row['user_id'],))
            conn.execute("""UPDATE flow_shop_pairings SET state='exchanged',token_hash=?,token_expires=?
                            WHERE code_hash=?""", (hashed, expiry, digest(data.code)))
    return {'state':'authorized', 'access_token':token, 'token_type':'Bearer',
            'scope':SCOPE, 'expires_at':expiry}


@router.get('/connections')
async def connections(request: Request):
    require_enabled()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _session(conn, request, int(time.time()))
        return flow_wireguard._discover_locked(conn, row['user_id'])


@router.get('/subscriptions/{sid}')
async def subscription(sid: str, request: Request):
    require_enabled()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _session(conn, request, int(time.time()))
        return flow_wireguard._feed_locked(conn, row['user_id'], sid)


@router.post('/auth/logout')
async def logout(request: Request):
    # Отзыв доступен даже при отключении новой авторизации.
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _session(conn, request, int(time.time()))
        conn.execute('DELETE FROM flow_shop_pairings WHERE code_hash=?', (row['code_hash'],))
    return {'ok': True}


@router.post('/auth/revoke')
async def revoke(request: Request, user=Depends(current_user)):
    # Владелец аккаунта отзывает все связанные запросы и сеансы.
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        browser = conn.execute('SELECT 1 FROM sessions WHERE token=? AND user_id=? AND expires>?',
            (digest(request.cookies.get('session','')), user['id'], int(time.time()))).fetchone()
        if not browser:
            raise HTTPException(401, 'Войдите в личный кабинет')
        conn.execute('DELETE FROM flow_shop_pairings WHERE user_id=?', (user['id'],))
    return {'ok':True}
