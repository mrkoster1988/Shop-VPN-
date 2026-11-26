"""Durable subscription messages for the Telegram bot of the MTProxy cabinet."""
import json
from html import escape
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import runtime_settings
from .cabinet_vpn import authorize
from .db import db

router = APIRouter()
DOMAIN = b'pushok-vpn-notices-v1:'
STATES = {'pending':'готовится', 'active':'активна', 'expired':'истекла', 'revoked':'отозвана'}
FIELDS = ('state','expires','device_limit','primary_key_state','free_traffic_gb','free_auto_renew','access_source')


def install(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cabinet_vpn_notices(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          subscription_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, expiry INTEGER,
          created INTEGER NOT NULL DEFAULT (unixepoch()), delivered INTEGER, lease TEXT,
          next_attempt INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS cabinet_vpn_notice_reminder ON cabinet_vpn_notices(subscription_id,expiry)
            WHERE kind='reminder';
        CREATE INDEX IF NOT EXISTS cabinet_vpn_notice_due ON cabinet_vpn_notices(delivered,next_attempt,id);
    """)
    def snapshot(row):
        return 'json_object('+','.join(f"'{field}',{row}.{field}" for field in FIELDS)+')'
    changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in (*FIELDS,'server_id'))
    linked = 'EXISTS(SELECT 1 FROM flow_bridge_identities b WHERE b.shop_user_id={row}.user_id)'
    for operation,kind,row,condition,payload in (
        ('INSERT','created','NEW','1',snapshot('NEW')),
        ('DELETE','removed','OLD','1',"'{}'"),
        ('UPDATE','changed','NEW',changed,"json_object('old',"+snapshot('OLD')+",'new',"+snapshot('NEW')+
            ",'moved',OLD.server_id IS NOT NEW.server_id)"),
    ):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS cabinet_notice_sub_{operation} AFTER {operation} ON subscriptions
            WHEN {row}.protocol='wg' AND ({condition}) AND {linked.format(row=row)}
            BEGIN INSERT INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload)
            VALUES ({row}.user_id,{row}.id,'{kind}',{payload}); END""")
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS cabinet_notice_expiry_reset AFTER UPDATE OF expires ON subscriptions
        WHEN NEW.protocol='wg' AND NEW.expires!=OLD.expires AND NEW.reminder!=0
        BEGIN UPDATE subscriptions SET reminder=0 WHERE id=NEW.id; END;
        CREATE TRIGGER IF NOT EXISTS cabinet_notice_profile AFTER UPDATE OF state ON devices
        WHEN OLD.state!=NEW.state AND NEW.state IN ('active','revoked')
        BEGIN INSERT INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload)
            SELECT s.user_id,s.id,'device',json_object('state',NEW.state) FROM subscriptions s
            JOIN flow_bridge_identities b ON b.shop_user_id=s.user_id WHERE s.id=NEW.subscription_id AND s.protocol='wg'; END;
        CREATE TRIGGER IF NOT EXISTS cabinet_notice_order AFTER UPDATE OF status ON orders
        WHEN NEW.protocol='wg' AND OLD.status!=NEW.status AND NEW.status IN ('paid','canceled')
             AND NEW.kind='purchase' AND NEW.amount>0
        BEGIN INSERT INTO cabinet_vpn_notices(user_id,kind,payload)
            SELECT NEW.user_id,'order',json_object('status',NEW.status,'amount',NEW.amount,'currency',NEW.currency)
            FROM flow_bridge_identities WHERE shop_user_id=NEW.user_id; END;
        CREATE TRIGGER IF NOT EXISTS cabinet_notice_link AFTER UPDATE OF version ON flow_wireguard_links
        WHEN OLD.version!=NEW.version
        BEGIN INSERT INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload)
            SELECT s.user_id,s.id,'link',json_object('enabled',NEW.enabled) FROM subscriptions s
            JOIN flow_bridge_identities b ON b.shop_user_id=s.user_id WHERE s.id=NEW.subscription_id; END;
    """)


def date(stamp):
    return datetime.fromtimestamp(stamp, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y в %H:%M МСК')


def message(row, policy):
    kind, data = row['kind'], json.loads(row['payload'])
    if kind == 'reminder':
        lines = [policy['expiry_text'], 'Доступ до '+date(row['expiry'])+'.']
    elif kind == 'premium':
        lines = ['✨ Вам подарили бессрочный Premium!',
                 'Спасибо, что вы с Пушком. Пусть на связи всегда будут те, по кому вы скучаете, а хорошие новости находят вас почаще. 💛',
                 'Оплата за счёт заведения. Продлевать подписку не придётся. Обнимаем лапками! 🐾']
    elif kind == 'renewal_plan':
        price = f"⭐ {data['stars']} Stars" if data['stars'] else 'Бесплатно'
        lines = ['✨ Ваш новый тариф — «'+data['name']+'».',
                 f"{price} · {data['days']} дн.\n📱 Дополнительные профили · {data['device_limit']}",'Доступ до '+date(data['new_expiry'])+'.' if data.get('new_expiry') else 'Срок действующей подписки сохранён. За смену тарифа деньги не списывались.']
    elif kind == 'removed':
        lines = ['🗂 Подписка удалена. Когда снова понадобится VPN, откройте раздел подключения.']
    elif kind == 'created':
        lines = ['VPN-подписка '+STATES.get(data['state'],'создана')+'.']
        if data['expires']: lines.append('Срок доступа до '+date(data['expires'])+'.')
    elif kind == 'order':
        lines = ['Оплата подтверждена.' if data['status']=='paid' else 'Заказ на подписку отменён.',
                 f"Сумма заказа {data['amount']} {data['currency']}."]
    elif kind == 'refund':
        lines = [f"Telegram подтвердил возврат {data['amount']} ⭐ за VPN-подписку."]
    elif kind == 'device':
        lines = ['Дополнительный профиль готов.' if data['state']=='active' else 'Дополнительный профиль отозван.']
    elif kind == 'link':
        lines = ['Персональная ссылка на подключение обновлена.' if data['enabled'] else 'Персональная ссылка отключена.']
    else:
        old,new = data['old'],data['new']
        lines = []
        if old['state']!=new['state']:
            lines.append({'active':'🟢 Подписка активна. Можно подключаться!',
                          'expired':'⏳ Срок подписки закончился. Её можно продлить в кабинете.',
                          'revoked':'🔒 Подписка отозвана. Прежний профиль отключён.',
                          'pending':'🐾 Готовим ваш профиль подключения.'}.get(new['state'],'🐾 Подписка обновлена.'))
        if old['expires']!=new['expires'] and new['expires']: lines.append('🗓 Доступ до '+date(new['expires'])+'.')
        if old['device_limit']!=new['device_limit']: lines.append(f"📱 Дополнительные профили · {old['device_limit']} → {new['device_limit']}.")
        if old['free_traffic_gb']!=new['free_traffic_gb']: lines.append('Лимит трафика: '+(str(new['free_traffic_gb'])+' ГБ' if new['free_traffic_gb'] else 'без ограничения')+'.')
        if old['free_auto_renew']!=new['free_auto_renew']: lines.append('Автопродление включено.' if new['free_auto_renew'] else 'Автопродление выключено.')
        if old['primary_key_state']!=new['primary_key_state']:
            lines.append({'active':'Основной профиль готов.','rotating':'Начат перевыпуск основного профиля.',
                          'revoked':'Основной профиль отозван.'}.get(new['primary_key_state'],'Состояние основного профиля изменено.'))
        if data['moved']: lines.append('Изменился узел подключения.')
        if not lines: lines.append('Условия доступа к VPN обновлены.')
    return 'Pushok ИИ\n\n'+'\n\n'.join(lines)


def claim(now, scheduled_by_cabinet=False):
    policy = runtime_settings.get('notifications')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM cabinet_vpn_notices WHERE delivered IS NOT NULL AND delivered<?',(now-30*86400,))
        if policy['expiry_enabled'] and not scheduled_by_cabinet:
            conn.execute("""INSERT OR IGNORE INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload,expiry)
                SELECT s.user_id,s.id,'reminder','{}',s.expires FROM subscriptions s
                JOIN flow_bridge_identities b ON b.shop_user_id=s.user_id
                WHERE s.protocol='wg' AND s.state='active' AND s.reminder=0 AND s.expires>? AND s.expires<=?""",
                (now,now+policy['expiry_hours']*3600))
        conn.execute("""DELETE FROM cabinet_vpn_notices WHERE kind='reminder' AND delivered IS NULL
            AND NOT EXISTS(SELECT 1 FROM subscriptions s WHERE s.id=subscription_id AND
            s.state='active' AND s.expires=expiry AND s.expires>?)""",(now,))
        rows = conn.execute("""SELECT n.*,b.telegram_id FROM cabinet_vpn_notices n
            JOIN flow_bridge_identities b ON b.shop_user_id=n.user_id
            JOIN users u ON u.id=b.shop_user_id AND u.telegram_id=b.telegram_id
            WHERE n.delivered IS NULL AND n.next_attempt<=?
            AND ((n.kind='reminder' AND ?) OR (n.kind!='reminder' AND ?))
            ORDER BY n.id LIMIT 5""",(now,policy['expiry_enabled'] and not scheduled_by_cabinet,policy['order_ready_enabled'])).fetchall()
        items = []
        for row in rows:
            lease = secrets.token_hex(16)
            conn.execute('UPDATE cabinet_vpn_notices SET lease=?,next_attempt=?,attempts=attempts+1 WHERE id=?',
                         (lease,now+90,row['id']))
            text = message(row,policy)
            paragraphs = text.removeprefix('Pushok ИИ').strip().split('\n\n')
            rendered = [escape(line) for line in paragraphs]
            if row['kind'] in ('changed','created','renewal_plan','device','link','removed') and rendered:
                rendered[0] = '<b>'+rendered[0]+'</b>'
            items.append({'id':row['id'],'lease':lease,'telegram_id':row['telegram_id'],
                          'category':row['kind'],'text':text,'formatted_body':'\n\n'.join(rendered),'renew':row['kind']=='reminder' or row['kind']=='changed'
                          and json.loads(row['payload'])['new']['state']=='expired'})
        return items


class Receipt(BaseModel):
    model_config = {'extra':'forbid'}
    id: int = Field(gt=0,strict=True)
    lease: str = Field(pattern=r'^[0-9a-f]{32}$')
    status: Literal['sent','retry','blocked']


@router.post('/api/flow/cabinet-notices/{operation}')
async def exchange(operation: Literal['claim','finish'], request: Request):
    raw = await request.body()
    authorize(operation.encode()+b':'+raw,request.headers.get('X-Pushok-Management',''),DOMAIN)
    now = int(time.time())
    if operation == 'claim':
        return {'items':claim(now,json.loads(raw or b'{}').get('scheduled_by_cabinet') is True)}
    from pydantic import ValidationError
    try: receipt = Receipt.model_validate_json(raw)
    except ValidationError: raise HTTPException(422,'Invalid receipt') from None
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM cabinet_vpn_notices WHERE id=? AND lease=? AND delivered IS NULL',
                         (receipt.id,receipt.lease)).fetchone()
        if row:
            if receipt.status=='retry':
                conn.execute('UPDATE cabinet_vpn_notices SET lease=NULL,next_attempt=? WHERE id=?',
                             (now+min(3600,30*2**min(row['attempts'],7)),receipt.id))
            else:
                conn.execute('UPDATE cabinet_vpn_notices SET delivered=?,lease=NULL WHERE id=?',(now,receipt.id))
                if receipt.status=='sent' and row['kind']=='reminder':
                    conn.execute('UPDATE subscriptions SET reminder=1 WHERE id=? AND expires=?',(row['subscription_id'],row['expiry']))
    return {'ok':True}
