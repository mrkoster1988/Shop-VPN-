import asyncio
import logging
import os
import secrets
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit
import httpx
from . import config as c, providers, servers, referrals, shop_settings, bot_menu as menu_editor
from .db import db, one, all_rows, execute
from .security import digest

log = logging.getLogger('pushok')
lock = asyncio.Lock()
worker_health={}

def reset_health():
    worker_health.clear();worker_health.update(last_started=None,last_success=None,last_failure=None,running=False)

reset_health()

async def worker_iteration():
    worker_health['last_started']=int(time.time());worker_health['running']=True
    try:await tick()
    except asyncio.CancelledError:raise
    except Exception:
        worker_health['last_failure']=int(time.time());raise
    else:worker_health['last_success']=int(time.time());worker_health['last_failure']=None
    finally:worker_health['running']=False



def telegram_account(tg_id):
    with db() as conn:
        conn.execute('INSERT OR IGNORE INTO users(telegram_id,created) VALUES (?,?)', (tg_id, int(time.time())))
        return dict(conn.execute('SELECT * FROM users WHERE telegram_id=?', (tg_id,)).fetchone())


def ticket(user_id):
    token = secrets.token_urlsafe(32)
    execute('INSERT INTO tickets VALUES (?,?,?)', (digest(token), user_id, int(time.time()) + 300))
    return c.PUBLIC_URL + '/login?ticket=' + token


async def send(tg_id, text, **kwargs):
    if not c.BOT_TOKEN or not tg_id:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post('https://api.telegram.org/bot' + c.BOT_TOKEN + '/sendMessage',
                              json={'chat_id': tg_id, 'text': text, **kwargs})
        r.raise_for_status()
        if not r.json().get('ok'):
            raise RuntimeError('Telegram send failed')


async def reconcile_payment(order):
    if not order['payment_id'] or order['status'] != 'pending' or order['method'] not in ('yookassa', 'sbp'):
        return
    payment = await providers.get_payment(order['payment_id'])
    try:
        valid = (payment['id'] == order['payment_id']
                 and payment['metadata']['order_id'] == order['id']
                 and payment['amount']['currency'] == 'RUB'
                 and Decimal(payment['amount']['value']) == Decimal(order['amount'])
                 and (order['method'] != 'sbp' or payment['payment_method']['type'] == 'sbp'))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError('Payment verification mismatch')
    if payment.get('status') == 'succeeded' and payment.get('paid') is True:
        with db() as conn:
            changed=conn.execute("UPDATE orders SET status='paid',error=NULL,next_attempt=0 WHERE id=? AND status='pending'", (order['id'],)).rowcount
            if changed:referrals.award_locked(conn,order['id'])
    elif payment.get('status') == 'canceled':
        execute("UPDATE orders SET status='canceled' WHERE id=? AND status='pending'", (order['id'],))


def primary_identity(sub):
    return c.decrypt(sub['credential_id']) if sub.get('credential_id') else sub['id']


def device_identity(device):
    return c.decrypt(device['credential_id']) if device.get('credential_id') else device['id']


