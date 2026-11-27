"""Учёт подтверждённых поступлений и сверенных расходов/возвратов.

Не банк, не кошелёк и не API для отправки денег. Рубли в копейках, XTR в звёздах.
"""
import hashlib
import json
import re
import time
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .db import db
from .commerce_analytics import MAX_TIME

METHODS=('manual','yookassa','sbp','platega','stars')
CURRENCIES=('RUB','XTR')
CATEGORIES=('server','commission','marketing','tax','refund','other')


class Entry(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    kind:Literal['expense','fee','refund']
    category:Literal['server','commission','marketing','tax','refund','other']
    currency:Literal['RUB','XTR']
    amount_minor:int=Field(ge=1,le=10000000000,strict=True)
    occurred_at:int=Field(ge=1,le=MAX_TIME,strict=True)
    order_id:str|None=Field(default=None,max_length=128)
    reference:str=Field(min_length=3,max_length=120,strict=True)
    note:str=Field(min_length=1,max_length=500,strict=True)
    verified:bool=Field(strict=True)

    @field_validator('reference')
    @classmethod
    def reference_safe(cls,value):
        value=value.strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9:._/-]{2,119}',value):
            raise ValueError('Введите идентификатор операции, без персональных данных')
        return value

    @field_validator('note')
    @classmethod
    def note_safe(cls,value):
        value=value.strip()
        if not value or any(ord(ch)<32 and ch!='\n' or ord(ch)==127 for ch in value):
            raise ValueError('Укажите основание')
        return value

    @model_validator(mode='after')
    def validate_entry(self):
        if not self.verified:raise ValueError('Сначала сверьте фактическую операцию')
        if self.kind=='refund' and (self.category!='refund' or not self.order_id):
            raise ValueError('Возврат требует номер заказа и категорию возврата')
        if self.kind=='fee' and self.category!='commission':
            raise ValueError('Комиссия требует категорию commission')
        if self.kind=='expense' and self.category in ('refund','commission'):
            raise ValueError('Используйте соответствующий тип операции')
        return self


