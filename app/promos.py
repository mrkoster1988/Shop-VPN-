"""Discounts use integer currency units. One code reserves one order per account."""
import json
import re
import time
import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,Field,field_validator
from . import config as c, protocols
from .db import db,all_rows

class CouponFields(BaseModel):
    model_config={'extra':'forbid'}
    percent: int=Field(ge=1,le=90,strict=True)
    enabled: bool=Field(default=False,strict=True)
    expires_at: int=Field(default=0,ge=0,le=4102444800,strict=True)
    max_uses: int=Field(default=100,ge=1,le=100000,strict=True)
    plan_id: str | None=Field(default=None,max_length=32,pattern=r'^[a-z][a-z0-9_-]*$')
    protocol: Literal['all','xui','wg','vmess','hysteria2']='all'

class CouponCreate(CouponFields):
    code: str=Field(min_length=4,max_length=32,pattern=r'^[A-Z0-9][A-Z0-9_-]{3,31}$')
    @field_validator('code',mode='before')
    @classmethod
    def normalize(cls,value):return value.strip().upper() if isinstance(value,str) else value

class CouponUpdate(CouponFields):
    version: int=Field(ge=1,strict=True)

class Quote(BaseModel):
    model_config={'extra':'forbid'}
    plan: str=Field(min_length=1,max_length=32,pattern=r'^[a-z][a-z0-9_-]*$')
    protocol: protocols.SalesProtocol
    plan_version: int=Field(ge=1,strict=True)
    promo_code: str | None=Field(default=None,min_length=1,max_length=32)


def normalize(value):
    if value is None:return None
    value=value.strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{3,31}',value):raise HTTPException(422,'Проверьте промокод: 4–32 латинских символа, цифры, дефис или подчёркивание')
    return value


def listing():
    return [{**row,'enabled':bool(row['enabled'])} for row in all_rows('SELECT c.*,(SELECT count(*) FROM coupon_uses u WHERE u.coupon_id=c.id) AS used FROM coupons c ORDER BY created DESC,id')]


def validate_scope(conn,data):
    if data.plan_id and not conn.execute('SELECT 1 FROM plans WHERE id=?',(data.plan_id,)).fetchone():raise HTTPException(422,'Тариф для промокода не найден')


