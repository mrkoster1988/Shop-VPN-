import asyncio
import base64
import secrets
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit
import qrcode
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Path as APIPath
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field, field_validator, ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount
from . import config as c, providers, service, servers, trials, promos, referrals, diagnostics
from .db import init_db, db, one, execute, all_rows
from .security import current_user, admin, session, password_hash, password_valid, digest, telegram_user

logging.basicConfig(level=logging.INFO)
# Telegram credentials are embedded in its API URL: never log HTTP request URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
STATIC = Path(__file__).parent / 'static'

@asynccontextmanager
async def lifespan(app):
    init_db()
    c.cipher()  # Fail before accepting purchases if encryption is not configured.
    if len(c.ADMIN_TOKEN) < 32:
        raise RuntimeError('ADMIN_TOKEN must contain at least 32 characters')
    if c.PAYMENT_MODE not in ('manual', 'yookassa'):
        raise RuntimeError('PAYMENT_MODE must be manual or yookassa')
    if c.PAYMENT_MODE == 'yookassa' and not all(os.getenv(k) for k in ('YOOKASSA_SHOP_ID', 'YOOKASSA_SECRET')):
        raise RuntimeError('Configure YooKassa credentials')
    try:
        servers.import_environment()
    except Exception:
        raise RuntimeError('Server registry import failed; check legacy provider settings') from None
    service.reset_health()
    tasks = []
    if os.getenv('BACKGROUND_TASKS', 'true') == 'true' and os.getenv('MAINTENANCE_MODE')!='true':
        tasks.append(asyncio.create_task(service.worker()))
        if c.BOT_TOKEN:
            tasks.append(asyncio.create_task(service.bot_loop()))
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task

app = FastAPI(title='Pushok VPN', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
@app.exception_handler(RequestValidationError)
async def safe_validation(request, exc):
    if request.url.path.removeprefix(c.BASE_PATH).startswith('/api/admin/servers'):
        return JSONResponse({'detail':'Проверьте поля сервера и профиль подключения.'},status_code=422)
    if request.url.path.removeprefix(c.BASE_PATH).startswith('/api/auth/'):
        return JSONResponse({'detail':'Проверьте данные для входа или смены пароля.'},status_code=422)
    return await request_validation_exception_handler(request,exc)

# Bounded per-process rate limits; run one process as required by SQLite worker design.
hits = defaultdict(deque)

@app.middleware('http')
async def safety(request, call_next):
    path = request.url.path.removeprefix(c.BASE_PATH)
    if os.getenv('MAINTENANCE_MODE')=='true' and path!='/health':
        headers={'Retry-After':'60','Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'}
        if path.startswith(('/api/','/sub/','/login')):
            return JSONResponse({'detail':'Магазин временно на обслуживании. Повторите позже.'},status_code=503,headers=headers)
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Магазин на обслуживании</title><body><main><h1>Скоро вернёмся</h1><p>Магазин временно на обслуживании. Пожалуйста, зайдите немного позже.</p></main></body></html>',status_code=503,headers=headers)
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        origin = request.headers.get('origin')
        if origin and origin != urlsplit(c.PUBLIC_URL)._replace(path='', query='', fragment='').geturl():
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
        if int(request.headers.get('content-length', '0') or 0) > 16384:
            return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
    if path.startswith(('/api/','/sub/')):
        ip = request.client.host if request.client else 'unknown'
        bucket = hits[(ip, 'sub' if path.startswith('/sub/') else 'auth' if path.startswith('/api/auth/') else 'api')]
        now = time.monotonic()
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        limit = 60 if path.startswith('/sub/') else 20 if path.startswith('/api/auth/') else 180
        if len(bucket) >= limit:
            return JSONResponse({'detail': 'Слишком много запросов. Подождите минуту.'}, status_code=429,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        bucket.append(now)
        if len(hits) > 10000:
            for key in list(hits):
                if not hits[key] or hits[key][-1] < now - 60:
                    del hits[key]
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if path.startswith(('/api/', '/login','/sub/')):
        response.headers['Cache-Control'] = 'no-store'
    return response

class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)

class PasswordChange(BaseModel):
    model_config = {'extra': 'forbid'}
    current_password: str = Field(min_length=1, max_length=128, strict=True)
    new_password: str = Field(min_length=10, max_length=128, strict=True)

class TgAuth(BaseModel):
    init_data: str = Field(max_length=10000)

class Purchase(BaseModel):
    plan: str = Field(min_length=1, max_length=32, pattern=r'^[a-z][a-z0-9_-]*$')
    protocol: Literal['xui', 'wg']
    accept_terms: bool = False
    plan_version: int = Field(default=1, ge=1, strict=True)
    receipt_email: str | None = Field(default=None, max_length=254)
    promo_code: str | None = Field(default=None,min_length=1,max_length=32)
    promo_version: int | None = Field(default=None,ge=1,strict=True)
    quoted_amount: int | None = Field(default=None,ge=1,le=1000000,strict=True)
    quote_currency: Literal['RUB','XTR'] | None = None


class PlanFields(BaseModel):
    model_config = {'extra': 'forbid'}
    name: str = Field(min_length=1, max_length=80)
    days: int = Field(ge=1, le=3650, strict=True)
    price: int = Field(ge=1, le=1000000, strict=True)
    stars: int = Field(ge=1, le=100000, strict=True)
    enabled: bool = Field(strict=True)
    device_limit: int = Field(default=4,ge=0,le=20,strict=True)
    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('Укажите название тарифа без управляющих символов')
        return value


class PlanUpdate(PlanFields):
    device_limit: int | None = Field(default=None,ge=0,le=20,strict=True)
    version: int = Field(ge=1, strict=True)

class PlanCreate(PlanFields):
    id: str = Field(min_length=1, max_length=32, pattern=r'^[a-z][a-z0-9_-]*$')
    enabled: bool = Field(default=False, strict=True)


class ServerFields(BaseModel):
    model_config = {'extra':'forbid'}
    name: str = Field(min_length=1, max_length=80)
    location: str = Field(default='', max_length=80)
    capacity: int = Field(default=0, ge=0, le=1000000, strict=True)

    @field_validator('name', 'location')
    @classmethod
    def safe_label(cls, value, info):
        value = value.strip()
        if (info.field_name=='name' and not value) or any(ord(x)<32 for x in value):
            raise ValueError('Некорректное название или локация')
        return value

class ServerCreate(ServerFields):
    protocol: Literal['xui','wg']
    settings: dict

class ServerUpdate(ServerFields):
    state: Literal['active','draining','maintenance','disabled']
    version: int = Field(ge=1, strict=True)
    settings: dict | None = None


def email_address(raw):
    value = raw.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise HTTPException(422, 'Укажите корректный email')
    return value

@app.get('/health')
async def health():
    one('SELECT 1 AS ok')
    return {'status': 'ok'}

def html_page(name):
    html = (STATIC / name).read_text().replace('__BASE_PATH__', c.BASE_PATH)
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})