class Void(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    version:int=Field(ge=1,strict=True)
    note:str=Field(min_length=1,max_length=500,strict=True)

    @field_validator('note')
    @classmethod
    def reason(cls,value):return Entry.note_safe(value)


def fingerprint(data):
    return hashlib.sha256(json.dumps(data.model_dump(mode='json'),sort_keys=True,ensure_ascii=False,
        separators=(',',':')).encode()).hexdigest()


def item(conn,eid):
    row=conn.execute('SELECT * FROM shop_finance_entries WHERE id=?',(eid,)).fetchone()
    if not row:raise HTTPException(404,'Операция не найдена')
    correction=conn.execute("SELECT note FROM shop_finance_actions WHERE entry_id=? AND action='void'",(eid,)).fetchone()
    return dict(row)|{'void_reason':correction['note'] if correction else None}


def record(data,actor):
    operation=str(data.operation_id);fp=fingerprint(data)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM shop_finance_actions WHERE operation_id=?',(operation,)).fetchone()
        if old:
            if old['action']!='record' or old['fingerprint']!=fp:
                raise HTTPException(409,'Идентификатор уже использован для другой операции')
            return item(conn,old['entry_id'])
        now=int(time.time())
        if data.occurred_at>now:raise HTTPException(422,'Укажите дату фактической операции, не будущую')
        if data.order_id:
            order=conn.execute("""SELECT id,amount,currency,method,status,kind FROM orders
                WHERE id=? AND kind='purchase' AND status IN ('paid','active')""",(data.order_id,)).fetchone()
            if not order or order['method'] not in METHODS or order['currency']!=data.currency:
                raise HTTPException(409,'Нужен подтверждённый заказ в той же валюте')
            if data.kind=='refund':
                gross=order['amount']*(100 if data.currency=='RUB' else 1)
                already=conn.execute("""SELECT coalesce(sum(amount_minor),0) FROM shop_finance_entries
                    WHERE order_id=? AND kind='refund' AND state='recorded'""",(data.order_id,)).fetchone()[0]
                if already+data.amount_minor>gross:
                    raise HTTPException(409,'Общая сумма возвратов превышает оплату заказа')
        if conn.execute('SELECT 1 FROM shop_finance_entries WHERE currency=? AND reference=?',
                        (data.currency,data.reference)).fetchone():
            raise HTTPException(409,'Такой идентификатор уже зарегистрирован')
        eid=str(uuid.uuid4())
        conn.execute("""INSERT INTO shop_finance_entries
            (id,kind,category,currency,amount_minor,occurred_at,order_id,reference,note,state,version,created,staff_id)
            VALUES (?,?,?,?,?,?,?,?,?,'recorded',1,?,?)""",
            (eid,data.kind,data.category,data.currency,data.amount_minor,data.occurred_at,data.order_id,
             data.reference,data.note,now,actor.get('id')))
        conn.execute("""INSERT INTO shop_finance_actions(operation_id,entry_id,action,fingerprint,note,created,staff_id)
            VALUES (?,?,'record',?,?,?,?)""",(operation,eid,fp,data.note,now,actor.get('id')))
        return item(conn,eid)


def void(eid,data,actor):
    operation=str(data.operation_id);fp=fingerprint(data)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM shop_finance_actions WHERE operation_id=?',(operation,)).fetchone()
        if old:
            if old['action']!='void' or old['entry_id']!=eid or old['fingerprint']!=fp:
                raise HTTPException(409,'Идентификатор уже использован')
            return item(conn,eid)
        row=item(conn,eid)
        if row['state']!='recorded' or row['version']!=data.version:
            raise HTTPException(409,'Операция уже исправлена. Обновите журнал.')
        now=int(time.time())
        conn.execute("UPDATE shop_finance_entries SET state='void',version=version+1 WHERE id=?",(eid,))
        conn.execute("""INSERT INTO shop_finance_actions(operation_id,entry_id,action,fingerprint,note,created,staff_id)
            VALUES (?,?,'void',?,?,?,?)""",(operation,eid,fp,data.note,now,actor.get('id')))
        return item(conn,eid)


def period(start,end):
    if (start is None)!=(end is None):raise HTTPException(422,'Укажите обе границы периода')
    if start is None:
        end=int(time.time())+1;start=max(0,end-30*86400)
    if not 0<=start<end<=MAX_TIME or end-start>366*86400:
        raise HTTPException(422,'Период должен быть не больше 366 дней')
    return start,end


def report(start=None,end=None,page=1):
    start,end=period(start,end)
    with db() as conn:
        conn.execute('BEGIN')
        totals={c:{'receipts':0,'refunds':0,'fees':0,'expenses':0,'net_recorded':0} for c in CURRENCIES}
        income=conn.execute("""SELECT o.currency,o.method,count(*) AS count,
            sum(o.amount * CASE WHEN o.currency='RUB' THEN 100 ELSE 1 END) AS amount_minor
            FROM order_milestones m JOIN orders o ON o.id=m.order_id
            WHERE m.event='paid' AND m.recorded_at>=? AND m.recorded_at<?
              AND o.kind='purchase' AND o.status IN ('paid','active')
              AND o.method IN ('manual','yookassa','sbp','platega','stars') AND o.currency IN ('RUB','XTR')
            GROUP BY o.currency,o.method ORDER BY o.currency,o.method""",(start,end)).fetchall()
        for r in income:totals[r['currency']]['receipts']+=r['amount_minor']
        deductions=conn.execute("""SELECT currency,kind,sum(amount_minor) AS amount
            FROM shop_finance_entries WHERE state='recorded' AND occurred_at>=? AND occurred_at<?
            GROUP BY currency,kind""",(start,end)).fetchall()
        for r in deductions:totals[r['currency']][{'refund':'refunds','fee':'fees','expense':'expenses'}[r['kind']]]+=r['amount']
        for t in totals.values():t['net_recorded']=t['receipts']-t['refunds']-t['fees']-t['expenses']
        missing=conn.execute("""SELECT count(*) FROM orders o WHERE o.kind='purchase'
            AND o.status IN ('paid','active') AND o.method IN ('manual','yookassa','sbp','platega','stars')
            AND NOT EXISTS (SELECT 1 FROM order_milestones m WHERE m.order_id=o.id AND m.event='paid')""").fetchone()[0]
        since=conn.execute('SELECT recorded_since FROM order_history_state WHERE id=1').fetchone()
        count=conn.execute('SELECT count(*) FROM shop_finance_entries WHERE occurred_at>=? AND occurred_at<?',(start,end)).fetchone()[0]
        rows=conn.execute('SELECT id FROM shop_finance_entries WHERE occurred_at>=? AND occurred_at<? ORDER BY occurred_at DESC,id DESC LIMIT 25 OFFSET ?',(start,end,(page-1)*25)).fetchall()
        return {'period':{'start':start,'end':end},'unit':{'RUB':'kopecks','XTR':'stars'},
                'source':'first_observed_paid_and_verified_manual_entries','totals':totals,
                'receipts_by_method':[dict(r) for r in income],'missing_payment_timestamp':missing,
                'recording_started_at':since['recorded_since'] if since else None,
                'entries':[item(conn,r['id']) for r in rows],'total':count,'page':page,'page_size':25,
                'real_refunds_executed':False,'is_wallet':False}