async def fulfill(order_id):
    # One process / one uvicorn worker. Persist intent before remote effects, reuse on retry.
    async with lock:
        order = one('SELECT * FROM orders WHERE id=?', (order_id,))
        if not order or order['status'] != 'paid' or order['next_attempt'] > time.time():
            return
        if order['kind']=='purchase' and one("SELECT id FROM orders WHERE user_id=? AND protocol=? AND kind='trial' AND status='paid'",(order['user_id'],order['protocol'])):
            return
        try:
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                raw = conn.execute('SELECT * FROM subscriptions WHERE user_id=? AND protocol=?', (order['user_id'],order['protocol'])).fetchone()
                if raw is None:
                    server = servers.select(conn, order['protocol'])
                    if server is None:
                        raise RuntimeError('NoServerAvailable')
                    sid = str(uuid.uuid4())
                    conn.execute('INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id) VALUES (?,?,?,?,?,?)',
                        (sid,order['user_id'],order['protocol'],0,'pending',server['id']))
                    raw = conn.execute('SELECT * FROM subscriptions WHERE id=?',(sid,)).fetchone()
                sub = dict(raw)
                if not sub['server_id']:
                    raise RuntimeError('ServerBindingMissing')
                if sub['primary_key_state']=='rotating' or conn.execute("SELECT 1 FROM primary_rotations WHERE subscription_id=? AND state NOT IN ('completed','canceled')",(sub['id'],)).fetchone():
                    raise RuntimeError('PrimaryRotationPending')
                target = order['target_expires']
                if target is None:
                    target = max(int(time.time()),sub['expires']) + order['days']*86400
                    conn.execute('UPDATE orders SET target_expires=? WHERE id=?',(target,order_id))
            secret = await providers.provision({**sub, 'id':primary_identity(sub),'expires': target})
            encrypted = c.encrypt(secret)
            device_secrets = []
            for device in all_rows("SELECT * FROM devices WHERE subscription_id=? AND state='active'",(sub['id'],)):
                value = await providers.provision({**sub,'id':device_identity(device),'expires':target})
                device_secrets.append((c.encrypt(value),target,device['id']))
            with db() as conn:
                conn.executemany("UPDATE devices SET secret=?,expires=?,error=NULL WHERE id=?",device_secrets)
                conn.execute("UPDATE subscriptions SET expires=?,state='active',secret=?,reminder=0,device_limit=?,credential_id=?,primary_key_state='active',rotation_error=NULL,access_source=? WHERE id=?",
                             (target, encrypted,sub['device_limit'] if order['kind']=='referral' else order['device_limit'],sub.get('credential_id') or c.encrypt(sub['id']),sub['access_source'] if order['kind']=='referral' else order['kind'],sub['id']))
                conn.execute("UPDATE orders SET status='active',error=NULL WHERE id=?", (order_id,))
        except Exception as exc:
            # Do not persist remote responses, URLs, private keys or credentials in logs.
            attempts = order['attempts'] + 1
            execute('UPDATE orders SET attempts=?,next_attempt=?,error=? WHERE id=?',
                    (attempts, int(time.time()) + min(1800, 10 * 2 ** min(attempts, 8)),
                     type(exc).__name__, order_id))
            log.warning('Provisioning retry order=%s error=%s', order_id, type(exc).__name__)


async def process_primary_rotations_locked():
    for job in all_rows("SELECT * FROM primary_rotations WHERE state NOT IN ('completed','canceled') AND next_attempt<=? ORDER BY created,id LIMIT 50",(int(time.time()),)):
        sub=one('SELECT * FROM subscriptions WHERE id=?',(job['subscription_id'],))
        if sub['state']!='active' or sub['expires']<=time.time():
            execute("UPDATE primary_rotations SET state='canceling' WHERE id=?",(job['id'],));job['state']='canceling'
        try:
            old=c.decrypt(job['old_credential_id']);new=c.decrypt(job['new_credential_id'])
            if job['state']=='canceling':
                for identity in (old,new):await providers.disable({**sub,'id':identity})
                with db() as conn:
                    conn.execute("UPDATE subscriptions SET primary_key_state='revoked',secret=NULL,credential_id=?,rotation_error=NULL WHERE id=?",(job['new_credential_id'],sub['id']))
                    conn.execute("UPDATE primary_rotations SET state='canceled',error=NULL WHERE id=?",(job['id'],))
                    conn.execute("UPDATE orders SET next_attempt=0 WHERE user_id=? AND protocol=? AND status='paid'",(sub['user_id'],sub['protocol']))
                continue
            if job['state']=='revoking':
                await providers.disable({**sub,'id':old})
                execute("UPDATE primary_rotations SET state='issuing',error=NULL WHERE id=?",(job['id'],))
            value=await providers.provision({**sub,'id':new})
            secret=c.encrypt(value)
            with db() as conn:
                conn.execute("UPDATE subscriptions SET primary_key_state='active',credential_id=?,secret=?,rotation_error=NULL WHERE id=?",(job['new_credential_id'],secret,sub['id']))
                conn.execute("UPDATE primary_rotations SET state='completed',error=NULL WHERE id=?",(job['id'],))
                conn.execute("UPDATE orders SET next_attempt=0 WHERE user_id=? AND protocol=? AND status='paid'",(sub['user_id'],sub['protocol']))
        except Exception as exc:
            attempts=job['attempts']+1
            with db() as conn:
                conn.execute('UPDATE primary_rotations SET attempts=?,next_attempt=?,error=? WHERE id=?',(attempts,int(time.time())+min(1800,10*2**min(attempts,8)),type(exc).__name__,job['id']))
                conn.execute('UPDATE subscriptions SET rotation_error=? WHERE id=?',(type(exc).__name__,sub['id']))
            log.warning('Primary rotation retry id=%s error=%s',job['id'],type(exc).__name__)