@app.get('/')
async def index():
    return html_page('index.html')

@app.get('/admin')
async def admin_page():
    return html_page('admin.html')

@app.get('/api/catalog')
async def catalog(request: Request):
    try:
        customer = current_user(request)
        tg = bool(customer['telegram_id'])
    except HTTPException:
        customer = None
        tg = False
    plans = [{**p, 'price': p['stars'] if tg else p['price']} for p in all_rows('SELECT id,name,days,price,stars,version,device_limit FROM plans WHERE enabled=1 ORDER BY position,id')]
    return {'plans': plans, 'currency': 'XTR' if tg else 'RUB',  'protocols': [{'id': 'xui', 'name': 'VLESS · 3x-ui', 'available': providers.available('xui', customer['id'] if customer else None)},
                                           {'id': 'wg', 'name': 'WireGuard', 'available': providers.available('wg', customer['id'] if customer else None)}],
            'trial':trials.offer(customer), 'payment_mode': 'stars' if tg else c.PAYMENT_MODE, 'bot_username': c.BOT_USERNAME,
            'support': os.getenv('SUPPORT_TEXT', 'Напишите администратору магазина.'),
            'receipts': not tg and os.getenv('YOOKASSA_RECEIPTS') == 'true',
            'terms': os.getenv('TERMS_TEXT', 'Условия продажи ещё не заполнены владельцем магазина.')}

@app.post('/api/auth/register')
async def register(data: Credentials):
    email = email_address(data.email)
    try:
        uid = execute('INSERT INTO users(email,password,created) VALUES (?,?,?)',
                      (email, await asyncio.to_thread(password_hash, data.password), int(time.time())))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Этот email уже зарегистрирован')
    response = JSONResponse({'ok': True})
    session(response, uid)
    return response

@app.post('/api/auth/login')
async def login(data: Credentials):
    user = one('SELECT * FROM users WHERE email=?', (email_address(data.email),))
    encoded = user['password'] if user else await asyncio.to_thread(password_hash, 'dummy-password')
    valid = await asyncio.to_thread(password_valid, data.password, encoded)
    if not user or not valid:
        raise HTTPException(401, 'Неверный email или пароль')
    response = JSONResponse({'ok': True})
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        fresh = conn.execute('SELECT password FROM users WHERE id=?', (user['id'],)).fetchone()
        if not fresh or fresh['password'] != encoded:
            raise HTTPException(401, 'Неверный email или пароль')
        session(response, user['id'], conn)
    return response

@app.post('/api/auth/password')
async def change_password(data: PasswordChange, request: Request, user=Depends(current_user)):
    if not user['email'] or not user['password']:
        raise HTTPException(409, 'Смена пароля доступна для аккаунта с email')
    if not await asyncio.to_thread(password_valid, data.current_password, user['password']):
        raise HTTPException(403, 'Текущий пароль неверен')
    if data.current_password == data.new_password:
        raise HTTPException(422, 'Новый пароль должен отличаться от текущего')
    encoded = await asyncio.to_thread(password_hash, data.new_password)
    response = JSONResponse({'ok': True})
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT 1 FROM sessions WHERE token=? AND user_id=? AND expires>?',
            (digest(request.cookies.get('session', '')), user['id'], int(time.time()))).fetchone()
        if not active:
            raise HTTPException(401, 'Войдите в личный кабинет')
        fresh = conn.execute('SELECT password FROM users WHERE id=?', (user['id'],)).fetchone()
        if not fresh or fresh['password'] != user['password']:
            raise HTTPException(409, 'Пароль уже изменён. Войдите заново.')
        conn.execute('UPDATE users SET password=? WHERE id=?', (encoded, user['id']))
        conn.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        conn.execute('DELETE FROM tickets WHERE user_id=?', (user['id'],))
        session(response, user['id'], conn)
    return response

@app.post('/api/auth/telegram')
async def tg_login(data: TgAuth):
    tg = telegram_user(data.init_data)
    user = service.telegram_account(tg['id'])
    response = JSONResponse({'ok': True})
    session(response, user['id'])
    return response

