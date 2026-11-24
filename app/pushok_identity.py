"""Shop-сторона доверенной авторизации Pushok ID. Без импорта ролей и денег."""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import config as c
from .db import db
from .security import digest, session, session_user

COOKIE='shop_id_request'
REQUEST_TTL=300
SESSION_TTL=3600
MAX_PENDING=1000
_KEY=re.compile(r'[a-f0-9]{64}')
_AUDIENCE=re.compile(r'pushok-shop:[A-Za-z0-9_-]{16,64}')
_TOKEN=re.compile(r'[A-Za-z0-9_-]{43}')
_PROOF=re.compile(r'pid1\.([A-Za-z0-9_-]{20,1000})\.([a-f0-9]{64})')
_DOMAIN=b'pushok-id-shop-approval-v1\0'


class PolicyUpdate(BaseModel):
    model_config={'extra':'forbid'}
    enabled:bool=Field(strict=True)
    allow_new_accounts:bool=Field(strict=True)
    version:int=Field(ge=0,strict=True)


class Start(BaseModel):
    model_config={'extra':'forbid'}
    mode:Literal['signin','link']


class Approval(BaseModel):
    model_config={'extra':'forbid'}
    proof:str=Field(min_length=100,max_length=1070,strict=True,repr=False)


class Finish(BaseModel):
    model_config={'extra':'forbid'}
    confirmation:str=Field(pattern=r'^[a-f0-9]{64}$',strict=True)
    subject:UUID
    confirm:bool=Field(strict=True)

    @field_validator('confirm')
    @classmethod
    def explicit(cls,value):
        if not value:raise ValueError('Требуется явное подтверждение')
        return value


def configuration():
    key=os.getenv('SHOP_PUSHOK_ID_SECRET','')
    audience=os.getenv('SHOP_PUSHOK_ID_AUDIENCE','')
    portal=os.getenv('SHOP_PUSHOK_ID_PORTAL','https://auth.pushok.shop').rstrip('/')
    try:
        url=urlsplit(portal)
        url.port
    except ValueError:return None
    if (not c.SECURE_COOKIE or not c.PUBLIC_URL.startswith('https://') or not _KEY.fullmatch(key)
        or key in (c.ADMIN_TOKEN,os.getenv('FLOW_SHOP_BRIDGE_SECRET',''),os.getenv('PARTNER_WEBHOOK_SECRET',''))
        or not _AUDIENCE.fullmatch(audience) or url.scheme!='https' or not url.hostname
        or url.username or url.password or url.query or url.fragment
        or not re.fullmatch(r'[A-Za-z0-9.-]+',url.hostname or '')
        or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*',url.path)
        or any(ord(ch)<33 or ord(ch)>126 for ch in portal)):
        return None
    fingerprint=digest(json.dumps([key,audience,portal,c.PUBLIC_URL],separators=(',',':')))
    return {'key':bytes.fromhex(key),'audience':audience,'portal':portal,'fingerprint':fingerprint}


def policy(conn):
    row=conn.execute('SELECT * FROM shop_id_policy WHERE id=1').fetchone()
    if not row:return {'enabled':False,'allow_new_accounts':False,'version':0,'updated':0,'config_hash':None}
    return {'enabled':bool(row['enabled']),'allow_new_accounts':bool(row['allow_new_accounts']),
            'version':row['version'],'updated':row['updated'],'config_hash':row['config_hash']}


def trusted(cfg,p):
    return bool(cfg and p['enabled'] and p['config_hash'] and
                hmac.compare_digest(cfg['fingerprint'],p['config_hash']))


def public_capabilities():
    with db() as conn:return {'enabled':trusted(configuration(),policy(conn)),
                              'wallet_status':'not_connected'}


def own_link(uid):
    with db() as conn:
        row=conn.execute('SELECT subject,created FROM shop_id_links WHERE user_id=?',(uid,)).fetchone()
        return {'linked':row is not None,'subject':row['subject'] if row else None,
                'linked_at':row['created'] if row else None,'wallet_status':'not_connected'}