async def process_rotations_locked():
    # Called only under the shared single-worker lock. Persist each remote phase.
    for job in all_rows("SELECT * FROM device_rotations WHERE state NOT IN ('completed','canceled') AND next_attempt<=? ORDER BY created,id LIMIT 50",(int(time.time()),)):
        device=one('SELECT * FROM devices WHERE id=?',(job['device_id'],))
        sub=one('SELECT * FROM subscriptions WHERE id=?',(device['subscription_id'],))
        if device['state']=='revoking' or sub['state']!='active' or sub['expires']<=time.time():
            execute("UPDATE device_rotations SET state='canceling' WHERE id=?",(job['id'],));job['state']='canceling'
        try:
            old_identity=c.decrypt(job['old_credential_id'])
            new_identity=c.decrypt(job['new_credential_id'])
            if job['state']=='canceling':
                # The new key may exist even if its provision response was lost.
                for identity in (old_identity,new_identity):
                    await providers.disable({**sub,'id':identity})
                with db() as conn:
                    conn.execute("UPDATE devices SET state='revoked',secret=NULL,error=NULL WHERE id=?",(device['id'],))
                    conn.execute("UPDATE device_rotations SET state='canceled',error=NULL WHERE id=?",(job['id'],))
                continue
            if job['state']=='revoking':
                await providers.disable({**sub,'id':old_identity})
                execute("UPDATE device_rotations SET state='issuing',error=NULL WHERE id=?",(job['id'],))
            value=await providers.provision({**sub,'id':new_identity})
            secret=c.encrypt(value)
            with db() as conn:
                conn.execute("UPDATE devices SET state='active',credential_id=?,secret=?,expires=?,error=NULL WHERE id=?",
                    (job['new_credential_id'],secret,sub['expires'],device['id']))
                conn.execute("UPDATE device_rotations SET state='completed',error=NULL WHERE id=?",(job['id'],))
        except Exception as exc:
            attempts=job['attempts']+1
            with db() as conn:
                conn.execute('UPDATE device_rotations SET attempts=?,next_attempt=?,error=? WHERE id=?',
                    (attempts,int(time.time())+min(1800,10*2**min(attempts,8)),type(exc).__name__,job['id']))
                conn.execute('UPDATE devices SET error=? WHERE id=?',(type(exc).__name__,device['id']))
            log.warning('Rotation retry id=%s error=%s',job['id'],type(exc).__name__)


async def process_devices():
    # Durable queue; never create another identity after an ambiguous remote response.
    async with lock:
        await process_primary_rotations_locked()
        await process_rotations_locked()
        for device in all_rows("SELECT * FROM devices WHERE state IN ('pending','revoking') AND NOT EXISTS (SELECT 1 FROM device_rotations r WHERE r.device_id=devices.id AND r.state NOT IN ('completed','canceled')) AND next_attempt<=? ORDER BY created,id LIMIT 50",(int(time.time()),)):
            sub = one('SELECT * FROM subscriptions WHERE id=?',(device['subscription_id'],))
            if device['state']=='pending' and (sub['state']!='active' or sub['expires']<=time.time()):
                execute("UPDATE devices SET state='revoking',next_attempt=0 WHERE id=?",(device['id'],))
                device['state']='revoking'
            remote = {**sub,'id':device_identity(device)}
            try:
                if device['state']=='revoking':
                    await providers.disable(remote)
                    execute("UPDATE devices SET state='revoked',secret=NULL,error=NULL WHERE id=?",(device['id'],))
                else:
                    value = await providers.provision(remote)
                    execute("UPDATE devices SET state='active',secret=?,expires=?,error=NULL WHERE id=?",(c.encrypt(value),sub['expires'],device['id']))
            except Exception as exc:
                attempts=device['attempts']+1
                execute('UPDATE devices SET attempts=?,next_attempt=?,error=? WHERE id=?',
                    (attempts,int(time.time())+min(1800,10*2**min(attempts,8)),type(exc).__name__,device['id']))
                log.warning('Device retry id=%s error=%s',device['id'],type(exc).__name__)


def notification_button(label):
    # Только навигация: URL не содержит учётных данных или одноразового билета.
    parts = urlsplit(c.PUBLIC_URL)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != 'view']
    url = parts._replace(query=urlencode(query + [('view', 'account')])).geturl()
    return {'inline_keyboard': [[{'text': label, 'web_app': {'url': url}}]]}