@app.get('/login')
async def ticket_login(ticket: str = ''):
    response = RedirectResponse(c.BASE_PATH + '/', status_code=303)
    with db() as conn:
        result = conn.execute('DELETE FROM tickets WHERE token=? AND expires>? RETURNING user_id',
                              (digest(ticket), int(time.time()))).fetchone()
        if not result:
            raise HTTPException(401, 'Ссылка истекла. Запросите новую командой /account в боте.')
        session(response, result['user_id'], conn)
    return response

@app.post('/api/auth/logout')
async def logout(request: Request):
    execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('session', '')),))
    response = JSONResponse({'ok': True})
    response.delete_cookie('session', path=c.COOKIE_PATH)
    return response

@app.get('/api/me')
async def me(user=Depends(current_user)):
    subs = all_rows('SELECT s.id,s.protocol,s.expires,s.state,s.device_limit,s.primary_key_state,s.access_source,(s.rotation_error IS NOT NULL) AS primary_retrying,coalesce(l.enabled,0) AS link_enabled,coalesce(l.follow_rotation,0) AS link_follows_rotation FROM subscriptions s LEFT JOIN subscription_links l ON l.subscription_id=s.id WHERE s.user_id=?', (user['id'],))
    orders = all_rows('SELECT id,protocol,plan,plan_name,days,amount,currency,method,status,created,payment_url,attempts,device_limit,kind,base_amount,discount,promo_code,promo_percent FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 50', (user['id'],))
    return {'id': user['id'], 'email': user['email'], 'telegram_id': user['telegram_id'], 'can_change_password': bool(user['email'] and user['password']), 'subscriptions': subs, 'orders': orders, 'referrals':referrals.summary(user)}

@app.post('/api/referrals/code')
async def referral_code(user=Depends(current_user)):
    async with service.lock:return referrals.code(user)

@app.post('/api/referrals/bind')
async def referral_bind(data: referrals.Bind,user=Depends(current_user)):
    async with service.lock:return referrals.bind(user,data)

@app.post('/api/referrals/redeem')
async def referral_redeem(data: referrals.Redeem,user=Depends(current_user)):
    async with service.lock:oid=referrals.redeem(user,data)
    return await order_status(oid,user)

@app.get('/api/admin/referrals',dependencies=[Depends(admin)])
async def referral_policy():return referrals.policy()

@app.put('/api/admin/referrals',dependencies=[Depends(admin)])
async def referral_policy_update(data: referrals.PolicyUpdate):
    async with service.lock:return referrals.update(data)

@app.post('/api/orders')
async def purchase(data: Purchase, user=Depends(current_user)):
    if not providers.available(data.protocol, user['id']):
        raise HTTPException(503, 'Этот способ подключения ещё не настроен')
    method = 'stars' if user['telegram_id'] else c.PAYMENT_MODE
    if method == 'stars' and (not c.BOT_TOKEN or not os.getenv('TERMS_TEXT')):
        raise HTTPException(503, 'Владелец ещё не настроил оплату и условия продажи в Telegram')
    if method == 'stars' and not data.accept_terms:
        raise HTTPException(422, 'Подтвердите согласие с условиями продажи')
    email = email_address(data.receipt_email) if data.receipt_email else user['email']
    if method == 'yookassa' and os.getenv('YOOKASSA_RECEIPTS') == 'true' and not email:
        raise HTTPException(422, 'Укажите email для чека')
    async with service.lock:
        order = promos.make_order(data,user,method,email)
        if method == 'stars' and order['status']=='pending' and not order['payment_url']:
            try:
                url = await providers.stars_invoice(order)
                execute('UPDATE orders SET payment_url=? WHERE id=?', (url, order['id']))
            except Exception as exc:
                logging.warning('Stars invoice order=%s error=%s', order['id'], type(exc).__name__)
                raise HTTPException(502, 'Не удалось создать счёт Telegram. Повторите запрос.')
        if method == 'yookassa' and order['status']=='pending' and not order['payment_id']:
            try:
                result = await providers.payment(order, order['receipt_email'])
                execute('UPDATE orders SET payment_id=?,payment_url=? WHERE id=?',
                        (result['id'], result.get('confirmation', {}).get('confirmation_url'), order['id']))
            except Exception as exc:
                logging.warning('Payment creation order=%s error=%s', order['id'], type(exc).__name__)
                raise HTTPException(502, 'Не удалось создать оплату. Повторите запрос: заказ сохранён.')
    return await order_status(order['id'], user)