def create(data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');validate_scope(conn,data)
        if conn.execute('SELECT 1 FROM coupons WHERE code=?',(data.code,)).fetchone():raise HTTPException(409,'Такой промокод уже существует')
        cid=str(uuid.uuid4());now=int(time.time())
        conn.execute('INSERT INTO coupons(id,code,percent,enabled,expires_at,max_uses,plan_id,protocol,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (cid,data.code,data.percent,int(data.enabled),data.expires_at,data.max_uses,data.plan_id,data.protocol,now,now))
        row=dict(conn.execute('SELECT * FROM coupons WHERE id=?',(cid,)).fetchone())
        conn.execute('INSERT INTO coupon_changes(coupon_id,created,before_data,after_data) VALUES (?,?,?,?)',(cid,now,'null',json.dumps(row)))
    return next(p for p in listing() if p['id']==cid)


def update(cid,data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');raw=conn.execute('SELECT * FROM coupons WHERE id=?',(cid,)).fetchone()
        if not raw:raise HTTPException(404,'Промокод не найден')
        before=dict(raw)
        if before['version']!=data.version:raise HTTPException(409,'Промокод изменён. Обновите список.')
        validate_scope(conn,data)
        if all(before[key]==value for key,value in data.model_dump(exclude={'version'}).items()):return next(p for p in listing() if p['id']==cid)
        conn.execute('UPDATE coupons SET percent=?,enabled=?,expires_at=?,max_uses=?,plan_id=?,protocol=?,version=version+1,updated=? WHERE id=?',
            (data.percent,int(data.enabled),data.expires_at,data.max_uses,data.plan_id,data.protocol,int(time.time()),cid))
        after=dict(conn.execute('SELECT * FROM coupons WHERE id=?',(cid,)).fetchone())
        conn.execute('INSERT INTO coupon_changes(coupon_id,created,before_data,after_data) VALUES (?,?,?,?)',(cid,after['updated'],json.dumps(before),json.dumps(after)))
    return next(p for p in listing() if p['id']==cid)


def quote_locked(conn,data,user):
    raw=conn.execute('SELECT * FROM plans WHERE id=? AND enabled=1',(data.plan,)).fetchone()
    if not raw:raise HTTPException(409,'Тариф больше недоступен')
    plan=dict(raw)
    if plan['version']!=data.plan_version:raise HTTPException(409,'Условия тарифа изменились. Рассчитайте стоимость заново.')
    base=plan['stars'] if user['telegram_id'] else plan['price'];code=normalize(data.promo_code)
    coupon=None;discount=0
    if code:
        raw=conn.execute('SELECT * FROM coupons WHERE code=?',(code,)).fetchone()
        if not raw:raise HTTPException(409,'Промокод недоступен или исчерпан')
        coupon=dict(raw)
        if code==c.PARTNER_PROMO_CODE:
            grant=conn.execute('SELECT 1 FROM partner_events WHERE telegram_id=? AND promo_code=? AND expires_at>? LIMIT 1',
                (user['telegram_id'] or 0,code,int(time.time()))).fetchone() if user['telegram_id'] else None
            if not grant:raise HTTPException(409,'Промокод недоступен или исчерпан')
        if (not coupon['enabled'] or (coupon['expires_at'] and coupon['expires_at']<=time.time())
            or coupon['plan_id'] not in (None,plan['id']) or coupon['protocol'] not in ('all',data.protocol)
            or conn.execute('SELECT count(*) FROM coupon_uses WHERE coupon_id=?',(coupon['id'],)).fetchone()[0]>=coupon['max_uses']):
            raise HTTPException(409,'Промокод недоступен или исчерпан')
        if conn.execute('SELECT 1 FROM coupon_uses WHERE coupon_id=? AND user_id=?',(coupon['id'],user['id'])).fetchone():
            raise HTTPException(409,'Этот код уже использован аккаунтом. Откройте заказ в истории.')
        discount=base*coupon['percent']//100
    amount=max(1,base-discount)
    result={'plan':plan['id'],'plan_name':plan['name'],'days':plan['days'],'device_limit':plan['device_limit'],
        'plan_version':plan['version'],'protocol':data.protocol,'currency':'XTR' if user['telegram_id'] else 'RUB',
        'base_amount':base,'amount':amount,'discount':base-amount,'promo_code':code,
        'promo_version':coupon['version'] if coupon else None,'promo_percent':coupon['percent'] if coupon else None}
    return result,coupon


def quote(data,user):
    with db() as conn:
        conn.execute('BEGIN')
        return quote_locked(conn,data,user)[0]


def make_order(data,user,method,email):
    code=normalize(data.promo_code)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if code:
            previous=conn.execute('SELECT o.* FROM coupon_uses u JOIN coupons c ON c.id=u.coupon_id JOIN orders o ON o.id=u.order_id WHERE u.user_id=? AND c.code=?',(user['id'],code)).fetchone()
            if previous:
                order=dict(previous)
                expected=(data.plan,data.protocol,data.plan_version,method,data.promo_version,data.quoted_amount,data.quote_currency)
                actual=tuple(order[k] for k in ('plan','protocol','plan_version','method','promo_version','amount','currency'))
                if expected!=actual or order['status']=='canceled':raise HTTPException(409,'Этот код уже использован. Откройте заказ в истории.')
                return order
        result,coupon=quote_locked(conn,data,user)
        if code and (data.promo_version is None or data.quoted_amount is None or data.quote_currency is None):
            raise HTTPException(409,'Сначала рассчитайте скидку и подтвердите итоговую сумму')
        if code and data.promo_version!=result['promo_version']:raise HTTPException(409,'Условия промокода изменились. Рассчитайте скидку заново.')
        if data.quoted_amount is not None and data.quoted_amount!=result['amount']:raise HTTPException(409,'Итоговая сумма изменилась. Рассчитайте стоимость заново.')
        if data.quote_currency is not None and data.quote_currency!=result['currency']:raise HTTPException(409,'Валюта изменилась. Рассчитайте стоимость заново.')
        if not code:
            if data.promo_version is not None:raise HTTPException(422,'Версия промокода указана без кода')
            previous=conn.execute("SELECT * FROM orders WHERE user_id=? AND protocol=? AND plan=? AND method=? AND plan_version=? AND promo_id IS NULL AND kind='purchase' AND status='pending' AND created>? ORDER BY created DESC,id LIMIT 1",
                (user['id'],data.protocol,data.plan,method,data.plan_version,int(time.time())-1800)).fetchone()
            if previous:return dict(previous)
        oid=str(uuid.uuid4());now=int(time.time())
        conn.execute('INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,receipt_email,currency,method,plan_version,plan_name,device_limit,base_amount,discount,promo_id,promo_code,promo_version,promo_percent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (oid,user['id'],data.protocol,data.plan,result['days'],result['amount'],now,email,result['currency'],method,result['plan_version'],result['plan_name'],result['device_limit'],result['base_amount'],result['discount'],coupon['id'] if coupon else None,code,result['promo_version'],result['promo_percent']))
        if coupon:conn.execute('INSERT INTO coupon_uses(coupon_id,user_id,order_id,created) VALUES (?,?,?,?)',(coupon['id'],user['id'],oid,now))
        return dict(conn.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone())
