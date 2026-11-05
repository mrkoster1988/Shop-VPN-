"""Ручное подтверждение размещений: площадка занята до подтверждённой остановки."""
import hashlib
import json
import time
import uuid
from fastapi import HTTPException
from pydantic import BaseModel,Field,field_validator
from . import ads
from .db import db

class Site(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    name:str=Field(min_length=1,max_length=80)
    enabled:bool=Field(default=False,strict=True)
    verified:bool=Field(default=False,strict=True)
    note:str=Field(min_length=1,max_length=500)
    @field_validator('name','note')
    @classmethod
    def trim(cls,value):
        value=value.strip()
        if not value:raise ValueError('Заполните поле')
        return value

class Command(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    request_version:int=Field(ge=1,strict=True)
    payment_version:int=Field(ge=0,le=2,strict=True)
    placement_version:int=Field(ge=0,strict=True)
    site_id:uuid.UUID|None=None
    site_version:int|None=Field(default=None,ge=1,strict=True)
    note:str=Field(min_length=1,max_length=500)
    checked:bool=Field(strict=True)
    @field_validator('note')
    @classmethod
    def trim(cls,value):
        value=value.strip()
        if not value:raise ValueError('Укажите основание проверки')
        return value
    @field_validator('checked')
    @classmethod
    def verified(cls,value):
        if not value:raise ValueError('Подтвердите фактическое состояние размещения')
        return value


def sites(conn,private=False):
    rows=conn.execute("SELECT s.*,EXISTS(SELECT 1 FROM ads_placements p WHERE p.site_id=s.id AND p.state IN ('active','paused')) AS occupied FROM ads_sites s ORDER BY name,id").fetchall()
    keys=('id','name','enabled','verified','occupied','version')+ (('note','updated') if private else ())
    return [{k:bool(r[k]) if k in ('enabled','verified','occupied') else r[k] for k in keys} for r in rows]


def save_site(sid,data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM ads_sites WHERE id=?',(sid,)).fetchone()
        if (old['version'] if old else 0)!=data.version:raise HTTPException(409,'Площадка изменена. Обновите список.')
        if not old and conn.execute('SELECT count(*) FROM ads_sites').fetchone()[0]>=100:
            raise HTTPException(409,'Доступно не более 100 площадок; выключите ненужные.')
        if not data.verified and conn.execute("SELECT 1 FROM ads_placements WHERE site_id=? AND state='active'",(sid,)).fetchone():
            raise HTTPException(409,'Сначала подтвердите паузу или остановку активного размещения.')
        conn.execute('INSERT INTO ads_sites VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled,verified=excluded.verified,note=excluded.note,version=excluded.version,updated=excluded.updated',
                     (sid,data.name,int(data.enabled),int(data.verified),data.note,data.version+1,int(time.time())))
        ads.event(conn,None,'admin','site_saved',{'site_id':sid,'version':data.version+1,'enabled':data.enabled,'verified':data.verified,'note':data.note})
        return next(r for r in sites(conn,True) if r['id']==sid)


def summary(conn,rid,private=False,now=None):
    row=conn.execute('SELECT * FROM ads_placements WHERE request_id=?',(rid,)).fetchone()
    if not row:return None
    now=int(time.time()) if now is None else now
    used=min(row['allocated_seconds'],row['used_seconds']+(max(0,now-row['segment_started']) if row['state']=='active' else 0))
    remaining=row['allocated_seconds']-used
    result={k:row[k] for k in ('state','version','site_name','allocated_seconds','started','finished','updated')} | {
        'used_seconds':used,'remaining_seconds':remaining,'attention_due':row['state']=='active' and remaining==0,
        'expected_stop_at':row['segment_started']+row['allocated_seconds']-row['used_seconds'] if row['state']=='active' else None,
        'verification':'manual'}
    if private:
        result['actions']=[dict(r) for r in conn.execute('SELECT action,note,created FROM ads_placement_actions WHERE request_id=? ORDER BY rowid DESC LIMIT 50',(rid,))]
    return result


def occupied(conn,rid):
    return bool(conn.execute("SELECT 1 FROM ads_placements WHERE request_id=? AND state IN ('active','paused')",(rid,)).fetchone())


def change(rid,action,data):
    if action not in ('start','pause','resume','finish'):raise HTTPException(422,'Неизвестное действие размещения')
    fingerprint=hashlib.sha256(json.dumps(data.model_dump(mode='json'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM ads_placement_actions WHERE operation_id=?',(str(data.operation_id),)).fetchone()
        if old:
            if old['request_id']!=rid or old['action']!=action or old['fingerprint']!=fingerprint:
                raise HTTPException(409,'Идентификатор использован для другого действия.')
            return summary(conn,rid,private=True)
        request=conn.execute('SELECT * FROM ads_requests WHERE id=?',(rid,)).fetchone()
        if not request:raise HTTPException(404,'Заявка не найдена.')
        p=conn.execute('SELECT * FROM ads_placements WHERE request_id=?',(rid,)).fetchone()
        if request['version']!=data.request_version or ads.payment(conn,rid)['version']!=data.payment_version or (p['version'] if p else 0)!=data.placement_version:
            raise HTTPException(409,'Данные изменились. Обновите заявку.')
        if request['state']!='approved' or ads.payment(conn,rid)['status']!='confirmed':
            raise HTTPException(409,'Нужны одобренная заявка и учтённое поступление без возврата.')
        now=int(time.time())
        if action=='start':
            if p:raise HTTPException(409,'Для заявки уже учтено размещение.')
            site=conn.execute('SELECT * FROM ads_sites WHERE id=?',(str(data.site_id),)).fetchone() if data.site_id else None
            if not site or not site['enabled'] or not site['verified'] or site['version']!=data.site_version:
                raise HTTPException(409,'Выберите включённую проверенную площадку и её текущую версию.')
            if conn.execute("SELECT 1 FROM ads_placements WHERE site_id=? AND state IN ('active','paused')",(site['id'],)).fetchone():
                raise HTTPException(409,'Площадка занята другим размещением, включая паузу.')
            conn.execute("INSERT INTO ads_placements(request_id,site_id,site_name,state,version,allocated_seconds,used_seconds,segment_started,started,updated) VALUES (?,?,?,'active',1,?,0,?,?,?)",
                         (rid,site['id'],site['name'],request['hours']*3600,now,now,now))
        else:
            if data.site_id is not None or data.site_version is not None:raise HTTPException(422,'При продолжении площадка не заменяется.')
            if not p or p['state']=='finished':raise HTTPException(409,'Размещение не запущено или уже завершено.')
            used=summary(conn,rid)['used_seconds']
            if action=='pause':
                if p['state']!='active':raise HTTPException(409,'Размещение уже на паузе.')
                state='paused';segment=None
            elif action=='resume':
                if p['state']!='paused' or used>=p['allocated_seconds']:raise HTTPException(409,'Нет доступного времени для продолжения.')
                site=conn.execute('SELECT * FROM ads_sites WHERE id=?',(p['site_id'],)).fetchone()
                if not site['enabled'] or not site['verified']:raise HTTPException(409,'Площадка выключена или не проверена.')
                state='active';segment=now
            else:
                state='finished';segment=None
            conn.execute('UPDATE ads_placements SET state=?,used_seconds=?,segment_started=?,finished=?,version=version+1,updated=? WHERE request_id=?',
                         (state,used,segment,now if state=='finished' else None,now,rid))
        conn.execute('INSERT INTO ads_placement_actions VALUES (?,?,?,?,?,?)',(str(data.operation_id),rid,action,fingerprint,data.note,now))
        ads.event(conn,rid,'admin','placement_'+action,{'note':data.note,'verification':'manual'})
        return summary(conn,rid,private=True)


def stop_queue(view='due',page=1):
    """Снимок очереди без автоматической остановки, списания времени или освобождения места."""
    deadline='p.segment_started+p.allocated_seconds-p.used_seconds'
    now=int(time.time())
    clauses={'due':(" AND p.state='active' AND "+deadline+'<=?',[now]),
             'upcoming':(" AND p.state='active' AND "+deadline+'>?',[now]),
             'paused':(" AND p.state='paused'",[]),'all':('',[])}
    if view not in clauses:raise HTTPException(422,'Неизвестный фильтр очереди')
    extra,args=clauses[view]
    base=" FROM ads_placements p JOIN ads_requests r ON r.id=p.request_id WHERE p.state IN ('active','paused')"
    with db() as conn:
        conn.execute('BEGIN')
        counts=conn.execute("SELECT SUM(CASE WHEN p.state='active' AND "+deadline+"<=? THEN 1 ELSE 0 END) AS due,SUM(CASE WHEN p.state='active' AND "+deadline+">? THEN 1 ELSE 0 END) AS upcoming,SUM(CASE WHEN p.state='paused' THEN 1 ELSE 0 END) AS paused"+base,(now,now)).fetchone()
        total=conn.execute('SELECT count(*)'+base+extra,args).fetchone()[0]
        rows=conn.execute('SELECT r.*'+base+extra+" ORDER BY CASE WHEN p.state='paused' THEN 2 WHEN "+deadline+"<=? THEN 0 ELSE 1 END,"+deadline+',p.updated,r.id LIMIT ? OFFSET ?',args+[now,20,(page-1)*20]).fetchall()
        items=[]
        for r in rows:
            placement=summary(conn,r['id'],now=now)
            category='paused' if placement['state']=='paused' else 'due' if placement['attention_due'] else 'upcoming'
            items.append({k:r[k] for k in ('id','user_id','channel','state','version','amount','hours')} | {
                'payment':ads.payment(conn,r['id']),'placement':placement,'queue_status':category,
                'overdue_seconds':max(0,now-placement['expected_stop_at']) if category=='due' else 0})
        return {'requests':items,'counts':{k:counts[k] or 0 for k in ('due','upcoming','paused')},
                'total':total,'page':page,'page_size':20,'view':view,'generated_at':now}
