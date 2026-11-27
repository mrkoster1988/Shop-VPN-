"""Политика новых платежей. Секреты провайдера остаются в окружении."""
import os
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from . import config as c, bank_review
from .db import db

METHODS=('manual','yookassa','sbp')
DEFAULT_NOTICE='Оплата временно недоступна. Обратитесь в поддержку.'


class Update(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    web_mode:Literal['inherit','manual','yookassa','sbp']
    web_enabled:bool=Field(strict=True)
    stars_enabled:bool=Field(strict=True)
    manual_text:str=Field(max_length=1000,strict=True)

    @field_validator('manual_text')
    @classmethod
    def clean_text(cls,value):
        if any(ord(ch)<32 and ch!='\n' or ord(ch)==127 for ch in value):
            raise ValueError('Недопустимый символ')
        return value.strip()


def merchant_configured():
    return bool(c.SECURE_COOKIE and os.getenv('YOOKASSA_SHOP_ID') and os.getenv('YOOKASSA_SECRET'))


def effective_mode(policy):
    mode=c.PAYMENT_MODE if policy['web_mode']=='inherit' else policy['web_mode']
    if mode not in METHODS:raise HTTPException(503,'Способ оплаты не настроен')
    return mode


def read(conn):
    row=conn.execute('SELECT * FROM shop_payment_policy WHERE id=1').fetchone()
    if not row:
        return {'version':0,'web_mode':'inherit','web_enabled':True,'stars_enabled':True,'manual_text':'','updated':0}
    return {k:row[k] for k in ('version','web_mode','manual_text','updated')} | {
        'web_enabled':bool(row['web_enabled']),'stars_enabled':bool(row['stars_enabled'])}


def open_web_orders(conn):
    return conn.execute("""SELECT count(*) FROM orders WHERE kind='purchase'
        AND method IN ('manual','yookassa','sbp') AND status IN ('pending','checkout')""").fetchone()[0]


def view(conn):
    p=read(conn)
    return p | {'effective_web_mode':effective_mode(p),'merchant_configured':merchant_configured(),
                'stars_bot_configured':bool(c.BOT_TOKEN),'open_web_orders':open_web_orders(conn)}


def get():
    with db() as conn:return view(conn)


def update(data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=read(conn)
        if old['version']!=data.version:raise HTTPException(409,'Настройки оплаты изменились. Обновите раздел.')
        values=data.model_dump(exclude={'version'})
        mode=effective_mode(values)
        if values['web_enabled'] and mode in ('yookassa','sbp') and values['web_mode']!='inherit' and not merchant_configured():
            raise HTTPException(409,'Для включения ЮKassa/СБП требуется HTTPS и настроенные реквизиты продавца.')
        if (mode!=effective_mode(old) or values['manual_text']!=old['manual_text']) and open_web_orders(conn):
            raise HTTPException(409,'Сначала завершите незакрытые рублёвые счета. Их условия нельзя менять.')
        if any(old[k]!=v for k,v in values.items()):
            conn.execute("""INSERT INTO shop_payment_policy(id,web_mode,web_enabled,stars_enabled,manual_text,version,updated)
                VALUES (1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET web_mode=excluded.web_mode,
                web_enabled=excluded.web_enabled,stars_enabled=excluded.stars_enabled,
                manual_text=excluded.manual_text,version=excluded.version,updated=excluded.updated""",
                (values['web_mode'],int(values['web_enabled']),int(values['stars_enabled']),
                 values['manual_text'],data.version+1,int(time.time())))
        return view(conn)


def method_for(user,conn=None):
    if user['telegram_id']:return 'stars'
    if conn is None:
        with db() as conn:return effective_mode(read(conn))
    return effective_mode(read(conn))


def require_new(conn,user,method,expected_version=None):
    if bank_review.enabled() and method != 'stars':
        raise HTTPException(409, bank_review.NOTICE)
    p=read(conn)
    if expected_version is not None and p['version']!=expected_version:
        raise HTTPException(409,'Настройки оплаты изменились. Повторите оформление.')
    if method!=method_for(user,conn):
        raise HTTPException(409,'Способ оплаты изменился. Повторите оформление.')
    if not p['stars_enabled' if method=='stars' else 'web_enabled']:
        raise HTTPException(409,DEFAULT_NOTICE)
    if method in ('yookassa','sbp') and p['web_mode']!='inherit' and not merchant_configured():
        raise HTTPException(503,DEFAULT_NOTICE)


def catalog(user):
    with db() as conn:
        p=read(conn);method='stars' if user and user['telegram_id'] else effective_mode(p)
        enabled=p['stars_enabled' if method=='stars' else 'web_enabled']
        if bank_review.enabled() and method != 'stars':enabled=False
        if method in ('yookassa','sbp') and p['web_mode']!='inherit' and not merchant_configured():enabled=False
        return {'payment_mode':method,'payment_available':enabled,'payment_policy_version':p['version'],
                'payment_notice':DEFAULT_NOTICE}


def manual_instructions():
    with db() as conn:
        return read(conn)['manual_text'] or os.getenv('MANUAL_PAYMENT_TEXT','Свяжитесь с поддержкой и сообщите номер заказа.')
