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
from urllib.parse import urlsplit, quote
import qrcode
from fastapi import FastAPI, Depends, HTTPException, Request, Query, Path as APIPath, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field, field_validator, ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount
from . import ads_bookings as bookings, ads_finance as finance
from . import support as support_cases
from . import shop_settings, platforms, bot_menu as menu_editor
from . import config as c, providers, service, servers, trials, promos, referrals, diagnostics, order_progress, ads, ads_placements as placements
from .db import init_db, db, one, execute, all_rows
from .security import current_user, admin, session, password_hash, password_valid, digest, telegram_user
from . import staff, mail, partner, commerce_analytics

logging.basicConfig(level=logging.INFO)
# Telegram credentials are embedded in its API URL: never log HTTP request URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
STATIC = Path(__file__).parent / 'static'

@asynccontextmanager
async def lifespan(app):
    init_db()
    c.cipher()  # Fail before accepting purchases if encryption is not configured.
    mail.validate_configuration()
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
    if request.url.path.removeprefix(c.BASE_PATH).startswith('/api/partner/'):
        return JSONResponse({'detail':'Проверьте подпись и данные события партнёра.'},status_code=422)
    if request.url.path.removeprefix(c.BASE_PATH).startswith('/api/admin/servers'):
        return JSONResponse({'detail':'Проверьте поля сервера и профиль подключения.'},status_code=422)
    if request.url.path.removeprefix(c.BASE_PATH).startswith(('/api/ads/','/api/admin/ads/')):
        return JSONResponse({'detail':'Проверьте поля рекламной заявки или настроек.'},status_code=422)
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
        limit = 60 if path.startswith('/sub/') else 20 if path.startswith(('/api/auth/','/api/admin/auth/')) else 180
        if len(bucket) >= limit:
            return JSONResponse({'detail': 'Слишком много запросов. Подождите минуту.'}, status_code=429,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        bucket.append(now)
        if len(hits) > 10000:
            for key in list(hits):
                if not hits[key] or hits[key][-1] < now - 60:
                    del hits[key]
    response = await call_next(request)
    actor = getattr(request.state, 'admin_actor', None)
    if actor and path.startswith('/api/admin/') and not path.startswith('/api/admin/auth/'):
        try: staff.audit(actor, request.method, path, response.status_code)
        except sqlite3.Error: logging.exception('Не удалось записать событие аудита администратора')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if path.startswith(('/api/', '/login','/sub/')):
        response.headers['Cache-Control'] = 'no-store'
    return response

class Credentials(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)

class PasswordResetRequest(BaseModel):
    model_config = {'extra':'forbid'}
    email: str = Field(min_length=5,max_length=254)

class PasswordResetConfirm(BaseModel):
    model_config = {'extra':'forbid'}
    token: str = Field(min_length=32,max_length=128)
    new_password: str = Field(min_length=10,max_length=128)

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

@app.get('/api/platforms')
async def platform_catalog():
    """Read-only code/config inventory, not live platform verification."""
    return platforms.public_catalog()

def html_page(name):
    html = (STATIC / name).read_text().replace('__BASE_PATH__', c.BASE_PATH)
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})

@app.get('/')
async def index():
    return html_page('index.html')

@app.get('/admin')
async def admin_page():
    return html_page('admin.html')

class StaffLogin(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)
class StaffCreate(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=128)
    role: Literal['panel_operator','mtproxy_manager','finance','support']
class StaffUpdate(BaseModel):
    enabled: bool | None = None
    role: Literal['owner','panel_operator','mtproxy_manager','finance','support'] | None = None
@app.post('/api/admin/auth/login')
async def staff_login(data: StaffLogin, response: Response):
    return staff.login(data.email, data.password, response)
@app.post('/api/admin/auth/bootstrap')
async def staff_bootstrap(data: StaffLogin, request: Request, response: Response):
    result=staff.bootstrap(data.email,data.password,request.headers.get('authorization',''))
    return {**result,**staff.login(data.email,data.password,response)}
