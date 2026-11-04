import asyncio
import logging
import os
import secrets
import time
import uuid
from decimal import Decimal, InvalidOperation
import httpx
from . import config as c, providers, servers
from .db import db, one, all_rows, execute
from .security import digest

log = logging.getLogger('pushok')
lock = asyncio.Lock()


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
    if not order['payment_id'] or order['status'] != 'pending' or order['method'] != 'yookassa':
        return
    payment = await providers.get_payment(order['payment_id'])
    try:
        valid = (payment['id'] == order['payment_id']
                 and payment['metadata']['order_id'] == order['id']
                 and payment['amount']['currency'] == 'RUB'
                 and Decimal(payment['amount']['value']) == Decimal(order['amount']))
    except (KeyError, InvalidOperation, TypeError):
        valid = False
    if not valid:
        raise RuntimeError('Payment verification mismatch')
    if payment.get('status') == 'succeeded' and payment.get('paid') is True:
        execute("UPDATE orders SET status='paid',error=NULL,next_attempt=0 WHERE id=? AND status='pending'", (order['id'],))
    elif payment.get('status') == 'canceled':
        execute("UPDATE orders SET status='canceled' WHERE id=? AND status='pending'", (order['id'],))


async def fulfill(order_id):
    # One process / one uvicorn worker. Persist intent before remote effects, reuse on retry.
    async with lock:
        order = one('SELECT * FROM orders WHERE id=?', (order_id,))
        if not order or order['status'] != 'paid' or order['next_attempt'] > time.time():
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
                target = order['target_expires']
                if target is None:
                    target = max(int(time.time()),sub['expires']) + order['days']*86400
                    conn.execute('UPDATE orders SET target_expires=? WHERE id=?',(target,order_id))
            secret = await providers.provision({**sub, 'expires': target})
            encrypted = c.encrypt(secret)
            device_secrets = []
            for device in all_rows("SELECT * FROM devices WHERE subscription_id=? AND state='active'",(sub['id'],)):
                value = await providers.provision({**sub,'id':device['id'],'expires':target})
                device_secrets.append((c.encrypt(value),target,device['id']))
            with db() as conn:
                conn.executemany("UPDATE devices SET secret=?,expires=?,error=NULL WHERE id=?",device_secrets)
                conn.execute("UPDATE subscriptions SET expires=?,state='active',secret=?,reminder=0,device_limit=? WHERE id=?",
                             (target, encrypted,order['device_limit'],sub['id']))
                conn.execute("UPDATE orders SET status='active',error=NULL WHERE id=?", (order_id,))
        except Exception as exc:
            # Do not persist remote responses, URLs, private keys or credentials in logs.
            attempts = order['attempts'] + 1
            execute('UPDATE orders SET attempts=?,next_attempt=?,error=? WHERE id=?',
                    (attempts, int(time.time()) + min(1800, 10 * 2 ** min(attempts, 8)),
                     type(exc).__name__, order_id))
            log.warning('Provisioning retry order=%s error=%s', order_id, type(exc).__name__)


async def process_devices():
    # Durable queue; never create another identity after an ambiguous remote response.
    async with lock:
        for device in all_rows("SELECT * FROM devices WHERE state IN ('pending','revoking') AND next_attempt<=? ORDER BY created,id LIMIT 50",(int(time.time()),)):
            sub = one('SELECT * FROM subscriptions WHERE id=?',(device['subscription_id'],))
            if device['state']=='pending' and (sub['state']!='active' or sub['expires']<=time.time()):
                execute("UPDATE devices SET state='revoking',next_attempt=0 WHERE id=?",(device['id'],))
                device['state']='revoking'
            remote = {**sub,'id':device['id']}
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