async def tick():
    now = int(time.time())
    for order in all_rows("SELECT * FROM orders WHERE status='pending' AND method IN ('yookassa','sbp') AND payment_id IS NOT NULL AND next_attempt<=? ORDER BY next_attempt,created LIMIT 50", (now,)):
        execute('UPDATE orders SET next_attempt=? WHERE id=?', (now + 60, order['id']))
        try:
            await reconcile_payment(order)
        except Exception as exc:
            log.warning('Payment poll error=%s', type(exc).__name__)
    for order in all_rows("SELECT id FROM orders WHERE status='paid' ORDER BY (kind='trial') DESC,created,id"):
        # A failed renewal blocks later renewals of the same subscription until it succeeds.
        current = one('SELECT * FROM orders WHERE id=?', (order['id'],))
        previous = one("SELECT id FROM orders WHERE user_id=? AND protocol=? AND status='paid' AND ((kind='trial' AND ?!='trial') OR ((kind='trial')=(?='trial') AND (created<? OR (created=? AND id<?))))",
                       (current['user_id'],current['protocol'],current['kind'],current['kind'],current['created'],current['created'],current['id']))
        if not previous:
            await fulfill(order['id'])
    async with lock:
        for sub in all_rows("SELECT * FROM subscriptions WHERE state='active' AND expires<=?", (now,)):
            try:
                for device in all_rows("SELECT * FROM devices WHERE subscription_id=? AND state='active'",(sub['id'],)):
                    await providers.disable({**sub,'id':device_identity(device),'expires':device['expires']})
                await providers.disable({**sub,'id':primary_identity(sub)})
                execute("UPDATE subscriptions SET state='expired' WHERE id=?", (sub['id'],))
            except Exception as exc:
                log.warning('Expiry retry subscription=%s error=%s', sub['id'], type(exc).__name__)
    await process_devices()
    for order in all_rows("SELECT o.*,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status='active' AND o.notified=0 AND u.telegram_id IS NOT NULL"):
        if not c.BOT_TOKEN:
            continue
        try:
            await send(order['telegram_id'], 'Пробный VPN готов! Настройки находятся в личном кабинете.' if order['kind']=='trial' else 'Бонусные дни добавлены к подписке.' if order['kind']=='referral' else 'Оплата подтверждена. VPN готов! Подключение находится в личном кабинете.',
                       reply_markup=notification_button('Открыть кабинет'))
            execute('UPDATE orders SET notified=1 WHERE id=?', (order['id'],))
        except Exception:
            pass
    for sub in all_rows("SELECT s.*,u.telegram_id FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.state='active' AND s.expires>? AND s.expires<? AND s.reminder=0 AND u.telegram_id IS NOT NULL", (now, now + 86400)):
        if not c.BOT_TOKEN:
            continue
        try:
            await send(sub['telegram_id'], 'Подписка Pushok VPN закончится менее чем через сутки. Продлите её в магазине.',
                       reply_markup=notification_button('Продлить VPN'))
            execute('UPDATE subscriptions SET reminder=1 WHERE id=?', (sub['id'],))
        except Exception:
            pass
    with db() as conn:
        conn.execute('DELETE FROM sessions WHERE expires<?', (now,))
        conn.execute('DELETE FROM tickets WHERE expires<?', (now,))


async def worker():
    while True:
        try:
            await worker_iteration()
        except Exception as exc:
            log.error('Worker iteration error=%s', type(exc).__name__)
        await asyncio.sleep(15)


def bot_menu(user_id):
    return menu_editor.keyboard(user_id, ticket)


async def send_shop_info(tg_id, section):
    settings = shop_settings.public()
    text = settings['support'] if section == 'support' else (
        settings['terms'] or 'Условия продажи ещё не заполнены владельцем магазина.')
    # Не более 4000 единиц UTF-16, включая emoji.
    for start in range(0, len(text), 2000):
        await send(tg_id, text[start:start + 2000])


async def answer_info_callback(query):
    sender = query.get('from') or {}
    message = query.get('message') or {}
    chat = message.get('chat') or {}
    tg_id = sender.get('id')
    action = query.get('data')
    valid = (type(tg_id) is int and tg_id > 0 and not sender.get('is_bot')
             and chat.get('type') == 'private' and chat.get('id') == tg_id
             and message.get('date') != 0 and action in ('shop:support', 'shop:terms'))
    if query.get('id') and c.BOT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(
                    'https://api.telegram.org/bot' + c.BOT_TOKEN + '/answerCallbackQuery',
                    json={'callback_query_id': query['id'],
                          **({} if valid else {'text': 'Откройте меню командой /start.'})})
                response.raise_for_status()
                if not response.json().get('ok'):
                    raise RuntimeError('Telegram callback acknowledgement failed')
        except Exception as exc:
            # Просроченное подтверждение кнопки не блокирует следующие обновления.
            log.warning('Callback acknowledgement error=%s', type(exc).__name__)
    if valid:
        await send_shop_info(tg_id, action.split(':')[1])


