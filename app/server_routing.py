"""Выбор узла только для новых привязок; существующие подписки не мигрируют."""
import asyncio
import logging
import time
from fractions import Fraction
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .db import db, one

log=logging.getLogger('pushok')
PROTOCOLS=('xui','wg','vmess','hysteria2','tuic')
_inflight=set()


class PolicyUpdate(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    strategy:Literal['least_count','utilization']
    reserve_enabled:bool=Field(strict=True)
    require_recent:bool=Field(strict=True)
    max_age_minutes:int=Field(ge=1,le=1440,strict=True)
    monitor_enabled:bool=Field(strict=True)
    monitor_interval_minutes:int=Field(ge=1,le=1440,strict=True)
    monitor_batch:int=Field(ge=1,le=4,strict=True)


class NodeUpdate(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    server_version:int=Field(ge=1,strict=True)
    priority:int=Field(ge=0,le=1000,strict=True)
    reserve:bool=Field(strict=True)
    accept_new:bool=Field(strict=True)


DEFAULT={'version':0,'strategy':'least_count','reserve_enabled':True,
    'require_recent':False,'max_age_minutes':60,'monitor_enabled':False,
    'monitor_interval_minutes':5,'monitor_batch':2,'updated':0}


def policy(conn):
    row=conn.execute('SELECT * FROM server_routing_policy WHERE id=1').fetchone()
    if row is None:return dict(DEFAULT)
    result={k:row[k] for k in DEFAULT}
    for key in ('reserve_enabled','require_recent','monitor_enabled'):result[key]=bool(result[key])
    return result


def update_policy(data):
    values=data.model_dump(exclude={'version'})
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=policy(conn)
        if old['version']!=data.version:raise HTTPException(409,'Правила распределения изменились. Обновите раздел.')
        if any(old[k]!=v for k,v in values.items()):
            conn.execute("""INSERT INTO server_routing_policy
                (id,strategy,reserve_enabled,require_recent,max_age_minutes,monitor_enabled,
                 monitor_interval_minutes,monitor_batch,version,updated)
                VALUES (1,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                strategy=excluded.strategy,reserve_enabled=excluded.reserve_enabled,
                require_recent=excluded.require_recent,max_age_minutes=excluded.max_age_minutes,
                monitor_enabled=excluded.monitor_enabled,monitor_interval_minutes=excluded.monitor_interval_minutes,
                monitor_batch=excluded.monitor_batch,version=excluded.version,updated=excluded.updated""",
                (data.strategy,int(data.reserve_enabled),int(data.require_recent),data.max_age_minutes,
                 int(data.monitor_enabled),data.monitor_interval_minutes,data.monitor_batch,data.version+1,int(time.time())))
        return policy(conn)


def update_node(sid,data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        server=conn.execute('SELECT id,version FROM servers WHERE id=?',(sid,)).fetchone()
        if not server:raise HTTPException(404,'Сервер не найден')
        row=conn.execute('SELECT * FROM server_routing_nodes WHERE server_id=?',(sid,)).fetchone()
        version=row['version'] if row else 0
        if data.server_version!=server['version'] or version!=data.version:
            raise HTTPException(409,'Сервер или приоритет изменился. Обновите список.')
        old=(row['priority'],bool(row['reserve']),bool(row['accept_new'])) if row else (100,False,True)
        if old!=(data.priority,data.reserve,data.accept_new):
            conn.execute("""INSERT INTO server_routing_nodes(server_id,priority,reserve,accept_new,version,updated)
                VALUES (?,?,?,?,?,?) ON CONFLICT(server_id) DO UPDATE SET priority=excluded.priority,
                reserve=excluded.reserve,accept_new=excluded.accept_new,version=excluded.version,updated=excluded.updated""",
                (sid,data.priority,int(data.reserve),int(data.accept_new),version+1,int(time.time())))
        return {'ok':True,'version':version+int(old!=(data.priority,data.reserve,data.accept_new))}


def rows(conn,protocol=None,now=None):
    now=int(time.time()) if now is None else now
    query="""SELECT v.*,
        COALESCE(r.priority,100) AS priority,COALESCE(r.reserve,0) AS reserve,
        COALESCE(r.accept_new,1) AS accept_new,COALESCE(r.version,0) AS routing_version,
        (SELECT count(*) FROM subscriptions s WHERE s.server_id=v.id AND
          (s.state='pending' OR (s.state='active' AND s.expires>?))) AS occupied
        FROM servers v LEFT JOIN server_routing_nodes r ON r.server_id=v.id"""
    args=[now]
    if protocol is not None:query+=' WHERE v.protocol=?';args.append(protocol)
    return [dict(r) for r in conn.execute(query,args)]


def blocked_reason(row,p,now):
    if row['state']!='active':return 'not_active'
    if not row['accept_new']:return 'new_assignments_paused'
    if row['health']=='error':return 'api_error'
    if row['capacity'] and row['occupied']>=row['capacity']:return 'capacity'
    if p['require_recent'] and (row['health']!='ok' or row['checked_at'] is None
        or not 0<=now-row['checked_at']<=p['max_age_minutes']*60):return 'stale_check'
    if row['reserve'] and not p['reserve_enabled']:return 'reserve_disabled'
    return None


def order_key(row,p):
    if p['strategy']=='utilization':
        # Не сравниваем процент с безлимитным узлом: ограниченные узлы сначала,
        # затем безлимитные по числу занятых подписок, внутри одной группы приоритета.
        metric=(row['capacity']==0,Fraction(row['occupied'],row['capacity'] or 1))
    else:metric=(False,Fraction(row['occupied']))
    return (bool(row['reserve']),-row['priority'],*metric,row['created'],row['id'])


def candidates(conn,protocol,now=None):
    now=int(time.time()) if now is None else now
    p=policy(conn)
    eligible=[r for r in rows(conn,protocol,now) if blocked_reason(r,p,now) is None]
    return sorted(eligible,key=lambda r:order_key(r,p))


def report():
    now=int(time.time())
    with db() as conn:
        conn.execute('BEGIN')
        p=policy(conn);nodes=rows(conn,now=now)
        result=[]
        for row in sorted(nodes,key=lambda r:(r['protocol'],order_key(r,p))):
            # Не выдаём зашифрованный профиль, адрес панели или идентификаторы клиентов.
            last=conn.execute("""SELECT checked_at,status,duration_ms,source FROM server_probe_history
                WHERE server_id=? AND server_version=? ORDER BY id DESC LIMIT 1""",
                (row['id'],row['version'])).fetchone()
            result.append({k:row[k] for k in ('id','name','location','protocol','state','health',
                'checked_at','capacity','occupied','priority','routing_version','version')}|
                {'reserve':bool(row['reserve']),'accept_new':bool(row['accept_new']),
                 'available_slots':max(0,row['capacity']-row['occupied']) if row['capacity'] else None,
                 'blocked_reason':blocked_reason(row,p,now),'last_probe':dict(last) if last else None})
        choices={}
        for proto in PROTOCOLS:
            available=sorted((r for r in nodes if r['protocol']==proto and blocked_reason(r,p,now) is None),key=lambda r:order_key(r,p))
            choices[proto]=available[0]['id'] if available else None
        return {'policy':p,'nodes':result,'next_assignment':choices,'generated_at':now,
                'scope':'subscription_slots_and_cached_api_health','traffic_verified':False}


def history(sid):
    with db() as conn:
        if not conn.execute('SELECT 1 FROM servers WHERE id=?',(sid,)).fetchone():
            raise HTTPException(404,'Сервер не найден')
        rows=conn.execute("""SELECT checked_at,status,duration_ms,source,server_version
            FROM server_probe_history WHERE server_id=? ORDER BY id DESC LIMIT 20""",(sid,)).fetchall()
        return {'server_id':sid,'checks':[dict(r) for r in rows]}


async def probe(sid,*,automatic=False,expected_policy_version=None):
    """Одна проверка на узел в процессе; результат старого профиля отбрасывается."""
    from . import providers
    if sid in _inflight:raise HTTPException(409,'Проверка этого сервера уже выполняется')
    _inflight.add(sid)
    try:
        row=one('SELECT * FROM servers WHERE id=?',(sid,))
        if not row:raise HTTPException(404,'Сервер не найден')
        if automatic and row['state'] not in ('active','draining'):return False
        started=time.monotonic()
        try:
            await asyncio.wait_for(providers.probe_server(row),timeout=30)
            health,error='ok',None
        except Exception:
            health,error='error','ProviderCheckFailed'
        duration=min(30000,max(0,int((time.monotonic()-started)*1000)))
        checked=int(time.time())
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if automatic:
                current=policy(conn)
                if not current['monitor_enabled'] or current['version']!=expected_policy_version:return False
            changed=conn.execute("""UPDATE servers SET health=?,checked_at=?,error_code=?
                WHERE id=? AND version=?""",(health,checked,error,sid,row['version'])).rowcount
            if not changed:
                if automatic:return False
                raise HTTPException(409,'Настройки изменились во время проверки. Запустите проверку заново.')
            conn.execute("""INSERT INTO server_probe_history(server_id,server_version,checked_at,status,duration_ms,source)
                VALUES (?,?,?,?,?,?)""",(sid,row['version'],checked,health,duration,'automatic' if automatic else 'manual'))
            conn.execute("""DELETE FROM server_probe_history WHERE server_id=? AND id NOT IN
                (SELECT id FROM server_probe_history WHERE server_id=? ORDER BY id DESC LIMIT 100)""",(sid,sid))
        return True
    finally:_inflight.discard(sid)


async def run_due(now=None):
    now=int(time.time()) if now is None else now
    with db() as conn:
        p=policy(conn)
        if not p['monitor_enabled']:return 0
        ids=[r['id'] for r in conn.execute("""SELECT id FROM servers WHERE state IN ('active','draining')
            AND (checked_at IS NULL OR checked_at>? OR checked_at<=?)
            ORDER BY COALESCE(checked_at,0),created,id LIMIT ?""",
            (now,now-p['monitor_interval_minutes']*60,p['monitor_batch']))]
    completed=0
    for sid in ids:
        with db() as conn:
            current=policy(conn)
            if not current['monitor_enabled'] or current['version']!=p['version']:break
        try:
            completed+=int(await probe(sid,automatic=True,expected_policy_version=p['version']))
        except HTTPException as exc:
            if exc.status_code not in (404,409):raise
    return completed


async def worker():
    # Отдельная задача: медленная панель не задерживает выдачу и отзыв подписок.
    while True:
        try:await run_due()
        except asyncio.CancelledError:raise
        except Exception as exc:log.warning('Server monitor iteration failed: %s',type(exc).__name__)
        await asyncio.sleep(15)
