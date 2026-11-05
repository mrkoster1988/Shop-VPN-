import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl
from fastapi import HTTPException, Request
from . import config as c
from .db import one, execute


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

def session(response, user_id, conn=None):
    raw = secrets.token_urlsafe(32)
    values = (digest(raw), user_id, int(time.time()) + 604800)
    if conn is None:
        execute('INSERT INTO sessions VALUES (?,?,?)', values)
    else:
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', values)
    response.set_cookie('session', raw, max_age=604800, httponly=True,
                        secure=c.SECURE_COOKIE, samesite='lax', path=c.COOKIE_PATH)

def current_user(request: Request):
    user = one('''SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                  WHERE s.token=? AND s.expires>?''',
               (digest(request.cookies.get('session', '')), int(time.time())))
    if not user:
        raise HTTPException(401, 'Войдите в личный кабинет')
    return user

def admin(request: Request):
    token = request.headers.get('authorization', '')
    if not c.ADMIN_TOKEN or not hmac.compare_digest(token, 'Bearer ' + c.ADMIN_TOKEN):
        raise HTTPException(403, 'Нет доступа')

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