@app.get('/api/admin/auth/me')
async def staff_me(request: Request):
    actor=staff.authorize(request)
    return {'id':actor.get('id'),'email':actor['email'],'role':actor['role'],'role_label':staff.ROLE_LABELS[actor['role']]}
@app.post('/api/admin/auth/logout')
async def staff_logout(request: Request, response: Response):
    raw=request.cookies.get('staff_session','')
    if raw:
        with db() as conn: conn.execute('DELETE FROM staff_sessions WHERE token_hash=?',(digest(raw),))
    response.delete_cookie('staff_session',path=c.COOKIE_PATH)
    return {'ok':True}
@app.get('/api/admin/shop-settings',dependencies=[Depends(admin)])
async def shop_settings_get(request: Request):
    if request.state.admin_actor['role']!='owner':
        raise HTTPException(403,'Настройки магазина доступны только владельцу')
    return shop_settings.get()

@app.put('/api/admin/shop-settings',dependencies=[Depends(admin)])
async def shop_settings_update(data: shop_settings.SettingsUpdate, request: Request):
    if request.state.admin_actor['role']!='owner':
        raise HTTPException(403,'Изменять настройки магазина может только владелец')
    return shop_settings.update(data)

@app.get('/api/admin/bot-menu',dependencies=[Depends(admin)])
async def bot_menu_get(request: Request):
    if request.state.admin_actor['role'] != 'owner':
        raise HTTPException(403, 'Редактор Telegram-меню доступен только владельцу')
    return menu_editor.get()

@app.put('/api/admin/bot-menu',dependencies=[Depends(admin)])
async def bot_menu_put(data: menu_editor.MenuUpdate, request: Request):
    if request.state.admin_actor['role'] != 'owner':
        raise HTTPException(403, 'Изменять Telegram-меню может только владелец')
    return menu_editor.update(data)

@app.get('/api/admin/staff',dependencies=[Depends(admin)])
async def staff_list():
    return staff.list_staff()
@app.post('/api/admin/staff',dependencies=[Depends(admin)])
async def staff_create(data: StaffCreate, request: Request):
    actor=request.state.admin_actor
    if actor['role']!='owner': raise HTTPException(403,'Управлять сотрудниками может только владелец')
    return JSONResponse(staff.create_staff(data.email,data.password,data.role,actor.get('id')),status_code=201)
@app.patch('/api/admin/staff/{staff_id}',dependencies=[Depends(admin)])
async def staff_update(staff_id: int, data: StaffUpdate, request: Request):
    actor=request.state.admin_actor
    if actor['role']!='owner': raise HTTPException(403,'Управлять сотрудниками может только владелец')
    return staff.update_staff(staff_id,data.enabled,data.role,actor.get('id'))
@app.get('/api/admin/audit',dependencies=[Depends(admin)])
async def admin_audit(limit: int = Query(default=200,ge=1,le=500)):
    return staff.audit_rows(limit)

@app.get('/api/catalog')
async def catalog(request: Request):
    try:
        customer = current_user(request)
        tg = bool(customer['telegram_id'])
    except HTTPException:
        customer = None
        tg = False
    plans = [{**p, 'price': p['stars'] if tg else p['price']} for p in all_rows('SELECT id,name,days,price,stars,version,device_limit FROM plans WHERE enabled=1 ORDER BY position,id')]
    public_settings=shop_settings.public()
    return {'plans': plans, 'currency': 'XTR' if tg else 'RUB',  'protocols': [{'id': 'xui', 'name': 'VLESS · 3x-ui', 'available': providers.available('xui', customer['id'] if customer else None)},
                                           {'id': 'wg', 'name': 'WireGuard', 'available': providers.available('wg', customer['id'] if customer else None)}],
            'trial':trials.offer(customer), 'payment_mode': 'stars' if tg else c.PAYMENT_MODE, 'bot_username': c.BOT_USERNAME,
            'support': public_settings['support'],
            'receipts': not tg and os.getenv('YOOKASSA_RECEIPTS') == 'true',
            'terms': public_settings['terms']}

