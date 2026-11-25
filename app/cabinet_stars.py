"""WireGuard checkout through the existing Telegram bot of the MTProxy cabinet."""
import hashlib
import hmac
import json
import os
import re
import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import mtproxy_web_login, service, shop_finance
from .cabinet_vpn import authorize
from .db import db, one

router = APIRouter()
DOMAIN = b'pushok-shop-vpn-stars-v1:'
PREFIX = 'shop-vpn:'


def available(user, protocol):
    return protocol == 'wg' and mtproxy_web_login.enabled() and bool(one(
        'SELECT 1 FROM flow_bridge_identities WHERE shop_user_id=? AND telegram_id=?',
        (user['id'],user['telegram_id'])))


async def invoice(order):
    raw = json.dumps({'order_id':order['id'],'days':order['days'],'amount':order['amount']},
                     separators=(',',':')).encode()
    stamp = str(int(time.time()))
    key = bytes.fromhex(os.environ['FLOW_SHOP_BRIDGE_SECRET'])
    signature = hmac.digest(key,DOMAIN+stamp.encode()+b':invoice:'+raw,'sha256').hex()
    with db() as conn:
        conn.execute('INSERT OR IGNORE INTO cabinet_vpn_invoices(order_id) VALUES (?)',(order['id'],))
    async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
        result = await client.post('https://pushok.shop/api/v1/shop/vpn-stars/invoice',content=raw,
            headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+signature})
        result.raise_for_status()
        link = result.json().get('url')
    if not isinstance(link,str) or not re.fullmatch(r'https://t\.me/\$[A-Za-z0-9_-]{1,256}',link):
        raise ValueError('Invalid invoice link')
    return link


class Event(BaseModel):
    model_config = {'extra':'forbid'}
    invoice_payload: str = Field(pattern=r'^shop-vpn:[a-f0-9-]{36}$')
    telegram_user_id: int = Field(gt=0,strict=True)
    currency: Literal['XTR']
    total_amount: int = Field(gt=0,strict=True)
    query_id: str | None = Field(default=None,max_length=256,min_length=1)
    telegram_payment_charge_id: str | None = Field(default=None,max_length=256,min_length=1)
    occurred_at: int | None = Field(default=None,gt=0,strict=True)


@router.post('/api/flow/cabinet-stars/{operation}')
async def callback(operation: Literal['precheckout','settle','refund'], event: Event, request: Request):
    raw = await request.body()
    authorize(operation.encode()+b':'+raw,request.headers.get('X-Pushok-Management',''),DOMAIN)
    oid = event.invoice_payload[len(PREFIX):]
    async with service.lock:
        order = one('SELECT o.* FROM orders o JOIN cabinet_vpn_invoices i ON i.order_id=o.id WHERE o.id=?',(oid,))
        proof = {'invoice_payload':oid,'currency':event.currency,'total_amount':event.total_amount,
                 'telegram_payment_charge_id':event.telegram_payment_charge_id}
        if not service.stars_match(order,event.telegram_user_id,proof):
            if operation=='precheckout':return {'ok':False}
            raise HTTPException(409,'payment_mismatch')
        if operation=='precheckout':
            if not event.query_id:return {'ok':False}
            return {'ok':service.reserve_checkout({**proof,'id':event.query_id,'from':{'id':event.telegram_user_id}})}
        if not event.telegram_payment_charge_id:
            raise HTTPException(422,'missing_charge')
        if operation=='settle':
            if order['payment_id']==event.telegram_payment_charge_id:return {'status':'already_processed'}
            if order['payment_id'] or order['status'] not in ('checkout','pending'):
                raise HTTPException(409,'payment_conflict')
            service.record_stars(event.telegram_user_id,proof)
            return {'status':'applied'}
        if order['payment_id']!=event.telegram_payment_charge_id or not event.occurred_at:
            raise HTTPException(409,'refund_mismatch')
        ref='tg-vpn-refund:'+hashlib.sha256(event.telegram_payment_charge_id.encode()).hexdigest()
        shop_finance.record(shop_finance.Entry(operation_id=uuid5(NAMESPACE_URL,ref),kind='refund',category='refund',
            currency='XTR',amount_minor=event.total_amount,occurred_at=event.occurred_at,order_id=oid,
            reference=ref,note='Telegram подтвердил возврат Stars по VPN-заказу.',verified=True),{})
        with db() as conn:
            if conn.execute('UPDATE cabinet_vpn_invoices SET refunded=? WHERE order_id=? AND refunded IS NULL',
                            (event.occurred_at,oid)).rowcount:
                conn.execute("""INSERT INTO cabinet_vpn_notices(user_id,kind,payload) VALUES (?,'refund',?)""",
                             (order['user_id'],json.dumps({'amount':event.total_amount})))
            conn.execute("UPDATE orders SET status='canceled' WHERE id=? AND status='paid'",(oid,))
        return {'status':'recorded'}
