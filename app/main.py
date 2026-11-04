import asyncio
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
from urllib.parse import urlsplit
import qrcode
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.routing import Mount
from . import config as c, providers, service
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
    tasks = []
    if os.getenv('BACKGROUND_TASKS', 'true') == 'true':
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
# Bounded per-process rate limits; run one process as required by SQLite worker design.
hits = defaultdict(deque)

@app.middleware('http')
async def safety(request, call_next):
    path = request.url.path.removeprefix(c.BASE_PATH)
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        origin = request.headers.get('origin')
        if origin and origin != urlsplit(c.PUBLIC_URL)._replace(path='', query='', fragment='').geturl():
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
        if int(request.headers.get('content-length', '0') or 0) > 16384:
            return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
    if path.startswith('/api/'):
        ip = request.client.host if request.client else 'unknown'
        bucket = hits[(ip, 'auth' if path.startswith('/api/auth/') else 'api')]
        now = time.monotonic()
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        limit = 20 if path.startswith('/api/auth/') else 180
        if len(bucket) >= limit:
            return JSONResponse({'detail': 'Слишком много запросов. Подождите минуту.'}, status_code=429)
        bucket.append(now)
        if len(hits) > 10000:
            for key in list(hits):
                if not hits[key] or hits[key][-1] < now - 60:
                    del hits[key]
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if path.startswith(('/api/', '/login')):
        response.headers['Cache-Control'] = 'no-store'
    return response

class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)

class TgAuth(BaseModel):
    init_data: str = Field(max_length=10000)

class Purchase(BaseModel):
    plan: Literal['month', 'quarter', 'year']
    protocol: Literal['xui', 'wg']
    accept_terms: bool = False
    receipt_email: str | None = Field(default=None, max_length=254)


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
        tg = bool(current_user(request)['telegram_id'])
    except HTTPException:
        tg = False
    plans = [{**p, 'price': p['stars'] if tg else p['price']} for p in c.PLANS]
    return {'plans': plans, 'currency': 'XTR' if tg else 'RUB',  'protocols': [{'id': 'xui', 'name': 'VLESS · 3x-ui', 'available': providers.available('xui')},
                                           {'id': 'wg', 'name': 'WireGuard', 'available': providers.available('wg')}],
            'payment_mode': 'stars' if tg else c.PAYMENT_MODE, 'bot_username': c.BOT_USERNAME,
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
    session(response, user['id'])
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
    with db() as conn:
        result = conn.execute('DELETE FROM tickets WHERE token=? AND expires>? RETURNING user_id',
                              (digest(ticket), int(time.time()))).fetchone()
    if not result:
        raise HTTPException(401, 'Ссылка истекла. Запросите новую командой /account в боте.')
    response = RedirectResponse(c.BASE_PATH + '/', status_code=303)
    session(response, result['user_id'])
    return response

@app.post('/api/auth/logout')
async def logout(request: Request):
    execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('session', '')),))
    response = JSONResponse({'ok': True})
    response.delete_cookie('session', path=c.COOKIE_PATH)
    return response

@app.get('/api/me')
async def me(user=Depends(current_user)):
    subs = all_rows('SELECT id,protocol,expires,state FROM subscriptions WHERE user_id=?', (user['id'],))
    orders = all_rows('SELECT id,protocol,plan,amount,currency,method,status,created,payment_url,attempts FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 50', (user['id'],))
    return {'id': user['id'], 'email': user['email'], 'telegram_id': user['telegram_id'], 'subscriptions': subs, 'orders': orders}

@app.post('/api/orders')
async def purchase(data: Purchase, user=Depends(current_user)):
    if not providers.available(data.protocol):
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
        plan = next(p for p in c.PLANS if p['id'] == data.plan)
        order = one("SELECT * FROM orders WHERE user_id=? AND protocol=? AND plan=? AND method=? AND status='pending' AND created>? ORDER BY created DESC LIMIT 1",
                    (user['id'], data.protocol, data.plan, method, int(time.time()) - 1800))
        if not order:
            oid = str(uuid.uuid4())
            execute('INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,receipt_email,currency,method) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (oid, user['id'], data.protocol, plan['id'], plan['days'], plan['stars'] if method == 'stars' else plan['price'], int(time.time()), email, 'XTR' if method == 'stars' else 'RUB', method))
            order = one('SELECT * FROM orders WHERE id=?', (oid,))
        if method == 'stars' and not order['payment_url']:
            try:
                url = await providers.stars_invoice(order)
                execute('UPDATE orders SET payment_url=? WHERE id=?', (url, order['id']))
            except Exception as exc:
                logging.warning('Stars invoice order=%s error=%s', order['id'], type(exc).__name__)
                raise HTTPException(502, 'Не удалось создать счёт Telegram. Повторите запрос.')
        if method == 'yookassa' and not order['payment_id']:
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
    return {k: order[k] for k in ('id', 'protocol', 'amount', 'currency', 'method', 'status', 'payment_url')} | {
        'instructions': os.getenv('MANUAL_PAYMENT_TEXT', 'Свяжитесь с поддержкой и сообщите номер заказа.')
        if order['method'] == 'manual' else ''}

@app.get('/api/subscriptions/{sid}/config')
async def config_file(sid: str, user=Depends(current_user)):
    sub = one('SELECT * FROM subscriptions WHERE id=? AND user_id=?', (sid, user['id']))
    if not sub or sub['state'] != 'active' or sub['expires'] <= time.time():
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

@app.get('/api/admin/overview', dependencies=[Depends(admin)])
async def overview():
    return {'orders': all_rows('SELECT o.*,u.email,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created DESC LIMIT 200'),
            'users': one('SELECT count(*) AS count FROM users')['count'],
            'active': one("SELECT count(*) AS count FROM subscriptions WHERE state='active' AND expires>?", (int(time.time()),))['count'],
            'revenue': one("SELECT coalesce(sum(amount),0) AS amount FROM orders WHERE status IN ('paid','active') AND currency='RUB'")['amount'],
            'stars': one("SELECT coalesce(sum(amount),0) AS amount FROM orders WHERE status IN ('paid','active') AND currency='XTR'")['amount']}

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
