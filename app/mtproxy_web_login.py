"""Sign into this same-origin Shop using explicit consent in the MTProxy cabinet."""
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config as c, flow_identity_bridge as bridge
from .db import db
from .security import digest, session, session_user


router = APIRouter(prefix='/api/auth/mtproxy')
COOKIE = 'shop_mtproxy_login'
LIFETIME = 300


class Finish(BaseModel):
    model_config = {'extra': 'forbid'}
    ticket: str = Field(min_length=100, max_length=900)


def enabled():
    return (os.getenv('FLOW_TRIAL_MTPROXY_ONLY', '').lower() == 'true'
            and c.SECURE_COOKIE and urlsplit(c.PUBLIC_URL).path == '/shop'
            and bool(re.fullmatch(r'[a-f0-9]{64}', os.getenv('FLOW_SHOP_BRIDGE_SECRET', ''))))


def check_request(request):
    if not enabled():
        raise HTTPException(503, 'Вход через MTProxy недоступен')
    site = urlsplit(c.PUBLIC_URL)
    if request.method == 'POST' and request.headers.get('origin') != f'{site.scheme}://{site.netloc}':
        raise HTTPException(403, 'Начните вход в этом магазине')


def require_challenge(conn, request):
    raw = request.cookies.get(COOKIE, '')
    if re.fullmatch(r'[a-zA-Z0-9_-]{43}', raw):
        challenge = digest(raw)
        if conn.execute('SELECT 1 FROM mtproxy_web_requests WHERE challenge=? AND expires>?',
                        (challenge, int(time.time()))).fetchone():
            return challenge
    raise HTTPException(409, 'Срок запроса истёк. Начните вход в магазине снова.')


def require_signed_out(conn, request):
    if session_user(conn, request.cookies.get('session', '')):
        raise HTTPException(409, 'Сначала выйдите из открытого аккаунта магазина.')


@router.post('/start')
def start(request: Request):
    check_request(request)
    raw = secrets.token_urlsafe(32)
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        require_signed_out(conn, request)
        conn.execute('DELETE FROM mtproxy_web_requests WHERE expires<=? OR challenge=?',
                     (now, digest(request.cookies.get(COOKIE, ''))))
        if conn.execute('SELECT count(*) FROM mtproxy_web_requests').fetchone()[0] >= 5000:
            raise HTTPException(429, 'Повторите вход позже')
        conn.execute('INSERT INTO mtproxy_web_requests VALUES (?,?)', (digest(raw), now + LIFETIME))
    response = JSONResponse({'ok': True}, headers=bridge.headers())
    response.set_cookie(COOKIE, raw, max_age=LIFETIME, path=c.COOKIE_PATH,
                        secure=True, httponly=True, samesite='strict')
    return response


@router.get('/challenge')
def challenge(request: Request):
    check_request(request)
    with db() as conn:
        return JSONResponse({'challenge': require_challenge(conn, request)}, headers=bridge.headers())


@router.post('/finish')
def finish(data: Finish, request: Request):
    check_request(request)
    response = JSONResponse({'ok': True}, headers=bridge.headers())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        require_signed_out(conn, request)
        challenge = require_challenge(conn, request)
        proof = bridge.verify(data.ticket, browser_challenge=challenge)
        uid = bridge.bind_identity_locked(conn, proof)
        conn.execute('DELETE FROM mtproxy_web_requests WHERE challenge=?', (challenge,))
        session(response, uid, conn, ttl=3600)
    response.delete_cookie(COOKIE, path=c.COOKIE_PATH, secure=True, httponly=True, samesite='strict')
    return response