async def tick():
    now = int(time.time())
    for order in all_rows("SELECT * FROM orders WHERE status='pending' AND method='yookassa' AND payment_id IS NOT NULL AND next_attempt<=? ORDER BY next_attempt,created LIMIT 50", (now,)):
        execute('UPDATE orders SET next_attempt=? WHERE id=?', (now + 60, order['id']))
        try:
            await reconcile_payment(order)
        except Exception as exc:
            log.warning('Payment poll error=%s', type(exc).__name__)
    for order in all_rows("SELECT id FROM orders WHERE status='paid' ORDER BY created,id"):
        # A failed renewal blocks later renewals of the same subscription until it succeeds.
        current = one('SELECT * FROM orders WHERE id=?', (order['id'],))
        previous = one("SELECT id FROM orders WHERE user_id=? AND protocol=? AND status='paid' AND (created<? OR (created=? AND id<?))",
                       (current['user_id'], current['protocol'], current['created'], current['created'], current['id']))
        if not previous:
            await fulfill(order['id'])
    async with lock:
        for sub in all_rows("SELECT * FROM subscriptions WHERE state='active' AND expires<=?", (now,)):
            try:
                for device in all_rows("SELECT * FROM devices WHERE subscription_id=? AND state='active'",(sub['id'],)):
                    await providers.disable({**sub,'id':device['id'],'expires':device['expires']})
                await providers.disable(sub)
                execute("UPDATE subscriptions SET state='expired' WHERE id=?", (sub['id'],))
            except Exception as exc:
                log.warning('Expiry retry subscription=%s error=%s', sub['id'], type(exc).__name__)
    await process_devices()
    for order in all_rows("SELECT o.*,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status='active' AND o.notified=0"):
        try:
            await send(order['telegram_id'], 'Оплата подтверждена. VPN готов! Подключение находится в личном кабинете.')
            execute('UPDATE orders SET notified=1 WHERE id=?', (order['id'],))
        except Exception:
            pass
    for sub in all_rows("SELECT s.*,u.telegram_id FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE s.state='active' AND s.expires>? AND s.expires<? AND s.reminder=0", (now, now + 86400)):
        try:
            await send(sub['telegram_id'], 'Подписка Pushok VPN закончится менее чем через сутки. Продлите её в магазине.')
            execute('UPDATE subscriptions SET reminder=1 WHERE id=?', (sub['id'],))
        except Exception:
            pass
    with db() as conn:
        conn.execute('DELETE FROM sessions WHERE expires<?', (now,))
        conn.execute('DELETE FROM tickets WHERE expires<?', (now,))


async def worker():
    while True:
        try:
            await tick()
        except Exception as exc:
            log.error('Worker iteration error=%s', type(exc).__name__)
        await asyncio.sleep(15)


async def bot_loop():
    saved = one("SELECT value FROM meta WHERE key='bot_offset'")
    offset = int(saved['value']) if saved else 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post('https://api.telegram.org/bot' + c.BOT_TOKEN + '/getUpdates',
                                      json={'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'pre_checkout_query']})
                r.raise_for_status()
                data = r.json()
                if not data.get('ok'):
                    raise RuntimeError('Telegram polling failed')
                # Answer time-sensitive pre-checkout updates before sending ordinary replies.
                for update in data['result']:
                    if 'pre_checkout_query' in update:
                        await answer_checkout(update['pre_checkout_query'])
                for update in data['result']:
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
                            url = ticket(user['id'])
                            buttons = [[{'text': 'Купить / продлить VPN', 'web_app': {'url': c.PUBLIC_URL}}],
                                       [{'text': 'Открыть кабинет в браузере', 'url': url}]]
                            await send(tg_id, 'Pushok VPN 🐾\nВыберите тариф, оплатите и получите подключение.\nСсылка входа в браузере одноразовая и действует 5 минут.',
                                       reply_markup={'inline_keyboard': buttons})
                        elif text in ('/support', '/paysupport'):
                            await send(tg_id, os.getenv('SUPPORT_TEXT', 'Напишите администратору магазина.'))
                        elif text == '/terms':
                            await send(tg_id, os.getenv('TERMS_TEXT', 'Условия продажи ещё не заполнены.'))
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
        conn.execute("UPDATE orders SET status='paid',payment_id=?,next_attempt=0 WHERE id=? AND status IN ('pending','checkout')",
                     (payment['telegram_payment_charge_id'], order['id']))
