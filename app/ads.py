"""Заявки рекламы MTProxy: модерация отделена от оплаты и фактического размещения."""
import hashlib
import json
import re
import time
import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,Field,field_validator
from .db import db

class Policy(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    accepting:bool=Field(strict=True)
    amount:int=Field(ge=1,le=1000000,strict=True)
    hours:int=Field(ge=1,le=720,strict=True)
    instructions:str=Field(default='',max_length=1000)

class RequestCreate(BaseModel):
    model_config={'extra':'forbid'}
    request_id:uuid.UUID
    policy_version:int=Field(ge=0,strict=True)
    channel:str=Field(min_length=5,max_length=100)
    comment:str=Field(default='',max_length=1000)
    @field_validator('channel')
    @classmethod
    def normalize(cls,value):
        value=value.strip()
        if value.startswith('https://t.me/'):value=value[13:]
        elif value.startswith('@'):value=value[1:]
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',value):raise ValueError('Нужен публичный адрес канала @username или https://t.me/username')
        return value.lower()
    @field_validator('comment')
    @classmethod
    def trim(cls,value):return value.strip()

class Decision(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=1,strict=True)
    state:Literal['approved','rejected']
    reason:str=Field(default='',max_length=500)
    @field_validator('reason')
    @classmethod
    def trim(cls,value):return value.strip()

class Cancel(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=1,strict=True)

class PaymentCommand(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    request_version:int=Field(ge=1,strict=True)
    payment_version:int=Field(ge=0,le=2,strict=True)
    amount:int=Field(ge=1,le=1000000,strict=True)
    reference:str=Field(min_length=3,max_length=120)
    note:str=Field(min_length=1,max_length=500)
    checked:bool=Field(strict=True)
    @field_validator('reference')
    @classmethod
    def reference_format(cls,value):
        value=value.strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9:._/-]{2,119}',value):raise ValueError('Укажите идентификатор операции без персональных данных')
        return value
    @field_validator('note')
    @classmethod
    def note_format(cls,value):
        value=value.strip()
        if not value:raise ValueError('Нужно основание проверки')
        return value
    @field_validator('checked')
    @classmethod
    def checked_required(cls,value):
        if not value:raise ValueError('Сначала проверьте фактическое движение денег')
        return value

DEFAULT={'accepting':False,'amount':300,'hours':24,'instructions':'Заявка проходит модерацию. Автоматическая оплата и запуск размещения пока не выполняются.','version':0,'updated':0}

def policy(conn=None):
    if conn is None:
        with db() as conn:return policy(conn)
    row=conn.execute('SELECT * FROM ads_policy WHERE id=1').fetchone()
    result={k:row[k] for k in DEFAULT} if row else dict(DEFAULT)
    result['accepting']=bool(result['accepting']);result['currency']='RUB'
    return result

def event(conn,rid,actor,action,details):
    conn.execute('INSERT INTO ads_events(request_id,actor,action,created,details) VALUES (?,?,?,?,?)',
                 (rid,actor,action,int(time.time()),json.dumps(details,ensure_ascii=False)))

def update_policy(data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');before=policy(conn)
        if before['version']!=data.version:raise HTTPException(409,'Настройки изменились. Обновите раздел.')
        now=int(time.time())
        conn.execute('INSERT INTO ads_policy VALUES (1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET accepting=excluded.accepting,amount=excluded.amount,hours=excluded.hours,instructions=excluded.instructions,version=excluded.version,updated=excluded.updated',
                     (int(data.accepting),data.amount,data.hours,data.instructions.strip(),data.version+1,now))
        after=policy(conn);event(conn,None,'admin','policy',{'before':before,'after':after});return after

def payment(conn,rid,private=False):
    rows=conn.execute('SELECT * FROM ads_payment_entries WHERE request_id=? ORDER BY id',(rid,)).fetchall()
    result={'status':'refunded' if len(rows)==2 else 'confirmed' if rows else 'unpaid','version':len(rows),
            'received_amount':sum(r['amount'] for r in rows if r['kind']=='receipt'),
            'refunded_amount':sum(r['amount'] for r in rows if r['kind']=='refund'),'currency':'RUB'}
    if private:result['entries']=[{k:r[k] for k in ('kind','amount','reference','note','created')} for r in rows]
    return result

def public(row,conn,private=False):
    return {k:row[k] for k in ('id','channel','comment','amount','hours','policy_version','state','reason','version','created','updated')} | {'currency':'RUB','payment':payment(conn,row['id'],private=private)}

def create(user,data):
    now=int(time.time());uid=user['id']
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=conn.execute('SELECT * FROM ads_requests WHERE user_id=? AND request_id=?',(uid,str(data.request_id))).fetchone()
        if previous:
            if previous['channel']!=data.channel or previous['comment']!=data.comment or previous['policy_version']!=data.policy_version:
                raise HTTPException(409,'Этот запрос уже использован для другой заявки.')
            return public(previous,conn)
        p=policy(conn)
        if not p['accepting']:raise HTTPException(409,'Приём заявок рекламы сейчас закрыт.')
        if p['version']!=data.policy_version:raise HTTPException(409,'Условия изменились. Проверьте новый тариф.')
        if conn.execute("SELECT count(*) FROM ads_requests WHERE user_id=? AND state IN ('pending','approved')",(uid,)).fetchone()[0]>=5:
            raise HTTPException(409,'Сначала завершите текущие заявки: доступно не более пяти открытых.')
        if conn.execute("SELECT 1 FROM ads_requests WHERE user_id=? AND channel=? AND state IN ('pending','approved')",(uid,data.channel)).fetchone():
            raise HTTPException(409,'Для этого канала уже есть открытая заявка.')
        rid=str(uuid.uuid4())
        conn.execute("INSERT INTO ads_requests(id,user_id,request_id,channel,comment,amount,hours,policy_version,state,created,updated) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
                     (rid,uid,str(data.request_id),data.channel,data.comment,p['amount'],p['hours'],p['version'],now,now))
        event(conn,rid,'customer','created',{'user_id':uid,'amount':p['amount'],'hours':p['hours']})
        return public(conn.execute('SELECT * FROM ads_requests WHERE id=?',(rid,)).fetchone(),conn)

def get(rid,uid=None):
    with db() as conn:
        conn.execute('BEGIN')
        row=conn.execute('SELECT * FROM ads_requests WHERE id=?'+(' AND user_id=?' if uid is not None else ''),(rid,uid) if uid is not None else (rid,)).fetchone()
        if not row:raise HTTPException(404,'Заявка не найдена.')
        result=public(row,conn,private=uid is None)
        if uid is None:result['user_id']=row['user_id']
        return result

def listing(uid=None,state=None,page=1):
    clauses=[];args=[]
    if uid is not None:clauses.append('user_id=?');args.append(uid)
    if state:clauses.append('state=?');args.append(state)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    with db() as conn:
        conn.execute('BEGIN')
        total=conn.execute('SELECT count(*) FROM ads_requests'+where,args).fetchone()[0]
        rows=conn.execute('SELECT * FROM ads_requests'+where+' ORDER BY created DESC,id DESC LIMIT 20 OFFSET ?',args+[(page-1)*20]).fetchall()
        return {'requests':[public(row,conn,private=uid is None) | ({'user_id':row['user_id']} if uid is None else {}) for row in rows],'total':total,'page':page,'page_size':20}

def transition(rid,data,uid=None):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM ads_requests WHERE id=?'+(' AND user_id=?' if uid is not None else ''),(rid,uid) if uid is not None else (rid,)).fetchone()
        if not row:raise HTTPException(404,'Заявка не найдена.')
        if row['version']!=data.version:raise HTTPException(409,'Заявка изменена. Обновите раздел.')
        target='canceled' if uid is not None else data.state
        allowed=('pending','approved') if uid is not None else ('pending',)
        if row['state'] not in allowed:raise HTTPException(409,'Действие недоступно для текущего состояния.')
        if uid is not None and payment(conn,rid)['status']=='confirmed':
            raise HTTPException(409,'Для оплаченной заявки сначала требуется проверенный возврат. Обратитесь к владельцу.')
        reason='' if uid is not None else data.reason
        if target=='rejected' and not reason:raise HTTPException(422,'Укажите причину отклонения.')
        conn.execute('UPDATE ads_requests SET state=?,reason=?,version=version+1,updated=? WHERE id=?',(target,reason,int(time.time()),rid))
        event(conn,rid,'customer' if uid is not None else 'admin',target,{'reason':reason} | ({'user_id':uid} if uid is not None else {}))
    return get(rid,uid)


def record_payment(rid,kind,data):
    # Журнал отражает ручную сверку владельца, а не ответ банка или платёжного API.
    raw=data.model_dump(mode='json')
    fingerprint=hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM ads_payment_entries WHERE operation_id=?',(str(data.operation_id),)).fetchone()
        if old:
            if old['request_id']!=rid or old['kind']!=kind or old['fingerprint']!=fingerprint:
                raise HTTPException(409,'Идентификатор уже использован для другой операции.')
            return payment(conn,rid,private=True)
        row=conn.execute('SELECT * FROM ads_requests WHERE id=?',(rid,)).fetchone()
        if not row:raise HTTPException(404,'Заявка не найдена.')
        current=payment(conn,rid)
        if row['version']!=data.request_version or current['version']!=data.payment_version:
            raise HTTPException(409,'Данные изменились. Обновите раздел перед проверкой.')
        if row['state']!='approved':raise HTTPException(409,'Учёт оплаты доступен только для одобренной заявки.')
        expected='unpaid' if kind=='receipt' else 'confirmed'
        if current['status']!=expected:raise HTTPException(409,'Операция недоступна для текущего состояния оплаты.')
        if data.amount!=row['amount']:raise HTTPException(409,'Нужна точная сумма из заявки. Частичные операции пока не поддерживаются.')
        if conn.execute('SELECT 1 FROM ads_payment_entries WHERE reference=?',(data.reference,)).fetchone():
            raise HTTPException(409,'Эта банковская операция уже учтена.')
        conn.execute('INSERT INTO ads_payment_entries(operation_id,request_id,kind,amount,reference,note,fingerprint,created) VALUES (?,?,?,?,?,?,?,?)',
                     (str(data.operation_id),rid,kind,data.amount,data.reference,data.note,fingerprint,int(time.time())))
        event(conn,rid,'admin','payment_'+kind,{'amount':data.amount,'currency':'RUB','reference':data.reference,'verification':'manual'})
        return payment(conn,rid,private=True)
