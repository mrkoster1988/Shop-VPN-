"""Reserved invitation terms, atomic payment awards, and days-only redemption."""
import json
import secrets
import time
import uuid
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel, Field
from . import config as c
from .db import db, one

class PolicyUpdate(BaseModel):
    model_config={'extra':'forbid'}
    enabled: bool=Field(strict=True)
    days: int=Field(ge=1,le=30,strict=True)
    max_invites: int=Field(ge=1,le=100000,strict=True)
    min_rub: int=Field(ge=1,le=1000000,strict=True)
    min_stars: int=Field(ge=1,le=100000,strict=True)
    version: int=Field(ge=1,strict=True)

class Bind(BaseModel):
    model_config={'extra':'forbid'}
    code: str=Field(min_length=24,max_length=24,pattern=r'^[a-f0-9]{24}$')
    policy_version: int=Field(ge=1,strict=True)
    accept_terms: bool=Field(strict=True)

class Redeem(BaseModel):
    model_config={'extra':'forbid'}
    subscription_id: UUID
    request_id: UUID
    days: int=Field(ge=1,le=365,strict=True)


def policy():
    p=one('SELECT * FROM referral_policy WHERE id=1');p['enabled']=bool(p['enabled'])
    p['used']=one('SELECT count(*) AS n FROM referral_bindings')['n']
    p['remaining']=max(0,p['max_invites']-p['used'])
    return p