@app.get('/api/orders/{order_id}')
async def order_status(order_id: str, user=Depends(current_user)):
    order = one('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user['id']))
    if not order:
        raise HTTPException(404, 'Заказ не найден')
    return {k: order[k] for k in ('id', 'protocol', 'plan_name', 'days', 'amount', 'currency', 'method', 'status', 'payment_url','device_limit','kind','base_amount','discount','promo_code','promo_percent')} | {
        'instructions': os.getenv('MANUAL_PAYMENT_TEXT', 'Свяжитесь с поддержкой и сообщите номер заказа.')
        if order['method'] == 'manual' else 'Пробный доступ без оплаты. Дождитесь выдачи в кабинете.' if order['kind']=='trial' else ''}

@app.post('/api/quote')
async def order_quote(data: promos.Quote,user=Depends(current_user)):
    if not providers.available(data.protocol,user['id']):raise HTTPException(503,'Этот способ подключения сейчас недоступен')
    return promos.quote(data,user)

@app.get('/api/admin/coupons',dependencies=[Depends(admin)])
async def coupons_list():return {'coupons':promos.listing()}

@app.post('/api/admin/coupons',dependencies=[Depends(admin)])
async def coupon_create(data: promos.CouponCreate):
    async with service.lock:row=promos.create(data)
    return JSONResponse(row,status_code=201)

@app.put('/api/admin/coupons/{cid}',dependencies=[Depends(admin)])
async def coupon_update(cid: str,data: promos.CouponUpdate):
    async with service.lock:return promos.update(cid,data)

@app.get('/api/admin/trial',dependencies=[Depends(admin)])
async def trial_policy():return trials.policy()

@app.put('/api/admin/trial',dependencies=[Depends(admin)])
async def trial_update(data: trials.PolicyUpdate):
    async with service.lock:return trials.update(data)

@app.post('/api/trial')
async def trial_claim(data: trials.Claim,user=Depends(current_user)):
    async with service.lock:oid=trials.claim(user,data)
    return await order_status(oid,user)

class DeviceCreate(BaseModel):
    model_config = {'extra':'forbid'}
    name: str = Field(min_length=1,max_length=60)
    request_id: UUID

    @field_validator('name')
    @classmethod
    def clean_name(cls,value):
        value=value.strip()
        if not value or any(ord(ch)<32 for ch in value):
            raise ValueError('Укажите название устройства')
        return value


def owned_subscription(sid,user):
    sub=one('SELECT * FROM subscriptions WHERE id=? AND user_id=?',(sid,user['id']))
    if not sub:
        raise HTTPException(404,'Подписка не найдена')
    return sub


def public_device(row,sub):
    return {k:row[k] for k in ('id','name','state','created','expires')} | {
        'protocol':sub['protocol'],'ready':row['state']=='active' and sub['state']=='active' and min(row['expires'],sub['expires'])>time.time(),
        'retrying':bool(row['error'])}

@app.get('/api/subscriptions/{sid}/devices')
async def device_list(sid: str,user=Depends(current_user)):
    sub=owned_subscription(sid,user)
    rows=all_rows("SELECT * FROM devices WHERE subscription_id=? AND state!='revoked' ORDER BY created,id",(sid,))
    return {'devices':[public_device(row,sub) for row in rows],'max_extra':sub['device_limit']}

@app.post('/api/subscriptions/{sid}/devices')
async def device_create(sid: str,data: DeviceCreate,user=Depends(current_user)):
    async with service.lock:
        sub=owned_subscription(sid,user)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM devices WHERE subscription_id=? AND request_id=?',(sid,str(data.request_id))).fetchone()
            if row:
                if row['name']!=data.name:
                    raise HTTPException(409,'Этот запрос уже использован для другого устройства')
                return public_device(dict(row),sub)
            if sub['state']!='active' or sub['expires']<=time.time() or not sub['server_id']:
                raise HTTPException(409,'Нужна действующая подписка с сервером')
            if conn.execute("SELECT count(*) FROM devices WHERE subscription_id=? AND state!='revoked'",(sid,)).fetchone()[0]>=sub['device_limit']:
                raise HTTPException(409,'Достигнут лимит дополнительных профилей')
            did=str(uuid.uuid4())
            conn.execute('INSERT INTO devices(id,subscription_id,request_id,name,created,credential_id) VALUES (?,?,?,?,?,?)',(did,sid,str(data.request_id),data.name,int(time.time()),c.encrypt(did)))
            row=dict(conn.execute('SELECT * FROM devices WHERE id=?',(did,)).fetchone())
        return JSONResponse(public_device(row,sub),status_code=201)

class RotationCreate(BaseModel):
    model_config = {'extra':'forbid'}
    request_id: UUID

@app.post('/api/subscriptions/{sid}/rotate')
async def rotate_primary(sid: str,data: RotationCreate,user=Depends(current_user)):
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            raw=conn.execute('SELECT * FROM subscriptions WHERE id=? AND user_id=?',(sid,user['id'])).fetchone()
            if not raw:raise HTTPException(404,'Подписка не найдена')
            sub=dict(raw)
            if sub['access_source']=='trial':raise HTTPException(409,'Перевыпуск станет доступен после покупки тарифа')
            previous=conn.execute('SELECT id,state FROM primary_rotations WHERE subscription_id=? AND request_id=?',(sid,str(data.request_id))).fetchone()
            if previous:return {'rotation':dict(previous),'subscription_id':sid}
            if sub['state']!='active' or sub['expires']<=time.time() or sub['primary_key_state']!='active':
                raise HTTPException(409,'Нужен действующий основной профиль')
            if conn.execute("SELECT 1 FROM primary_rotations WHERE subscription_id=? AND state NOT IN ('completed','canceled')",(sid,)).fetchone():
                raise HTTPException(409,'Основной ключ уже перевыпускается')
            job=str(uuid.uuid4())
            conn.execute('INSERT INTO primary_rotations(id,subscription_id,request_id,old_credential_id,new_credential_id,created) VALUES (?,?,?,?,?,?)',
                (job,sid,str(data.request_id),sub['credential_id'] or c.encrypt(sid),c.encrypt(str(uuid.uuid4())),int(time.time())))
            conn.execute("UPDATE subscriptions SET primary_key_state='rotating',secret=NULL,rotation_error=NULL WHERE id=?",(sid,))
            conn.execute('UPDATE subscription_links SET enabled=0,token_hash=NULL,token_secret=NULL,version=version+1 WHERE subscription_id=? AND enabled=1 AND follow_rotation=0',(sid,))
        return JSONResponse({'rotation':{'id':job,'state':'revoking'},'subscription_id':sid},status_code=201)

@app.post('/api/devices/{did}/rotate')
async def rotate_device(did: str,data: RotationCreate,user=Depends(current_user)):
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            raw=conn.execute('SELECT d.* FROM devices d JOIN subscriptions s ON s.id=d.subscription_id WHERE d.id=? AND s.user_id=?',(did,user['id'])).fetchone()
            if not raw:raise HTTPException(404,'Устройство не найдено')
            device=dict(raw)
            sub=dict(conn.execute('SELECT * FROM subscriptions WHERE id=?',(device['subscription_id'],)).fetchone())
            if sub['access_source']=='trial':raise HTTPException(409,'Перевыпуск станет доступен после покупки тарифа')
            previous=conn.execute('SELECT id,state FROM device_rotations WHERE device_id=? AND request_id=?',(did,str(data.request_id))).fetchone()
            if previous:return {'rotation':dict(previous),'device':public_device(device,sub)}
            if device['state']!='active' or sub['state']!='active' or min(device['expires'],sub['expires'])<=time.time():
                raise HTTPException(409,'Перевыпуск доступен для действующего профиля')
            active=conn.execute("SELECT 1 FROM device_rotations WHERE device_id=? AND state NOT IN ('completed','canceled')",(did,)).fetchone()
            if active:raise HTTPException(409,'Перевыпуск уже выполняется')
            job=str(uuid.uuid4());identity=str(uuid.uuid4())
            conn.execute('INSERT INTO device_rotations(id,device_id,request_id,old_credential_id,new_credential_id,created) VALUES (?,?,?,?,?,?)',
                (job,did,str(data.request_id),device['credential_id'] or c.encrypt(did),c.encrypt(identity),int(time.time())))
            conn.execute("UPDATE devices SET state='rotating',secret=NULL,error=NULL WHERE id=?",(did,))
            device.update(state='rotating',secret=None,error=None)
        return JSONResponse({'rotation':{'id':job,'state':'revoking'},'device':public_device(device,sub)},status_code=201)

@app.delete('/api/devices/{did}')
async def device_revoke(did: str,user=Depends(current_user)):
    async with service.lock:
        row=one('SELECT d.* FROM devices d JOIN subscriptions s ON s.id=d.subscription_id WHERE d.id=? AND s.user_id=?',(did,user['id']))
        if not row: raise HTTPException(404,'Устройство не найдено')
        with db() as conn:
            conn.execute("UPDATE devices SET state='revoking',secret=NULL,next_attempt=0,error=NULL WHERE id=? AND state NOT IN ('revoking','revoked')",(did,))
            conn.execute("UPDATE device_rotations SET state='canceling',next_attempt=0 WHERE device_id=? AND state NOT IN ('completed','canceled')",(did,))
    return {'ok':True}

@app.get('/api/devices/{did}/config')
async def device_config(did: str,user=Depends(current_user)):
    row=one('SELECT d.*,s.protocol,s.state AS parent_state,s.expires AS parent_expires FROM devices d JOIN subscriptions s ON s.id=d.subscription_id WHERE d.id=? AND s.user_id=?',(did,user['id']))
    if not row or row['state']!='active' or row['parent_state']!='active' or min(row['expires'],row['parent_expires'])<=time.time():
        raise HTTPException(404,'Действующий профиль не найден')
    return {'config':c.decrypt(row['secret']),'protocol':row['protocol']}

@app.get('/api/devices/{did}/download')
async def device_download(did: str,user=Depends(current_user)):
    result=await device_config(did,user)
    extension='conf' if result['protocol']=='wg' else 'txt'
    return Response(result['config'],media_type='text/plain',headers={'Content-Disposition':f'attachment; filename="pushok-device.{extension}"'})

@app.get('/api/devices/{did}/qr')
async def device_qr(did: str,user=Depends(current_user)):
    result=await device_config(did,user)
    buffer=io.BytesIO();qrcode.make(result['config']).save(buffer,format='PNG')
    return Response(buffer.getvalue(),media_type='image/png')

class LinkUpdate(BaseModel):
    model_config = {'extra':'forbid'}
    action: Literal['enable','rotate','disable']
    version: int = Field(ge=0,strict=True)
    follow_rotation: bool = Field(default=False,strict=True)


def link_metadata(row):
    if not row:return {'enabled':False,'follow_rotation':False,'version':0,'url':None}
    return {'enabled':bool(row['enabled']),'follow_rotation':bool(row['follow_rotation']),'version':row['version'],
            'url':c.PUBLIC_URL+'/sub/'+c.decrypt(row['token_secret']) if row['enabled'] else None}

@app.get('/api/subscriptions/{sid}/link')
async def subscription_link(sid: str,user=Depends(current_user)):
    sub=owned_subscription(sid,user)
    if sub['protocol']!='xui' or sub['access_source']=='trial':raise HTTPException(409,'Ссылки подписки доступны для оплаченного VLESS')
    return link_metadata(one('SELECT * FROM subscription_links WHERE subscription_id=?',(sid,)))

@app.put('/api/subscriptions/{sid}/link')
async def update_subscription_link(sid: str,data: LinkUpdate,user=Depends(current_user)):
    async with service.lock:
        sub=owned_subscription(sid,user)
        if sub['protocol']!='xui' or sub['access_source']=='trial':raise HTTPException(409,'Ссылки подписки доступны для оплаченного VLESS')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            raw=conn.execute('SELECT * FROM subscription_links WHERE subscription_id=?',(sid,)).fetchone()
            before=dict(raw) if raw else None
            if (before['version'] if before else 0)!=data.version:raise HTTPException(409,'Настройки ссылки изменились. Обновите окно.')
            if data.action=='enable' and (sub['state']!='active' or sub['expires']<=time.time() or sub['primary_key_state']!='active'):
                raise HTTPException(409,'Включение ссылки доступно для действующего основного профиля')
            if data.action=='rotate' and (not before or not before['enabled']):raise HTTPException(409,'Сначала включите ссылку')
            if data.action=='disable' and not before:return link_metadata(None)
            new_token=data.action=='rotate' or (data.action=='enable' and (not before or not before['enabled']))
            token=secrets.token_urlsafe(32) if new_token else None
            encrypted=c.encrypt(token) if new_token else (before['token_secret'] if before else None)
            hashed=digest(token) if new_token else (before['token_hash'] if before else None)
            enabled=data.action!='disable'
            if not enabled:encrypted=hashed=None
            conn.execute('INSERT INTO subscription_links(subscription_id,token_hash,token_secret,enabled,follow_rotation,version,created) VALUES (?,?,?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET token_hash=excluded.token_hash,token_secret=excluded.token_secret,enabled=excluded.enabled,follow_rotation=excluded.follow_rotation,version=excluded.version',
                (sid,hashed,encrypted,int(enabled),int(data.follow_rotation),data.version+1,int(time.time())))
            result=dict(conn.execute('SELECT * FROM subscription_links WHERE subscription_id=?',(sid,)).fetchone())
        return link_metadata(result)

@app.get('/sub/{token}')
async def export_subscription(token: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):raise HTTPException(404,'Подписка недоступна')
    row=one('SELECT s.* FROM subscription_links l JOIN subscriptions s ON s.id=l.subscription_id WHERE l.token_hash=? AND l.enabled=1',(digest(token),))
    if not row or row['protocol']!='xui' or row['access_source']=='trial' or row['state']!='active' or row['primary_key_state']!='active' or row['expires']<=time.time() or not row['secret']:
        raise HTTPException(404,'Подписка недоступна')
    try:config=c.decrypt(row['secret'])
    except Exception:raise HTTPException(503,'Профиль временно недоступен') from None
    if not config.startswith('vless://') or len(config)>16384 or any(ord(ch)<33 for ch in config):
        raise HTTPException(503,'Профиль временно недоступен')
    body=base64.b64encode((config+'\n').encode()).decode()
    return Response(body,media_type='text/plain',headers={'Subscription-Userinfo':'expire='+str(row['expires']),'Profile-Update-Interval':'12',
        'Content-Disposition':'attachment; filename="pushok-subscription.txt"','X-Robots-Tag':'noindex, nofollow, noarchive'})

@app.get('/api/subscriptions/{sid}/config')
async def config_file(sid: str, user=Depends(current_user)):
    sub = one('SELECT * FROM subscriptions WHERE id=? AND user_id=?', (sid, user['id']))
    if not sub or sub['state'] != 'active' or sub['expires'] <= time.time() or sub['primary_key_state']!='active':
        raise HTTPException(404, 'Активная подписка не найдена')
    return {'config': c.decrypt(sub['secret']), 'protocol': sub['protocol']}

@app.get('/api/subscriptions/{sid}/download')
async def download(sid: str, user=Depends(current_user)):
    result = await config_file(sid, user)
    extension = 'conf' if result['protocol'] == 'wg' else 'txt'
    return Response(result['config'], media_type='text/plain', headers={
        'Content-Disposition': f'attachment; filename="pushok.{extension}"'})

@app.get('/api/subscriptions/{sid}/qr')
async def qr(sid: str, user=Depends(current_user)):
    result = await config_file(sid, user)
    img = qrcode.make(result['config'])
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return Response(buffer.getvalue(), media_type='image/png')


def admin_plan(row):
    return {**row, 'enabled': bool(row['enabled'])}


@app.get('/api/admin/servers', dependencies=[Depends(admin)])
async def admin_servers():
    return {'servers':servers.list_servers(), 'unassigned_subscriptions':one('SELECT count(*) AS n FROM subscriptions WHERE server_id IS NULL')['n']}

@app.post('/api/admin/servers', dependencies=[Depends(admin)])
async def create_server(data: ServerCreate):
    async with service.lock:
        try:
            result = servers.create(data)
        except (ValidationError, ValueError):
            raise HTTPException(422,'Проверьте профиль подключения: адрес, секреты, inbound и шаблон VLESS.') from None
    return JSONResponse(result, status_code=201)

@app.put('/api/admin/servers/{sid}', dependencies=[Depends(admin)])
async def update_server(sid: str, data: ServerUpdate):
    async with service.lock:
        try:
            return servers.update(sid,data)
        except (ValidationError, ValueError):
            raise HTTPException(422,'Проверьте профиль подключения. Секреты в ответе не возвращаются.') from None

@app.post('/api/admin/servers/{sid}/check', dependencies=[Depends(admin)])
async def check_server(sid: str):
    row = one('SELECT * FROM servers WHERE id=?',(sid,))
    if not row:
        raise HTTPException(404,'Сервер не найден')
    try:
        await providers.probe_server(row)
        health, error = 'ok', None
    except Exception:
        health, error = 'error', 'ProviderCheckFailed'
    with db() as conn:
        changed = conn.execute('UPDATE servers SET health=?,checked_at=?,error_code=? WHERE id=? AND version=?',
            (health,int(time.time()),error,sid,row['version'])).rowcount
    if not changed:
        raise HTTPException(409,'Настройки изменились во время проверки. Запустите проверку заново.')
    return next(item for item in servers.list_servers() if item['id']==sid)

@app.get('/api/admin/plans', dependencies=[Depends(admin)])
async def admin_plans():
    return {'plans': [admin_plan(row) for row in all_rows('SELECT * FROM plans ORDER BY position,id')]}


@app.post('/api/admin/plans', dependencies=[Depends(admin)])
async def create_plan(data: PlanCreate):
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM plans WHERE id=?', (data.id,)).fetchone():
                raise HTTPException(409, 'Тариф с таким кодом уже существует. Обновите список тарифов.')
            position = conn.execute('SELECT coalesce(max(position),-1)+1 FROM plans').fetchone()[0]
            conn.execute('INSERT INTO plans(id,name,days,price,stars,enabled,position,updated,device_limit) VALUES (?,?,?,?,?,?,?,?,?)',
                         (data.id, data.name, data.days, data.price, data.stars, int(data.enabled), position, int(time.time()),data.device_limit))
            saved = dict(conn.execute('SELECT * FROM plans WHERE id=?', (data.id,)).fetchone())
            conn.execute('INSERT INTO plan_changes(plan_id,created,before_data,after_data) VALUES (?,?,?,?)',
                         (data.id, saved['updated'], 'null', json.dumps(saved, ensure_ascii=False)))
    return JSONResponse(admin_plan(saved), status_code=201)

@app.put('/api/admin/plans/{plan_id}', dependencies=[Depends(admin)])
async def update_plan(plan_id: str, data: PlanUpdate):
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Тариф не найден')
            before = dict(row)
            if before['version'] != data.version:
                raise HTTPException(409, 'Тариф уже изменён. Обновите страницу перед сохранением.')
            if all(before[key] == value for key, value in data.model_dump(exclude={'version'},exclude_none=True).items()):
                return admin_plan(before)
            conn.execute('UPDATE plans SET name=?,days=?,price=?,stars=?,enabled=?,device_limit=?,version=version+1,updated=? WHERE id=?',
                         (data.name, data.days, data.price, data.stars, int(data.enabled),before['device_limit'] if data.device_limit is None else data.device_limit,int(time.time()),plan_id))
            after = dict(conn.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone())
            conn.execute('INSERT INTO plan_changes(plan_id,created,before_data,after_data) VALUES (?,?,?,?)',
                         (plan_id, after['updated'], json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
    return admin_plan(after)

@app.get('/api/admin/diagnostics',dependencies=[Depends(admin)])
async def diagnostic_report():
    try:return diagnostics.report(dict(service.worker_health))
    except sqlite3.Error:raise HTTPException(503,'Не удалось прочитать локальное состояние магазина') from None

@app.get('/api/admin/overview', dependencies=[Depends(admin)])
async def overview():
    return {'orders': all_rows('SELECT o.*,u.email,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created DESC LIMIT 200'),
            'users': one('SELECT count(*) AS count FROM users')['count'],
            'active': one("SELECT count(*) AS count FROM subscriptions WHERE state='active' AND expires>?", (int(time.time()),))['count'],
            'revenue': one("SELECT coalesce(sum(amount),0) AS amount FROM orders WHERE status IN ('paid','active') AND currency='RUB'")['amount'],
            'stars': one("SELECT coalesce(sum(amount),0) AS amount FROM orders WHERE status IN ('paid','active') AND currency='XTR'")['amount']}


@app.get('/api/admin/users', dependencies=[Depends(admin)])
async def admin_users(q: str = Query(default='', max_length=100),
                      page: int = Query(default=1, ge=1, le=1000000),
                      page_size: int = Query(default=25, ge=1, le=100)):
    query = q.strip()
    where, args = '', []
    if query:
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where = "WHERE (u.email LIKE ? ESCAPE '\\' OR CAST(u.id AS TEXT)=? OR CAST(u.telegram_id AS TEXT)=?)"
        args = ['%' + escaped + '%', query, query]
    with db() as conn:
        conn.execute('BEGIN')
        total = conn.execute('SELECT count(*) FROM users u ' + where, args).fetchone()[0]
        rows = conn.execute("""SELECT u.id,u.email,u.telegram_id,u.created,
            (SELECT count(*) FROM subscriptions s WHERE s.user_id=u.id AND s.state='active' AND s.expires>?) AS active_subscriptions,
            (SELECT count(*) FROM orders o WHERE o.user_id=u.id) AS orders_count
            FROM users u """ + where + ' ORDER BY u.created DESC,u.id DESC LIMIT ? OFFSET ?',
            [int(time.time()), *args, page_size, (page-1)*page_size]).fetchall()
    return {'users': [dict(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}

@app.get('/api/admin/users/{user_id}', dependencies=[Depends(admin)])
async def admin_user(user_id: int = APIPath(ge=1, le=9223372036854775807),
                     orders_page: int = Query(default=1, ge=1, le=1000000),
                     orders_page_size: int = Query(default=20, ge=1, le=100)):
    with db() as conn:
        conn.execute('BEGIN')
        row = conn.execute('SELECT id,email,telegram_id,created FROM users WHERE id=?', (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Пользователь не найден')
        subscriptions = [dict(sub) for sub in conn.execute(
            'SELECT id,protocol,expires,state,device_limit FROM subscriptions WHERE user_id=? ORDER BY protocol,id', (user_id,))]
        total = conn.execute('SELECT count(*) FROM orders WHERE user_id=?', (user_id,)).fetchone()[0]
        orders = [dict(order) for order in conn.execute("""SELECT id,protocol,plan,plan_name,days,amount,
            currency,method,status,created,attempts,error,promo_code,promo_percent,base_amount,discount FROM orders WHERE user_id=?
            ORDER BY created DESC,id DESC LIMIT ? OFFSET ?""", (user_id, orders_page_size, (orders_page-1)*orders_page_size))]
    now = int(time.time())
    for sub in subscriptions:
        sub['access_active'] = sub['state'] == 'active' and sub['expires'] > now
    return {'user': dict(row), 'subscriptions': subscriptions, 'orders': orders,
            'orders_total': total, 'orders_page': orders_page, 'orders_page_size': orders_page_size}


@app.get('/api/admin/orders', dependencies=[Depends(admin)])
async def admin_orders(q: str = Query(default='', max_length=100),
                       status: Literal['', 'pending', 'checkout', 'paid', 'active', 'canceled'] = '',
                       method: Literal['', 'manual', 'yookassa', 'stars','trial','referral'] = '',
                       protocol: Literal['', 'xui', 'wg'] = '',
                       page: int = Query(default=1, ge=1, le=1000000),
                       page_size: int = Query(default=25, ge=1, le=100)):
    conditions, args = [], []
    query = q.strip()
    if query:
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append("(o.id LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\' OR CAST(u.telegram_id AS TEXT)=? OR CAST(u.id AS TEXT)=?)")
        args.extend(['%' + escaped + '%', '%' + escaped + '%', query, query])
    for field, value in (('status', status), ('method', method), ('protocol', protocol)):
        if value:
            conditions.append('o.' + field + '=?')
            args.append(value)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    with db() as conn:
        conn.execute('BEGIN')
        total = conn.execute('SELECT count(*) FROM orders o JOIN users u ON u.id=o.user_id' + where, args).fetchone()[0]
        rows = conn.execute("""SELECT o.id,o.user_id,o.protocol,o.plan,o.plan_name,o.days,o.amount,
            o.currency,o.method,o.status,o.created,o.attempts,o.error,o.promo_code,o.promo_percent,o.base_amount,o.discount,u.email,u.telegram_id,
            (o.status='pending' AND o.method='manual' AND o.payment_id IS NULL) AS can_confirm,
            (o.status='paid') AS can_retry
            FROM orders o JOIN users u ON u.id=o.user_id""" + where +
            ' ORDER BY o.created DESC,o.id DESC LIMIT ? OFFSET ?', [*args, page_size, (page-1)*page_size]).fetchall()
    orders = []
    for row in rows:
        item = dict(row)
        item['can_confirm'] = bool(item['can_confirm'])
        item['can_reject'] = item['can_confirm']
        item['can_retry'] = bool(item['can_retry'])
        orders.append(item)
    return {'orders': orders, 'total': total, 'page': page, 'page_size': page_size}

@app.post('/api/admin/orders/{oid}/confirm', dependencies=[Depends(admin)])
async def confirm(oid: str):
    async with service.lock:
        order = one('SELECT * FROM orders WHERE id=?', (oid,))
        if not order:
            raise HTTPException(404, 'Заказ не найден')
        if order['payment_id'] or order['method'] != 'manual':
            raise HTTPException(409, 'Платёж проверяется через ЮKassa')
        if order['status'] == 'canceled':
            raise HTTPException(409, 'Заказ отменён')
        with db() as conn:
            changed = conn.execute("UPDATE orders SET status='paid' WHERE id=? AND status='pending'", (oid,)).rowcount
            if changed:
                referrals.award_locked(conn,oid)
                conn.execute('INSERT INTO audit(created,action,order_id) VALUES (?,?,?)',
                             (int(time.time()), 'manual_payment_confirmed', oid))
    return {'ok': True}

@app.post('/api/admin/orders/{oid}/reject', dependencies=[Depends(admin)])
async def reject(oid: str):
    async with service.lock:
        order = one('SELECT * FROM orders WHERE id=?', (oid,))
        if not order or order['status'] != 'pending' or order['method'] != 'manual' or order['payment_id']:
            raise HTTPException(409, 'Можно отменить только неоплаченный ручной заказ')
        execute("UPDATE orders SET status='canceled' WHERE id=?", (oid,))
        execute('INSERT INTO audit(created,action,order_id) VALUES (?,?,?)', (int(time.time()), 'manual_order_canceled', oid))
    return {'ok': True}

@app.post('/api/admin/orders/{oid}/retry', dependencies=[Depends(admin)])
async def retry(oid: str):
    execute("UPDATE orders SET next_attempt=0 WHERE id=? AND status='paid'", (oid,))
    return {'ok': True}

app.mount('/static', StaticFiles(directory=STATIC), name='static')


# Preserve the external prefix: reverse proxies must forward /shop unchanged.
shop_app = app

def create_app(base_path):
    if not base_path:
        return shop_app
    return Starlette(routes=[Mount(base_path, app=shop_app)], lifespan=lifespan)

app = create_app(c.BASE_PATH)
