"""Плановые периоды MTProxy: резерв не запускает узел и не освобождает фактическое место."""
import hashlib
import json
import time
import uuid
from fastapi import HTTPException
from pydantic import BaseModel,Field,field_validator
from . import ads
from .db import db

class BaseCommand(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:uuid.UUID
    request_version:int=Field(ge=1,strict=True)
    payment_version:int=Field(ge=0,le=2,strict=True)
    booking_version:int=Field(ge=0,strict=True)
    note:str=Field(min_length=1,max_length=500)
    @field_validator('note')
    @classmethod
    def trim(cls,value):
        value=value.strip()
        if not value:raise ValueError('Укажите основание изменения брони')
        return value

class Reserve(BaseCommand):
    site_id:uuid.UUID
    site_version:int=Field(ge=1,strict=True)
    starts_at:int=Field(ge=1,strict=True)


def summary(conn,rid,private=False,now=None):
    row=conn.execute('SELECT * FROM ads_bookings WHERE request_id=?',(rid,)).fetchone()
    if not row:return None
    now=int(time.time()) if now is None else now
    result={k:row[k] for k in ('state','version','site_name','starts_at','ends_at','updated')}
    result.update(expired=row['state']=='reserved' and now>=row['ends_at'],
                  can_start=row['state']=='reserved' and row['starts_at']<=now<row['ends_at'])
    if private:
        result['site_id']=row['site_id']
        result['actions']=[{**dict(r),'details':json.loads(r['details'])} for r in conn.execute('SELECT action,note,details,created FROM ads_booking_actions WHERE request_id=? ORDER BY rowid DESC LIMIT 20',(rid,))]
    return result


def reserved(conn,rid):
    return bool(conn.execute("SELECT 1 FROM ads_bookings WHERE request_id=? AND state='reserved'",(rid,)).fetchone())


def check_interval(conn,sid,start,end,rid):
    if conn.execute("SELECT 1 FROM ads_bookings WHERE site_id=? AND state='reserved' AND request_id!=? AND starts_at<? AND ends_at>?",(sid,rid,end,start)).fetchone():
        raise HTTPException(409,'Полный период пересекается с другой бронью. Выберите другое время или площадку.')


def change(rid,action,data):
    fingerprint=hashlib.sha256(json.dumps(data.model_dump(mode='json'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM ads_booking_actions WHERE operation_id=?',(str(data.operation_id),)).fetchone()
        if old:
            if old['request_id']!=rid or old['action']!=action or old['fingerprint']!=fingerprint:raise HTTPException(409,'UUID уже использован для другого изменения брони.')
            return summary(conn,rid,True)
        request=conn.execute('SELECT * FROM ads_requests WHERE id=?',(rid,)).fetchone()
        if not request:raise HTTPException(404,'Заявка не найдена.')
        previous=conn.execute('SELECT * FROM ads_bookings WHERE request_id=?',(rid,)).fetchone()
        if request['version']!=data.request_version or ads.payment(conn,rid)['version']!=data.payment_version or (previous['version'] if previous else 0)!=data.booking_version:
            raise HTTPException(409,'Данные изменились. Обновите заявку и бронь.')
        now=int(time.time())
        if action in ('reserve','reschedule'):
            if request['state']!='approved' or ads.payment(conn,rid)['status']!='confirmed':raise HTTPException(409,'Нужны одобрение и учтённое поступление без возврата.')
            if conn.execute('SELECT 1 FROM ads_placements WHERE request_id=?',(rid,)).fetchone():raise HTTPException(409,'Для этой заявки уже начато размещение.')
            if action=='reschedule':
                if not previous or previous['state']!='reserved':raise HTTPException(409,'Перенос доступен только для неиспользованной брони.')
            elif previous and previous['state']!='canceled':raise HTTPException(409,'Сначала отмените прежнюю бронь.')
            if not now<data.starts_at<=now+365*86400:raise HTTPException(409,'Начало должно быть в будущем, не далее 365 дней.')
            site=conn.execute('SELECT * FROM ads_sites WHERE id=?',(str(data.site_id),)).fetchone()
            if not site or site['version']!=data.site_version or not site['enabled'] or not site['verified']:raise HTTPException(409,'Площадка выключена, не проверена или изменена.')
            if conn.execute("SELECT 1 FROM ads_placements WHERE site_id=? AND state IN ('active','paused')",(site['id'],)).fetchone():raise HTTPException(409,'Нельзя бронировать фактически занятую площадку, включая паузу.')
            end=data.starts_at+request['hours']*3600
            check_interval(conn,site['id'],data.starts_at,end,rid)
            conn.execute("INSERT INTO ads_bookings VALUES (?,?,?,?,?,'reserved',?,?,?) ON CONFLICT(request_id) DO UPDATE SET site_id=excluded.site_id,site_name=excluded.site_name,starts_at=excluded.starts_at,ends_at=excluded.ends_at,state=excluded.state,version=excluded.version,updated=excluded.updated",
                         (rid,site['id'],site['name'],data.starts_at,end,data.booking_version+1,now,now))
        elif action=='cancel':
            if not previous or previous['state']!='reserved':raise HTTPException(409,'Нет неиспользованной брони для отмены.')
            conn.execute("UPDATE ads_bookings SET state='canceled',version=version+1,updated=? WHERE request_id=?",(now,rid))
        else:raise HTTPException(422,'Неизвестное действие брони')
        current=conn.execute('SELECT * FROM ads_bookings WHERE request_id=?',(rid,)).fetchone()
        details={k:current[k] for k in ('site_id','site_name','starts_at','ends_at','state','version')}
        conn.execute('INSERT INTO ads_booking_actions VALUES (?,?,?,?,?,?,?)',(str(data.operation_id),rid,action,fingerprint,data.note,json.dumps(details,ensure_ascii=False),now))
        ads.event(conn,rid,'admin','booking_'+action,{'note':data.note,'booking':details})
        return summary(conn,rid,True)


def check_start(conn,rid,sid,now,seconds,version):
    row=conn.execute("SELECT * FROM ads_bookings WHERE request_id=? AND state='reserved'",(rid,)).fetchone()
    if row:
        if row['version']!=version or row['site_id']!=sid:raise HTTPException(409,'Нужны текущая версия брони и её площадка.')
        if not row['starts_at']<=now<row['ends_at']:raise HTTPException(409,'Окно старта ещё не наступило или уже истекло. Отмените или перенесите бронь.')
    elif version is not None:raise HTTPException(409,'Бронь изменена или отменена.')
    check_interval(conn,sid,now,now+seconds,rid)
    return row


def consume(conn,rid,now):
    conn.execute("UPDATE ads_bookings SET state='consumed',version=version+1,updated=? WHERE request_id=? AND state='reserved'",(now,rid))
    ads.event(conn,rid,'admin','booking_consumed',{'verification':'manual_start'})


def listing(view='reserved',page=1):
    now=int(time.time())
    clauses={'reserved':(" WHERE b.state='reserved'",[]),'expired':(" WHERE b.state='reserved' AND b.ends_at<=?",[now]),
             'all':('',[])}
    if view not in clauses:raise HTTPException(422,'Неизвестный фильтр бронирования')
    where,args=clauses[view]
    with db() as conn:
        conn.execute('BEGIN')
        total=conn.execute('SELECT count(*) FROM ads_bookings b'+where,args).fetchone()[0]
        rows=conn.execute('SELECT r.* FROM ads_bookings b JOIN ads_requests r ON r.id=b.request_id'+where+' ORDER BY b.starts_at,b.request_id LIMIT 20 OFFSET ?',args+[(page-1)*20]).fetchall()
        items=[ads.public(r,conn,private=True) | {'user_id':r['user_id']} for r in rows]
        from . import ads_placements as placements
        return {'requests':items,'sites':placements.sites(conn),'total':total,'page':page,'page_size':20,'view':view,'generated_at':now}