@app.get('/api/support/tickets')
async def support_user_list(page:int=Query(default=1,ge=1,le=100000),page_size:int=Query(default=20,ge=1,le=50),user=Depends(current_user)):
    return support_cases.list_user(user['id'],page,page_size)

@app.post('/api/support/tickets',status_code=201)
async def support_user_create(data:support_cases.TicketCreate,user=Depends(current_user)):
    return support_cases.create(user['id'],data)

@app.get('/api/support/tickets/{case_id}')
async def support_user_get(case_id:UUID,user=Depends(current_user)):
    return support_cases.get_user(str(case_id),user['id'])

@app.post('/api/support/tickets/{case_id}/messages')
async def support_user_reply(case_id:UUID,data:support_cases.UserReply,user=Depends(current_user)):
    return support_cases.reply_user(str(case_id),user['id'],data)

@app.get('/api/admin/support/tickets',dependencies=[Depends(admin)])
async def support_staff_list(status:Literal['open','waiting_staff','waiting_customer','closed']|None=None,page:int=Query(default=1,ge=1,le=100000),page_size:int=Query(default=25,ge=1,le=100)):
    return support_cases.list_staff(status,page,page_size)

@app.get('/api/admin/support/tickets/{case_id}',dependencies=[Depends(admin)])
async def support_staff_get(case_id:UUID):
    return support_cases.get_staff(str(case_id))

@app.post('/api/admin/support/tickets/{case_id}/messages',dependencies=[Depends(admin)])
async def support_staff_reply(case_id:UUID,data:support_cases.StaffReply):
    return support_cases.reply_staff(str(case_id),data)

@app.patch('/api/admin/support/tickets/{case_id}',dependencies=[Depends(admin)])
async def support_staff_status(case_id:UUID,data:support_cases.TicketStatus):
    return support_cases.set_status(str(case_id),data)

@app.get('/api/ads/policy')
async def ads_policy():return ads.policy()

@app.get('/api/ads/requests')
async def ads_requests(page:int=Query(default=1,ge=1,le=100000),user=Depends(current_user)):
    return ads.listing(user['id'],page=page)

@app.post('/api/ads/requests')
async def ads_create(data:ads.RequestCreate,user=Depends(current_user)):
    return ads.create(user,data)

@app.get('/api/ads/requests/{rid}')
async def ads_get(rid:UUID,user=Depends(current_user)):
    return ads.get(str(rid),user['id'])

@app.post('/api/ads/requests/{rid}/cancel')
async def ads_cancel(rid:UUID,data:ads.Cancel,user=Depends(current_user)):
    return ads.transition(str(rid),data,user['id'])

@app.get('/api/admin/ads/policy',dependencies=[Depends(admin)])
async def admin_ads_policy():return ads.policy()

@app.put('/api/admin/ads/policy',dependencies=[Depends(admin)])
async def admin_ads_policy_update(data:ads.Policy):return ads.update_policy(data)

@app.get('/api/admin/ads/requests',dependencies=[Depends(admin)])
async def admin_ads_requests(page:int=Query(default=1,ge=1,le=100000),state:Literal['pending','approved','rejected','canceled']|None=None):
    return ads.listing(state=state,page=page)

@app.put('/api/admin/ads/requests/{rid}',dependencies=[Depends(admin)])
async def admin_ads_decision(rid:UUID,data:ads.Decision):return ads.transition(str(rid),data)

@app.post('/api/admin/ads/requests/{rid}/payments/{action}',dependencies=[Depends(admin)])
async def admin_ads_payment(rid:UUID,action:Literal['receipt','refund'],data:ads.PaymentCommand):
    return ads.record_payment(str(rid),action,data)

