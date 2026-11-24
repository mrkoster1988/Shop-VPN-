import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl
from fastapi import HTTPException, Request
from . import config as c
from .db import one, execute, db


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def password_hash(password):
    salt = secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()

def password_valid(password, encoded):
    if not encoded:
        return False
    salt, expected = encoded.split(':')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return hmac.compare_digest(actual, expected)

def session(response, user_id, conn=None, *, ttl=604800):
    raw = secrets.token_urlsafe(32)
    if type(ttl) is not int or not 1<=ttl<=604800:raise ValueError('Invalid session lifetime')
    values = (digest(raw), user_id, int(time.time()) + ttl)
    if conn is None:
        execute('INSERT INTO sessions VALUES (?,?,?)', values)
    else:
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', values)
    response.set_cookie('session', raw, max_age=ttl, httponly=True,
                        secure=c.SECURE_COOKIE, samesite='lax', path=c.COOKIE_PATH)
    return values[0]

def session_user(conn,raw,now=None):
    row=conn.execute("""SELECT u.*,b.config_hash AS id_config_hash FROM users u
        JOIN sessions s ON s.user_id=u.id LEFT JOIN shop_id_sessions b ON b.session_hash=s.token
        WHERE s.token=? AND s.expires>?""",(digest(raw),int(time.time()) if now is None else now)).fetchone()
    if not row:return None
    result=dict(row)
    fingerprint=result.pop('id_config_hash')
    if fingerprint is not None:
        from . import pushok_identity
        if not pushok_identity.session_valid(conn,fingerprint):return None
    return result


def current_user(request: Request):
    with db() as conn:
        conn.execute('BEGIN')
        user=session_user(conn,request.cookies.get('session',''))
    if not user:raise HTTPException(401,'Войдите в личный кабинет')
    return user


def admin(request: Request):
    from .staff import authorize
    return authorize(request)

def telegram_user(raw):
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs) or not c.BOT_TOKEN:
            raise ValueError()
        received = data.pop('hash')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        key = hmac.new(b'WebAppData', c.BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError()
        age = time.time() - int(data['auth_date'])
        if age < -30 or age > 3600:
            raise ValueError()
        user = json.loads(data['user'])
        if not isinstance(user['id'], int) or user['id'] <= 0:
            raise ValueError()
        return user
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, 'Данные Telegram недействительны или устарели')