def admin_view(conn):
    now=int(time.time());p=policy(conn);cfg=configuration()
    return {k:v for k,v in p.items() if k!='config_hash'}|{'configured':cfg is not None,
              'available':trusted(cfg,p),'reapproval_required':bool(p['enabled'] and not trusted(cfg,p)),
              'linked_accounts':conn.execute('SELECT count(*) FROM shop_id_links').fetchone()[0],
              'pending_requests':conn.execute('SELECT count(*) FROM shop_id_requests WHERE expires>?',(now,)).fetchone()[0],
              'active_id_sessions':conn.execute("""SELECT count(*) FROM shop_id_sessions b JOIN sessions s
                  ON s.token=b.session_hash WHERE s.expires>?""",(now,)).fetchone()[0],
              'wallet_status':'not_connected','session_ttl':SESSION_TTL,
              'central_issuer_verified':False}


def admin_status():
    with db() as conn:
        conn.execute('BEGIN')
        return admin_view(conn)


def update_policy(data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=policy(conn)
        if old['version']!=data.version:raise HTTPException(409,'Настройки изменились. Обновите раздел.')
        cfg=configuration()
        if data.enabled and cfg is None:
            raise HTTPException(409,'Сначала настройте HTTPS, отдельный секрет и идентификатор магазина.')
        fingerprint=cfg['fingerprint'] if data.enabled else None
        if (old['enabled'],old['allow_new_accounts'],old['config_hash'])!=(data.enabled,data.allow_new_accounts,fingerprint):
            conn.execute("""INSERT INTO shop_id_policy(id,enabled,allow_new_accounts,version,updated,config_hash)
                VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,
                allow_new_accounts=excluded.allow_new_accounts,version=excluded.version,updated=excluded.updated,
                config_hash=excluded.config_hash""",
                (int(data.enabled),int(data.allow_new_accounts),data.version+1,int(time.time()),fingerprint))
            conn.execute('DELETE FROM shop_id_requests')
            if not data.enabled or old['config_hash']!=fingerprint:
                conn.execute('DELETE FROM sessions WHERE token IN (SELECT session_hash FROM shop_id_sessions)')
        return admin_view(conn)


def session_valid(conn,fingerprint):
    cfg=configuration()
    return bool(trusted(cfg,policy(conn)) and hmac.compare_digest(cfg['fingerprint'],fingerprint))


def _enabled(conn):
    cfg=configuration();p=policy(conn)
    if not trusted(cfg,p):raise HTTPException(503,'Вход через Pushok ID не включён или конфигурация требует подтверждения')
    return cfg,p


def _browser(request):
    value=request.cookies.get(COOKIE,'')
    return digest(value) if _TOKEN.fullmatch(value) else None


def _clear_cookie(response):
    response.delete_cookie(COOKIE,path=c.COOKIE_PATH,secure=c.SECURE_COOKIE,httponly=True,samesite='lax')


def _request(conn,request,now,cfg,p):
    browser=_browser(request)
    row=conn.execute('SELECT * FROM shop_id_requests WHERE browser_hash=?',(browser,)).fetchone() if browser else None
    if not row or row['expires']<=now or row['policy_version']!=p['version'] or row['config_hash']!=cfg['fingerprint']:
        return None
    return row


def _same_browser_account(conn,request,row,now):
    raw=request.cookies.get('session','')
    local=session_user(conn,raw,now)
    if row['mode']=='link':
        if not local or local['id']!=row['local_user_id'] or digest(raw)!=row['local_session_hash']:
            raise HTTPException(409,'Сеанс магазина изменился. Начните привязку заново.')
    elif local:
        raise HTTPException(409,'В магазине уже выполнен вход. Начните привязку из личного кабинета.')
    return local


def start(data,request):
    now=int(time.time());raw=secrets.token_urlsafe(32);browser=secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        cfg,p=_enabled(conn)
        local=session_user(conn,request.cookies.get('session',''),now)
        if data.mode=='link' and (not local or not local['telegram_id']):
            raise HTTPException(409,'Привязка требует входа в Shop с тем же подтверждённым Telegram ID. Email-аккаунты не объединяются автоматически.')
        if data.mode=='signin' and local:raise HTTPException(409,'В магазине уже выполнен вход')
        conn.execute('DELETE FROM shop_id_requests WHERE expires<=?',(now,))
        old=_browser(request)
        if old:conn.execute('DELETE FROM shop_id_requests WHERE browser_hash=?',(old,))
        if conn.execute('SELECT count(*) FROM shop_id_requests').fetchone()[0]>=MAX_PENDING:
            raise HTTPException(503,'Слишком много запросов входа. Повторите позже.')
        conn.execute("""INSERT INTO shop_id_requests
            (request_hash,browser_hash,mode,local_user_id,local_session_hash,policy_version,config_hash,created,expires)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (digest(raw),digest(browser),data.mode,local['id'] if local else None,
             digest(request.cookies.get('session','')) if local else None,p['version'],cfg['fingerprint'],now,now+REQUEST_TTL))
        # Только случайный идентификатор запроса. Cookie и подпись никогда не передаются в URL.
        url=cfg['portal']+'/?'+urlencode({'shop_request':raw,'shop_audience':cfg['audience']})
    response=JSONResponse({'state':'pending','approval_url':url,'expires_at':now+REQUEST_TTL})
    response.set_cookie(COOKIE,browser,max_age=REQUEST_TTL,secure=True,httponly=True,
                        samesite='lax',path=c.COOKIE_PATH)
    return response


def invalid():
    raise HTTPException(401,'Подтверждение Pushok ID недействительно')


def verify(proof,cfg,now):
    match=_PROOF.fullmatch(proof)
    if not match:invalid()
    encoded,signature=match.groups()
    try:
        raw=base64.b64decode(encoded+'='*((-len(encoded))%4),altchars=b'-_',validate=True)
        if len(raw)>750 or base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')!=encoded:invalid()
        expected=hmac.new(cfg['key'],_DOMAIN+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):invalid()
        claims=json.loads(raw.decode('ascii'))
        if type(claims) is not dict or set(claims)!={'v','iss','aud','purpose','request','sub','tg','iat','exp'}:invalid()
        if type(claims['v']) is not int or claims['v']!=1 or claims['iss']!='pushok-id' or claims['aud']!=cfg['audience'] or claims['purpose']!='shop_identity':invalid()
        if type(claims['request']) is not str or not _TOKEN.fullmatch(claims['request']):invalid()
        if type(claims['sub']) is not str or str(UUID(claims['sub']))!=claims['sub']:invalid()
        if type(claims['tg']) is not int or not 0<claims['tg']<2**52:invalid()
        if (type(claims['iat']) is not int or type(claims['exp']) is not int or claims['exp']!=claims['iat']+60
            or not now-60<=claims['iat']<=now+10 or claims['exp']<=now):invalid()
        if json.dumps(claims,ensure_ascii=True,separators=(',',':'),sort_keys=True).encode('ascii')!=raw:invalid()
    except (ValueError,UnicodeError,TypeError,OverflowError,binascii.Error):
        invalid()
    return claims


def approve(data):
    """Back-channel from ID. Signed proof approves a pending request; never creates a session."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        cfg,p=_enabled(conn);now=int(time.time())
        proof=verify(data.proof,cfg,now)
        row=conn.execute('SELECT * FROM shop_id_requests WHERE request_hash=?',(digest(proof['request']),)).fetchone()
        if (not row or row['expires']<=now or row['policy_version']!=p['version']
            or row['config_hash']!=cfg['fingerprint']):
            raise HTTPException(409,'Запрос входа отсутствует или устарел')
        if row['subject'] is not None:raise HTTPException(409,'Запрос уже подтверждён')
        conn.execute('UPDATE shop_id_requests SET subject=?,telegram_id=?,approved_at=? WHERE request_hash=?',
                     (proof['sub'],proof['tg'],now,row['request_hash']))
    return {'ok':True}


def status(request):
    with db() as conn:
        conn.execute('BEGIN')
        cfg,p=_enabled(conn);now=int(time.time())
        row=_request(conn,request,now,cfg,p)
        if not row:return {'state':'missing'}
        _same_browser_account(conn,request,row,now)
        if row['subject'] is None:return {'state':'pending','expires_at':row['expires']}
        return {'state':'approved','confirmation':row['request_hash'],'subject':row['subject'],
                'telegram_id':row['telegram_id'],'mode':row['mode'],'expires_at':row['expires']}


def finish(data,request):
    """Связывание и выдача короткой Shop-сессии только после явного подтверждения браузера."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now=int(time.time());cfg,p=_enabled(conn)
        row=_request(conn,request,now,cfg,p)
        if not row or not row['subject'] or not hmac.compare_digest(row['request_hash'],data.confirmation) or row['subject']!=str(data.subject):
            raise HTTPException(409,'Подтверждение изменилось или истекло. Начните вход заново.')
        local=_same_browser_account(conn,request,row,now)
        tg=row['telegram_id'];subject=row['subject']
        if local and local['telegram_id']!=tg:
            raise HTTPException(409,'Telegram ID аккаунтов не совпадают. Объединение запрещено.')
        existing=conn.execute('SELECT id FROM users WHERE telegram_id=?',(tg,)).fetchone()
        by_subject=conn.execute('SELECT * FROM shop_id_links WHERE subject=?',(subject,)).fetchone()
        by_tg=conn.execute('SELECT * FROM shop_id_links WHERE telegram_id=?',(tg,)).fetchone()
        if ((by_subject and (by_subject['telegram_id']!=tg or not existing or by_subject['user_id']!=existing['id']))
            or (by_tg and by_tg['subject']!=subject)):
            raise HTTPException(409,'Привязка конфликтует с существующим аккаунтом. Обратитесь в поддержку.')
        if existing is None:
            if local or not p['allow_new_accounts']:raise HTTPException(409,'Регистрация через Pushok ID пока закрыта')
            uid=conn.execute('INSERT INTO users(telegram_id,created) VALUES (?,?)',(tg,now)).lastrowid
        else:uid=existing['id']
        by_user=conn.execute('SELECT subject FROM shop_id_links WHERE user_id=?',(uid,)).fetchone()
        if by_user and by_user['subject']!=subject:
            raise HTTPException(409,'Аккаунт уже связан с другим Pushok ID')
        if not by_subject:
            conn.execute('INSERT INTO shop_id_links(subject,user_id,telegram_id,created) VALUES (?,?,?,?)',(subject,uid,tg,now))
        conn.execute('DELETE FROM shop_id_requests WHERE request_hash=?',(row['request_hash'],))
        response=JSONResponse({'ok':True,'linked':True,'signed_in':row['mode']=='signin','wallet_status':'not_connected'})
        if row['mode']=='signin':
            token_hash=session(response,uid,conn,ttl=SESSION_TTL)
            conn.execute('INSERT INTO shop_id_sessions(session_hash,user_id,config_hash) VALUES (?,?,?)',
                         (token_hash,uid,cfg['fingerprint']))
        _clear_cookie(response)
    if row['mode']=='signin':
        from . import flow_auto_provision
        flow_auto_provision.after_login(uid)
    return response


def cancel(request,response=None):
    browser=_browser(request)
    if browser:
        with db() as conn:conn.execute('DELETE FROM shop_id_requests WHERE browser_hash=?',(browser,))
    if response is None:response=JSONResponse({'ok':True})
    _clear_cookie(response)
    return response


def revoke(request,user):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        raw=digest(request.cookies.get('session',''))
        signed_out=conn.execute('SELECT 1 FROM shop_id_sessions WHERE session_hash=? AND user_id=?',(raw,user['id'])).fetchone() is not None
        count=conn.execute('DELETE FROM sessions WHERE token IN (SELECT session_hash FROM shop_id_sessions WHERE user_id=?)',(user['id'],)).rowcount
        conn.execute('DELETE FROM shop_id_requests WHERE local_user_id=?',(user['id'],))
    response=JSONResponse({'ok':True,'revoked':count,'signed_out':signed_out})
    if signed_out:response.delete_cookie('session',path=c.COOKIE_PATH)
    _clear_cookie(response)
    return response