async def bot_loop():
    saved = one("SELECT value FROM meta WHERE key='bot_offset'")
    offset = int(saved['value']) if saved else 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post('https://api.telegram.org/bot' + c.BOT_TOKEN + '/getUpdates',
                                      json={'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'pre_checkout_query', 'callback_query']})
                r.raise_for_status()
                data = r.json()
                if not data.get('ok'):
                    raise RuntimeError('Telegram polling failed')
                # Answer time-sensitive pre-checkout updates before sending ordinary replies.
                for update in data['result']:
                    if 'pre_checkout_query' in update:
                        await answer_checkout(update['pre_checkout_query'])
                for update in data['result']:
                    if 'callback_query' in update:
                        await answer_info_callback(update['callback_query'])
                    msg = update.get('message', {})
                    if msg.get('chat', {}).get('type') == 'private' and not msg.get('from', {}).get('is_bot'):
                        tg_id = msg['from']['id']
                        user = telegram_account(tg_id)
                        if msg.get('successful_payment'):
                            try:
                                record_stars(tg_id, msg['successful_payment'])
                            except Exception as exc:
                                log.error('Stars payment requires review error=%s', type(exc).__name__)
                            offset = update['update_id'] + 1
                            execute("INSERT OR REPLACE INTO meta VALUES ('bot_offset',?)", (str(offset),))
                            continue
                        text = msg.get('text', '').split('@')[0].split(' ')[0]
                        if text in ('/start', '/buy', '/account'):
                            await send(tg_id, 'Pushok VPN 🐾\nВыберите тариф, оплатите и получите подключение.\nСсылка входа в браузере одноразовая и действует 5 минут.',
                                       reply_markup=bot_menu(user['id']))
                        elif text in ('/support', '/paysupport'):
                            await send_shop_info(tg_id, 'support')
                        elif text == '/terms':
                            await send_shop_info(tg_id, 'terms')
                        else:
                            await send(tg_id, 'Команды: /buy — купить VPN, /account — личный кабинет, /support — поддержка')
                    offset = update['update_id'] + 1
                    execute("INSERT OR REPLACE INTO meta VALUES ('bot_offset',?)", (str(offset),))
        except Exception as exc:
            log.warning('Bot polling error=%s', type(exc).__name__)
            await asyncio.sleep(5)



def stars_match(order, tg_id, payment):
    owner = one('SELECT telegram_id FROM users WHERE id=?', (order['user_id'],)) if order else None
    return bool(order and owner and order['method'] == 'stars' and order['currency'] == 'XTR'
                and owner['telegram_id'] == tg_id and payment.get('currency') == 'XTR'
                and payment.get('total_amount') == order['amount'])


def reserve_checkout(query):
    order = one('SELECT * FROM orders WHERE id=?', (query.get('invoice_payload', ''),))
    if not stars_match(order, query.get('from', {}).get('id'), query):
        return False
    if order['status'] == 'checkout' and order['checkout_id'] == query['id']:
        return True
    with db() as conn:
        return conn.execute("UPDATE orders SET status='checkout',checkout_id=? WHERE id=? AND status='pending'",
                            (query['id'], order['id'])).rowcount == 1


async def answer_checkout(query):
    ok = reserve_checkout(query)
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.post('https://api.telegram.org/bot' + c.BOT_TOKEN + '/answerPreCheckoutQuery',
                              json={'pre_checkout_query_id': query['id'], 'ok': ok,
                                    **({} if ok else {'error_message': 'Заказ уже обработан или недоступен. Создайте новый заказ.'})})
        r.raise_for_status()
        if not r.json().get('ok'):
            raise RuntimeError('Telegram checkout response failed')


def record_stars(tg_id, payment):
    order = one('SELECT * FROM orders WHERE id=?', (payment.get('invoice_payload', ''),))
    if not stars_match(order, tg_id, payment) or not payment.get('telegram_payment_charge_id'):
        raise RuntimeError('Stars payment mismatch')
    with db() as conn:
        # Only authenticated getUpdates results reach this function. Replayed events do not add days.
        changed=conn.execute("UPDATE orders SET status='paid',payment_id=?,next_attempt=0 WHERE id=? AND status IN ('pending','checkout')",
                     (payment['telegram_payment_charge_id'], order['id'])).rowcount
        if changed:referrals.award_locked(conn,order['id'])