@app.get('/api/admin/ads/requests/{rid}/payments',dependencies=[Depends(admin)])
async def admin_ads_payment_history(rid:UUID):
    ads.get(str(rid))
    with ads.db() as conn:return ads.payment(conn,str(rid),private=True)

@app.get('/api/admin/ads/finance',dependencies=[Depends(admin)])
async def admin_ads_finance(start:int|None=Query(default=None,ge=0,le=finance.MAX_TIME),end:int|None=Query(default=None,ge=1,le=finance.MAX_TIME),page:int=Query(default=1,ge=1,le=100000)):
    return finance.report(start,end,page)

@app.post('/api/admin/ads/expenses',dependencies=[Depends(admin)])
async def admin_ads_expense(data:finance.Expense):return finance.change('create',data)

@app.post('/api/admin/ads/expenses/{eid}/void',dependencies=[Depends(admin)])
async def admin_ads_expense_void(eid:UUID,data:finance.Void):return finance.change('void',data,str(eid))

@app.get('/api/admin/ads/bookings',dependencies=[Depends(admin)])
async def admin_ads_bookings(view:Literal['reserved','expired','all']='reserved',page:int=Query(default=1,ge=1,le=100000)):
    return bookings.listing(view,page)

@app.post('/api/admin/ads/requests/{rid}/booking/reserve',dependencies=[Depends(admin)])
async def admin_ads_reserve(rid:UUID,data:bookings.Reserve):return bookings.change(str(rid),'reserve',data)

@app.post('/api/admin/ads/requests/{rid}/booking/reschedule',dependencies=[Depends(admin)])
async def admin_ads_reschedule(rid:UUID,data:bookings.Reserve):return bookings.change(str(rid),'reschedule',data)

@app.post('/api/admin/ads/requests/{rid}/booking/cancel',dependencies=[Depends(admin)])
async def admin_ads_cancel_booking(rid:UUID,data:bookings.BaseCommand):return bookings.change(str(rid),'cancel',data)

@app.get('/api/admin/ads/stop-queue',dependencies=[Depends(admin)])
async def admin_ads_stop_queue(view:Literal['due','upcoming','paused','all']='due',page:int=Query(default=1,ge=1,le=100000)):
    return placements.stop_queue(view,page)

@app.get('/api/admin/ads/sites',dependencies=[Depends(admin)])
async def admin_ads_sites():
    with ads.db() as conn:return {'sites':placements.sites(conn,private=True)}

@app.put('/api/admin/ads/sites/{sid}',dependencies=[Depends(admin)])
async def admin_ads_site(sid:UUID,data:placements.Site):
    return placements.save_site(str(sid),data)

@app.post('/api/admin/ads/requests/{rid}/placement/{action}',dependencies=[Depends(admin)])
async def admin_ads_placement(rid:UUID,action:Literal['start','pause','resume','finish'],data:placements.Command):
    return placements.change(str(rid),action,data)

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

@app.post('/api/auth/password-reset/request', status_code=202)
async def request_password_reset(data: PasswordResetRequest, background_tasks: BackgroundTasks):
    email=email_address(data.email)
    user=one('SELECT id,email,password FROM users WHERE email=?',(email,))
    now=int(time.time())
    if user and user['password'] and mail.enabled():
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            latest=conn.execute('SELECT max(created) FROM password_reset_tokens WHERE user_id=?',(user['id'],)).fetchone()[0]
            if latest is None or now-latest>=60:
                conn.execute('DELETE FROM password_reset_tokens WHERE user_id=? AND (expires<=? OR consumed IS NOT NULL)',(user['id'],now))
                conn.execute('DELETE FROM password_reset_tokens WHERE user_id=?',(user['id'],))
                raw=secrets.token_urlsafe(32)
                conn.execute('INSERT INTO password_reset_tokens(token_hash,user_id,created,expires) VALUES (?,?,?,?)',
                    (digest(raw),user['id'],now,now+1200))
                link=c.PUBLIC_URL+'/reset-password#'+quote(raw,safe='')
                background_tasks.add_task(mail.send_password_reset,user['email'],link)
    return JSONResponse({'ok':True,'message':'Если аккаунт с таким email зарегистрирован и почта настроена, письмо для смены пароля будет отправлено.'},status_code=202)

