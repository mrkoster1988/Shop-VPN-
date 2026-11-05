"""Сводка ручного учёта рекламы: денежный остаток не объявляется чистой прибылью."""
import hashlib
import json
import time
import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,Field,field_validator
from . import ads
from .db import db

MAX_TIME=32503680000

class Expense(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    amount:int=Field(ge=1,le=1000000,strict=True)
    category:Literal['server','commission','marketing','other']
    incurred_at:int=Field(ge=1,le=MAX_TIME,strict=True)
    reference:str=Field(min_length=3,max_length=120)
    note:str=Field(min_length=1,max_length=500)
    @field_validator('reference')
    @classmethod
    def reference_format(cls,value):return ads.PaymentCommand.reference_format(value)
    @field_validator('note')
    @classmethod
    def note_format(cls,value):return ads.PaymentCommand.note_format(value)

class Void(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    version:int=Field(ge=1,strict=True)
    note:str=Field(min_length=1,max_length=500)
    @field_validator('note')
    @classmethod
    def note_format(cls,value):return ads.PaymentCommand.note_format(value)


def public(row,conn):
    correction=conn.execute("SELECT note FROM ads_expense_actions WHERE expense_id=? AND action='void'",(row['id'],)).fetchone()
    return {k:row[k] for k in ('id','amount','category','incurred_at','reference','note','state','version','created','updated')} | {'void_reason':correction['note'] if correction else None}


def change(action,data,eid=None):
    fingerprint=hashlib.sha256(json.dumps(data.model_dump(mode='json'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM ads_expense_actions WHERE operation_id=?',(str(data.operation_id),)).fetchone()
        if old:
            if old['action']!=action or old['fingerprint']!=fingerprint or (eid is not None and eid!=old['expense_id']):raise HTTPException(409,'UUID уже использован для другого расхода или действия.')
            return public(conn.execute('SELECT * FROM ads_expenses WHERE id=?',(old['expense_id'],)).fetchone(),conn)
        now=int(time.time())
        if action=='create':
            if data.incurred_at>now:raise HTTPException(409,'Расход должен быть фактически понесён; будущая дата недоступна.')
            if conn.execute("SELECT 1 FROM ads_expenses WHERE reference=? AND state='recorded'",(data.reference,)).fetchone():raise HTTPException(409,'Расход с этим ID уже учтён.')
            eid=str(uuid.uuid4())
            conn.execute("INSERT INTO ads_expenses VALUES (?,?,?,?,?,?,'recorded',1,?,?)",(eid,data.amount,data.category,data.incurred_at,data.reference,data.note,now,now))
        elif action=='void':
            row=conn.execute('SELECT * FROM ads_expenses WHERE id=?',(eid,)).fetchone()
            if not row:raise HTTPException(404,'Расход не найден.')
            if row['version']!=data.version or row['state']!='recorded':raise HTTPException(409,'Запись изменена или уже исключена. Обновите сводку.')
            conn.execute("UPDATE ads_expenses SET state='void',version=version+1,updated=? WHERE id=?",(now,eid))
        else:raise HTTPException(422,'Неизвестное действие расхода')
        conn.execute('INSERT INTO ads_expense_actions VALUES (?,?,?,?,?,?)',(str(data.operation_id),eid,action,fingerprint,data.note,now))
        row=conn.execute('SELECT * FROM ads_expenses WHERE id=?',(eid,)).fetchone()
        ads.event(conn,None,'admin','expense_'+action,{'expense_id':eid,'amount':row['amount'],'category':row['category'],'verification':'manual'})
        return public(row,conn)


def report(start=None,end=None,page=1):
    now=int(time.time())
    if (start is None)!=(end is None):raise HTTPException(422,'Укажите обе границы периода.')
    if start is None:end=now+1;start=max(0,end-30*86400)
    if not 0<=start<end<=MAX_TIME or end-start>366*86400:raise HTTPException(422,'Период должен быть положительным и не превышать 366 дней.')
    with db() as conn:
        conn.execute('BEGIN')
        money=conn.execute("SELECT coalesce(sum(CASE WHEN kind='receipt' THEN amount ELSE 0 END),0) AS received,coalesce(sum(CASE WHEN kind='refund' THEN amount ELSE 0 END),0) AS refunded FROM ads_payment_entries WHERE created>=? AND created<?",(start,end)).fetchone()
        expenses=conn.execute("SELECT coalesce(sum(CASE WHEN state='recorded' THEN amount ELSE 0 END),0) AS amount,sum(CASE WHEN state='recorded' THEN 1 ELSE 0 END) AS recorded_count,sum(CASE WHEN state='void' THEN 1 ELSE 0 END) AS voided_count,count(*) AS total FROM ads_expenses WHERE incurred_at>=? AND incurred_at<?",(start,end)).fetchone()
        rows=conn.execute('SELECT * FROM ads_expenses WHERE incurred_at>=? AND incurred_at<? ORDER BY incurred_at DESC,id LIMIT 20 OFFSET ?',(start,end,(page-1)*20)).fetchall()
        groups={k:{'requests':0,'contract_amount':0,'remaining_seconds':0} for k in ('not_started','active','paused','early_stopped','completed')}
        statuses="CASE WHEN p.request_id IS NULL THEN 'not_started' WHEN p.state='finished' AND p.used_seconds>=p.allocated_seconds THEN 'completed' WHEN p.state='finished' THEN 'early_stopped' ELSE p.state END"
        remaining="CASE WHEN p.request_id IS NULL THEN r.hours*3600 ELSE max(0,p.allocated_seconds-min(p.allocated_seconds,p.used_seconds+CASE WHEN p.state='active' THEN max(0,?-p.segment_started) ELSE 0 END)) END"
        sql='SELECT '+statuses+' AS status,count(*) AS requests,sum(r.amount) AS contract_amount,sum('+remaining+") AS remaining_seconds FROM ads_requests r JOIN ads_payment_entries receipt ON receipt.request_id=r.id AND receipt.kind='receipt' LEFT JOIN ads_payment_entries refund ON refund.request_id=r.id AND refund.kind='refund' LEFT JOIN ads_placements p ON p.request_id=r.id WHERE refund.id IS NULL GROUP BY status"
        for row in conn.execute(sql,(now,)):groups[row['status']]={k:row[k] for k in ('requests','contract_amount','remaining_seconds')}
        net=money['received']-money['refunded']
        return {'generated_at':now,'currency':'RUB','source':'manual_ledger','period':{'start':start,'end':end},
                'cash':{'received':money['received'],'refunded':money['refunded'],'net_received':net},
                'expenses':{'amount':expenses['amount'],'recorded_count':expenses['recorded_count'] or 0,'voided_count':expenses['voided_count'] or 0,'entries':[public(r,conn) for r in rows],'total':expenses['total'],'page':page,'page_size':20},
                'cash_after_recorded_expenses':net-expenses['amount'],'is_net_profit':False,
                'current_paid_requests':groups}