def update(data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');before=dict(conn.execute('SELECT * FROM referral_policy WHERE id=1').fetchone())
        if before['version']!=data.version:raise HTTPException(409,'Условия приглашений изменились. Обновите страницу.')
        if all(before[k]==v for k,v in data.model_dump(exclude={'version'}).items()):return policy()
        conn.execute('UPDATE referral_policy SET enabled=?,days=?,max_invites=?,min_rub=?,min_stars=?,version=version+1,updated=? WHERE id=1',
            (int(data.enabled),data.days,data.max_invites,data.min_rub,data.min_stars,int(time.time())))
        after=dict(conn.execute('SELECT * FROM referral_policy WHERE id=1').fetchone())
        conn.execute('INSERT INTO referral_policy_changes(created,before_data,after_data) VALUES (?,?,?)',(after['updated'],json.dumps(before),json.dumps(after)))
    return policy()


def purchased(conn,uid):
    return conn.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND access_source='purchase' AND state='active' AND expires>?",(uid,int(time.time()))).fetchone() is not None


def summary(user):
    uid=user['id'];p=policy()
    with db() as conn:
        conn.execute('BEGIN')
        raw=conn.execute('SELECT code FROM referral_codes WHERE user_id=?',(uid,)).fetchone()
        code=raw['code'] if raw else None
        raw=conn.execute('SELECT days,min_rub,min_stars,policy_version,created FROM referral_bindings WHERE invitee_id=?',(uid,)).fetchone()
        bound=dict(raw) if raw else None
        if bound:bound['rewarded']=bool(conn.execute('SELECT 1 FROM referral_rewards WHERE invitee_id=?',(uid,)).fetchone())
        eligible=purchased(conn,uid)
        fresh=not conn.execute('SELECT 1 FROM orders WHERE user_id=? UNION ALL SELECT 1 FROM subscriptions WHERE user_id=? LIMIT 1',(uid,uid)).fetchone()
        earned=conn.execute('SELECT coalesce(sum(days),0) FROM referral_rewards WHERE inviter_id=?',(uid,)).fetchone()[0]
        spent=conn.execute('SELECT coalesce(sum(days),0) FROM referral_redemptions WHERE user_id=?',(uid,)).fetchone()[0]
        invited=conn.execute('SELECT count(*) FROM referral_bindings WHERE inviter_id=?',(uid,)).fetchone()[0]
        rewarded=conn.execute('SELECT count(*) FROM referral_rewards WHERE inviter_id=?',(uid,)).fetchone()[0]
        history=[dict(r) for r in conn.execute('SELECT days,created FROM referral_rewards WHERE inviter_id=? ORDER BY created DESC,rowid DESC LIMIT 20',(uid,))]
    return {'policy':{k:p[k] for k in ('enabled','days','min_rub','min_stars','version')},
        'can_share':eligible and p['enabled'] and p['remaining']>0,'can_bind':fresh and not bound and p['enabled'] and p['remaining']>0,
        'code':code,'url':c.PUBLIC_URL+'/?ref='+code if code else None,
        'bound':bound,'earned_days':earned,'spent_days':spent,'balance_days':earned-spent,
        'invited':invited,'rewarded':rewarded,'history':history}


def code(user):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE');p=dict(conn.execute('SELECT * FROM referral_policy WHERE id=1').fetchone())
        if not p['enabled'] or not purchased(conn,user['id']):raise HTTPException(409,'Приглашения доступны владельцу действующей платной подписки при включённой программе')
        conn.execute('INSERT OR IGNORE INTO referral_codes(user_id,code,created) VALUES (?,?,?)',(user['id'],secrets.token_hex(12),int(time.time())))
    return summary(user)


def bind(user,data):
    uid=user['id']
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=conn.execute('SELECT c.code FROM referral_bindings b JOIN referral_codes c ON c.user_id=b.inviter_id WHERE b.invitee_id=?',(uid,)).fetchone()
        if previous:
            if previous['code']!=data.code:raise HTTPException(409,'Пригласивший уже выбран и не может быть заменён')
            return summary(user)
        if not data.accept_terms:raise HTTPException(422,'Подтвердите условия приглашения')
        p=dict(conn.execute('SELECT * FROM referral_policy WHERE id=1').fetchone())
        if not p['enabled'] or p['version']!=data.policy_version:raise HTTPException(409,'Условия приглашений изменились. Обновите кабинет.')
        inviter=conn.execute('SELECT user_id FROM referral_codes WHERE code=?',(data.code,)).fetchone()
        if not inviter or inviter['user_id']==uid:raise HTTPException(409,'Приглашение недоступно')
        if conn.execute('SELECT 1 FROM orders WHERE user_id=? UNION ALL SELECT 1 FROM subscriptions WHERE user_id=? LIMIT 1',(uid,uid)).fetchone():raise HTTPException(409,'Приглашение принимают до первого заказа или пробного доступа')
        if not purchased(conn,inviter['user_id']):raise HTTPException(409,'Приглашение недоступно')
        if conn.execute('SELECT count(*) FROM referral_bindings').fetchone()[0]>=p['max_invites']:raise HTTPException(409,'Лимит приглашений исчерпан')
        conn.execute('INSERT INTO referral_bindings(invitee_id,inviter_id,days,min_rub,min_stars,policy_version,created) VALUES (?,?,?,?,?,?,?)',
            (uid,inviter['user_id'],p['days'],p['min_rub'],p['min_stars'],p['version'],int(time.time())))
    return summary(user)


def award_locked(conn,oid):
    # Called in the same transaction that records a verified payment. No historical backfill.
    order=conn.execute("SELECT * FROM orders WHERE id=? AND status='paid' AND kind='purchase' AND amount>0",(oid,)).fetchone()
    if not order:return
    binding=conn.execute('SELECT * FROM referral_bindings WHERE invitee_id=?',(order['user_id'],)).fetchone()
    if not binding:return
    minimum=binding['min_stars'] if order['currency']=='XTR' else binding['min_rub']
    if order['currency'] not in ('RUB','XTR') or order['amount']<minimum:return
    conn.execute('INSERT OR IGNORE INTO referral_rewards(invitee_id,inviter_id,order_id,days,created) VALUES (?,?,?,?,?)',
        (binding['invitee_id'],binding['inviter_id'],oid,binding['days'],int(time.time())))


def redeem(user,data):
    uid=user['id'];sid=str(data.subscription_id);request=str(data.request_id)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=conn.execute('SELECT * FROM referral_redemptions WHERE user_id=? AND request_id=?',(uid,request)).fetchone()
        if previous:
            if previous['subscription_id']!=sid or previous['days']!=data.days:raise HTTPException(409,'Этот запрос уже использован с другими условиями')
            return previous['order_id']
        raw=conn.execute('SELECT * FROM subscriptions WHERE id=? AND user_id=?',(sid,uid)).fetchone()
        if not raw:raise HTTPException(404,'Подписка не найдена')
        sub=dict(raw)
        if sub['access_source']!='purchase' or sub['state']!='active' or sub['expires']<=time.time():raise HTTPException(409,'Бонус продлевает только действующую платную подписку')
        earned=conn.execute('SELECT coalesce(sum(days),0) FROM referral_rewards WHERE inviter_id=?',(uid,)).fetchone()[0]
        spent=conn.execute('SELECT coalesce(sum(days),0) FROM referral_redemptions WHERE user_id=?',(uid,)).fetchone()[0]
        if data.days>earned-spent:raise HTTPException(409,'Недостаточно бонусных дней')
        oid=str(uuid.uuid4());now=int(time.time())
        conn.execute("INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,base_amount,currency,method,status,created,kind,device_limit) VALUES (?,?,?,'referral','Реферальное продление',?,0,0,'BONUS','referral','paid',?,'referral',?)",(oid,uid,sub['protocol'],data.days,now,sub['device_limit']))
        conn.execute('INSERT INTO referral_redemptions(user_id,request_id,subscription_id,order_id,days,created) VALUES (?,?,?,?,?,?)',(uid,request,sid,oid,data.days,now))
    return oid