@app.post('/api/auth/password-reset/confirm')
async def confirm_password_reset(data: PasswordResetConfirm):
    now=int(time.time()); encoded=await asyncio.to_thread(password_hash,data.new_password)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT user_id FROM password_reset_tokens WHERE token_hash=? AND expires>? AND consumed IS NULL',
            (digest(data.token),now)).fetchone()
        if not row: raise HTTPException(400,'Ссылка недействительна или срок её действия истёк. Запросите новую.')
        uid=row['user_id']
        changed=conn.execute('UPDATE users SET password=? WHERE id=? AND email IS NOT NULL',(encoded,uid)).rowcount
        if not changed: raise HTTPException(400,'Ссылка недействительна или срок её действия истёк. Запросите новую.')
        conn.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        conn.execute('DELETE FROM tickets WHERE user_id=?',(uid,))
        conn.execute('UPDATE password_reset_tokens SET consumed=? WHERE token_hash=?',(now,digest(data.token)))
        conn.execute('DELETE FROM password_reset_tokens WHERE user_id=? AND token_hash!=?',(uid,digest(data.token)))
    return {'ok':True,'message':'Пароль изменён. Войдите с новым паролем.'}

@app.get('/reset-password')
async def reset_password_page():
    return html_page('reset-password.html')

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
    return {'id': user['id'], 'email': user['email'], 'telegram_id': user['telegram_id'], 'can_change_password': bool(user['email'] and user['password']), 'subscriptions': subs, 'orders': [{**row,'progress':order_progress.public(row)} for row in orders], 'referrals':referrals.summary(user)}

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
    if method == 'stars' and (not c.BOT_TOKEN or not shop_settings.terms_configured()):
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
        'progress': order_progress.public(order),
        'instructions': os.getenv('MANUAL_PAYMENT_TEXT', 'Свяжитесь с поддержкой и сообщите номер заказа.')
        if order['method'] == 'manual' and order['status'] in ('pending','checkout') else ''}

@app.post('/api/partner/events')
async def partner_event(request: Request):
    content_type=request.headers.get('content-type','').split(';',1)[0].strip().lower()
    if content_type!='application/json':
        raise HTTPException(415,'Ожидается application/json')
    length=request.headers.get('content-length')
    if length:
        try:
            if int(length)>4096:raise HTTPException(413,'Событие партнёра слишком большое')
        except ValueError:
            raise HTTPException(400,'Некорректная длина запроса')
    chunks=[];total=0
    async for chunk in request.stream():
        total+=len(chunk)
        if total>4096:raise HTTPException(413,'Событие партнёра слишком большое')
        chunks.append(chunk)
    body=b''.join(chunks)
    return partner.accept(request.headers,body)


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
    if config.startswith('https://'):
        try:
            parsed=urlsplit(config)
            parsed.port
        except (TypeError, ValueError):
            raise HTTPException(503,'Профиль временно недоступен') from None
        if (len(config)>4096 or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or any(ord(ch)<33 for ch in config)):
            raise HTTPException(503,'Профиль временно недоступен')
        # Сохраняем URL и User-Agent клиента: панель формирует для него формат подписки.
        return RedirectResponse(config,status_code=302,headers={
            'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
            'X-Robots-Tag':'noindex, nofollow, noarchive'})
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

@app.get('/api/admin/commerce-analytics', dependencies=[Depends(admin)])
async def commerce_analytics_report(
        start: int | None = Query(default=None, ge=0, le=commerce_analytics.MAX_TIME),
        end: int | None = Query(default=None, ge=1, le=commerce_analytics.MAX_TIME)):
    return commerce_analytics.report(start, end)


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
