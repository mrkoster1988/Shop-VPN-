"""One accepted trial per Telegram account. Policy edits never reset claims."""
import json
import os
import time
import uuid
from fastapi import HTTPException
from pydantic import BaseModel,Field
from typing import Literal
from . import config as c,servers,shop_settings,protocols
from .db import db,one

class PolicyUpdate(BaseModel):
    model_config={'extra':'forbid'}
    enabled: bool=Field(strict=True)
    days: int=Field(ge=1,le=3,strict=True)
    max_claims: int=Field(ge=1,le=100000,strict=True)
    version: int=Field(ge=1,strict=True)

class Claim(BaseModel):
    model_config={'extra':'forbid'}
    protocol: protocols.SalesProtocol
    policy_version: int=Field(ge=1,strict=True)
    accept_terms: bool=Field(default=False,strict=True)


def configured():
    return bool(c.BOT_TOKEN and c.BOT_USERNAME and shop_settings.terms_configured())


def policy():
    row=one('SELECT * FROM trial_policy WHERE id=1')
    row['enabled']=bool(row['enabled'])
    row['used']=one('SELECT count(*) AS n FROM trial_claims')['n']
    row['remaining']=max(0,row['max_claims']-row['used'])
    return row


def update(data):
    if data.enabled and not configured():raise HTTPException(409,'Сначала настройте бота, его имя и условия продажи')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        before=dict(conn.execute('SELECT * FROM trial_policy WHERE id=1').fetchone())
        if before['version']!=data.version:raise HTTPException(409,'Условия пробного доступа изменились. Обновите страницу.')
        if all(before[key]==value for key,value in data.model_dump(exclude={'version'}).items()):return policy()
        conn.execute('UPDATE trial_policy SET enabled=?,days=?,max_claims=?,version=version+1,updated=? WHERE id=1',
            (int(data.enabled),data.days,data.max_claims,int(time.time())))
        after=dict(conn.execute('SELECT * FROM trial_policy WHERE id=1').fetchone())
        conn.execute('INSERT INTO trial_policy_changes(created,before_data,after_data) VALUES (?,?,?)',
            (after['updated'],json.dumps(before),json.dumps(after)))
    return policy()


def offer(user):
    p=policy();claim=None;reason='disabled'
    if user:
        claim=one('SELECT o.id,o.protocol,o.status,o.days FROM trial_claims t JOIN orders o ON o.id=t.order_id WHERE t.user_id=?',(user['id'],))
    if claim:reason='already_claimed'
    elif p['enabled']:
        if not configured():reason='not_configured'
        elif not user or not user['telegram_id']:reason='telegram_required'
        elif one('SELECT 1 AS n FROM orders WHERE user_id=? UNION ALL SELECT 1 FROM subscriptions WHERE user_id=? LIMIT 1',(user['id'],user['id'])):reason='existing_customer'
        elif p['remaining']==0:reason='limit_reached'
        else:reason='eligible'
    return {'enabled':p['enabled'],'days':p['days'],'version':p['version'],'device_limit':0,
        'telegram_only':True,'eligible':reason=='eligible','reason':reason,'claim':claim}


def claim(user,data):
    if not user['telegram_id']:raise HTTPException(403,'Пробный доступ доступен только через Telegram')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=conn.execute('SELECT order_id,protocol FROM trial_claims WHERE user_id=?',(user['id'],)).fetchone()
        if previous:
            if previous['protocol']!=data.protocol:raise HTTPException(409,'Пробный доступ уже выбран для другого протокола')
            return previous['order_id']
        p=dict(conn.execute('SELECT * FROM trial_policy WHERE id=1').fetchone())
        if not p['enabled'] or not configured():raise HTTPException(409,'Пробный доступ сейчас недоступен')
        if p['version']!=data.policy_version:raise HTTPException(409,'Условия пробного доступа изменились. Обновите каталог.')
        if not data.accept_terms:raise HTTPException(422,'Подтвердите условия пробного доступа')
        if conn.execute('SELECT 1 FROM orders WHERE user_id=? UNION ALL SELECT 1 FROM subscriptions WHERE user_id=? LIMIT 1',(user['id'],user['id'])).fetchone():
            raise HTTPException(409,'Пробный доступ предназначен для аккаунтов без предыдущих заказов и подписок')
        if conn.execute('SELECT count(*) FROM trial_claims').fetchone()[0]>=p['max_claims']:
            raise HTTPException(409,'Лимит пробных активаций исчерпан')
        server=servers.select(conn,data.protocol)
        if not server:raise HTTPException(409,'Для пробного доступа сейчас нет свободного сервера')
        sid,oid=str(uuid.uuid4()),str(uuid.uuid4());now=int(time.time())
        conn.execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id,device_limit,access_source) VALUES (?,?,?,0,'pending',?,0,'trial')",(sid,user['id'],data.protocol,server['id']))
        conn.execute("INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,currency,method,status,created,plan_version,device_limit,kind,base_amount) VALUES (?,?,?,'trial','Пробный доступ',?,0,'XTR','trial','paid',?,?,0,'trial',0)",
            (oid,user['id'],data.protocol,p['days'],now,p['version']))
        conn.execute('INSERT INTO trial_claims(user_id,order_id,protocol,created,policy_version,terms_snapshot) VALUES (?,?,?,?,?,?)',
            (user['id'],oid,data.protocol,now,p['version'],shop_settings.terms()))
    return oid
