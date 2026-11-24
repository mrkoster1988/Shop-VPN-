"""Pushok ИИ: локальный экспертный анализ, без LLM и исполнительных полномочий.

Только агрегаты и фиксированные правила. Решение сотрудника меняет статус
рекомендации, а не сервер, тариф, платёж или подписку.
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import diagnostics, server_routing, shop_finance
from .db import db

log=logging.getLogger('pushok')
CATEGORY_RIGHTS={'diagnostics':'diagnostics','servers':'servers_read',
                 'commerce':'analytics','finance':'finance_read'}
CATEGORIES=tuple(CATEGORY_RIGHTS)
MANUAL_COOLDOWN=30
ENGINE='local_rules_v1'
DEFAULT={'enabled':False,'automatic':False,'interval_minutes':60,
         'diagnostics_enabled':True,'servers_enabled':True,
         'commerce_enabled':False,'finance_enabled':False,
         'queue_age_minutes':10,'capacity_percent':85,
         'sales_days':30,'min_orders':20,'payment_rate_percent':30}


class Policy(BaseModel):
    model_config={'extra':'forbid'}
    enabled:bool=Field(strict=True)
    automatic:bool=Field(strict=True)
    interval_minutes:int=Field(ge=5,le=1440,strict=True)
    diagnostics_enabled:bool=Field(strict=True)
    servers_enabled:bool=Field(strict=True)
    commerce_enabled:bool=Field(strict=True)
    finance_enabled:bool=Field(strict=True)
    queue_age_minutes:int=Field(ge=1,le=1440,strict=True)
    capacity_percent:int=Field(ge=50,le=100,strict=True)
    sales_days:int=Field(ge=7,le=90,strict=True)
    min_orders:int=Field(ge=5,le=10000,strict=True)
    payment_rate_percent:int=Field(ge=1,le=100,strict=True)


class PolicyUpdate(Policy):
    version:int=Field(ge=0,strict=True)


class Review(BaseModel):
    model_config={'extra':'forbid'}
    operation_id:UUID
    version:int=Field(ge=1,strict=True)
    action:Literal['acknowledge','dismiss','reopen']
    reason:Literal['investigate','expected','later','not_applicable','revisit']


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def policy(conn):
    row=conn.execute('SELECT * FROM shop_ai_policy WHERE id=1').fetchone()
    if not row:return dict(DEFAULT)|{'version':0,'updated':0}
    try:
        parsed=Policy.model_validate_json(row['content'])
        if parsed.model_fields_set!=set(Policy.model_fields):raise ValueError('Incomplete policy')
    except (ValidationError,ValueError):
        raise HTTPException(503,'Политика Pushok ИИ недоступна') from None
    return parsed.model_dump()|{'version':row['version'],'updated':row['updated']}


def update_policy(data):
    values=data.model_dump(exclude={'version'})
    if data.enabled and not any(values[k+'_enabled'] for k in CATEGORIES):
        raise HTTPException(422,'Выберите хотя бы одно направление анализа')
    if data.automatic and not data.enabled:
        raise HTTPException(422,'Для автоматического анализа сначала включите помощника')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=policy(conn)
        if old['version']!=data.version:raise HTTPException(409,'Настройки изменились. Обновите раздел.')
        if any(old[k]!=v for k,v in values.items()):
            conn.execute("""INSERT INTO shop_ai_policy(id,content,version,updated)
                VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,version=excluded.version,updated=excluded.updated""",
                (encoded(values),data.version+1,int(time.time())))
        return policy(conn)


def categories_for(actor):
    if actor['role']=='owner':return CATEGORIES
    rights=set(actor.get('permissions',[]))
    return tuple(k for k,v in CATEGORY_RIGHTS.items() if v in rights)


def finding(code,category,severity,title,recommendation,evidence):
    return {'code':code,'category':category,'severity':severity,'title':title,
            'recommendation':recommendation,'evidence':evidence}


def collect(p,worker,now):
    """Все данные локальны; пользовательский текст, токены и конфиги не собираются."""
    out=[]
    def add(code,category,severity,title,recommendation,**evidence):
        out.append(finding(code,category,severity,title,recommendation,evidence))
    if p['diagnostics_enabled']:
        d=diagnostics.report(worker)
        state=d['worker']['state']
        if state!='recent_success' and state!='busy':
            add('worker_state','diagnostics','info' if state=='disabled' else 'warning',
                'Состояние обработчика очереди','Проверьте режим обслуживания и состояние worker. Не запускайте второй обработчик.',
                state=state)
        for key,queue in d['queues'].items():
            age=queue['oldest_age_seconds'] or 0
            if queue['with_error'] or (key!='payment_poll' and queue['pending'] and age>=p['queue_age_minutes']*60):
                add('queue_'+key,'diagnostics','warning','Очередь требует внимания',
                    'Проверьте ошибки и штатный повтор операции. Не подтверждайте оплату повторно.',
                    queue=key,pending=queue['pending'],errors=queue['with_error'],age_seconds=age)
        expiry=d['expiry']
        if expiry['pending'] and (expiry['oldest_age_seconds'] or 0)>=p['queue_age_minutes']*60:
            add('expiry_delay','diagnostics','critical','Задержка отключения истёкшего доступа',
                'Проверьте доступность исходной панели и штатную очередь отзыва. Не удаляйте записи вручную.',
                pending=expiry['pending'],age_seconds=expiry['oldest_age_seconds'])
        for code,title,message in (
            ('encryption_key_invalid','Проблема конфигурации шифрования','Проверьте исходный ключ и резервную копию. Не генерируйте замену поверх действующих данных.'),
            ('payment_settings_incomplete','Настройки оплаты неполны','Проверьте локальную конфигурацию и договор с провайдером; выполните тестовую оплату отдельно.'),
            ('telegram_sales_settings_incomplete','Telegram-продажи не готовы','Проверьте бота и условия продажи. Не включайте реальные платежи без приёмки.'),
            ('no_active_plans','Нет активных тарифов','Проверьте витрину, если продажи должны быть открыты.')):
            if code in d['warnings']:add(code,'diagnostics','warning',title,message,observed=1)
    if p['servers_enabled']:
        r=server_routing.report()
        for proto in server_routing.PROTOCOLS:
            nodes=[n for n in r['nodes'] if n['protocol']==proto and n['state']=='active' and n['accept_new']]
            if not nodes:continue
            eligible=sum(n['blocked_reason'] is None for n in nodes)
            if not eligible:
                add('routing_unavailable_'+proto,'servers','warning','Нет подходящего узла для новых назначений',
                    'Проверьте лимиты, приоритеты, резерв и свежесть API. Существующие подписки не переносить.',
                    protocol=proto,nodes=len(nodes),eligible=eligible)
            finite=[n for n in nodes if n['capacity']>0 and not n['reserve']]
            busy=[n for n in finite if n['occupied']*100>=n['capacity']*p['capacity_percent']]
            if busy:
                add('routing_capacity_'+proto,'servers','warning','Основные узлы приближаются к лимиту',
                    'Оцените свободные слоты и резерв. Увеличивайте лимит только после проверки реальной ёмкости панели.',
                    protocol=proto,near_limit=len(busy),finite_nodes=len(finite),threshold_percent=p['capacity_percent'])
        active=sum(n['state'] in ('active','draining') for n in r['nodes'])
        if active and not r['policy']['monitor_enabled']:
            add('monitor_paused','servers','info','Автомониторинг API выключен',
                'Проверьте настройки распределения; при необходимости включите ограниченный мониторинг после проверки панелей.',
                nodes=active)
    if p['commerce_enabled']:
        # Исключаем последние 24 часа, чтобы не считать свежие неоплаченные счета отказами.
        end=max(1,now-86400);start=max(0,end-p['sales_days']*86400)
        with db() as conn:
            row=conn.execute("""SELECT count(*) AS total,
                coalesce(sum(status IN ('paid','active')),0) AS paid FROM orders
                WHERE kind='purchase' AND method IN ('manual','yookassa','sbp','stars')
                  AND created>=? AND created<?""",(start,end)).fetchone()
        if row['total']>=p['min_orders'] and row['paid']*100<row['total']*p['payment_rate_percent']:
            add('payment_conversion','commerce','info','Низкая доля подтверждённых покупок',
                'Проверьте удобство оплаты, доступность способов, цены и причины отказов. Не меняйте тарифы без анализа.',
                orders=row['total'],paid=row['paid'],days=p['sales_days'],threshold_percent=p['payment_rate_percent'])
    if p['finance_enabled']:
        f=shop_finance.report(max(0,now-p['sales_days']*86400),now+1,include_entries=False)
        if f['missing_payment_timestamp']:
            add('finance_missing_dates','finance','info','Часть старых поступлений не имеет даты подтверждения',
                'Сверьте исторические оплаты с выписками. Не придумывайте дату или сумму.',
                orders=f['missing_payment_timestamp'])
        for currency in ('RUB','XTR'):
            totals=f['totals'][currency]
            if totals['net_recorded']<0:
                add('finance_negative_'+currency.lower(),'finance','warning','Учтённые вычеты превышают поступления за период',
                    'Сверьте поступления, комиссии, расходы и возвраты в этой валюте. Это не банковский баланс и не чистая прибыль.',
                    currency=currency,days=p['sales_days'],**totals)
    return out


def current_run(conn):
    row=conn.execute('SELECT * FROM shop_ai_state WHERE id=1').fetchone()
    return dict(row) if row else {'last_scan':None,'policy_version':None,'source':None,'scan_count':0}


def due(run,p,now,automatic):
    last=run['last_scan']
    return last is None or now-last >= (p['interval_minutes']*60 if automatic else MANUAL_COOLDOWN) or now<last


def event(conn,code,action,now,version,actor=None,reason=None,operation=None,fingerprint=None):
    conn.execute("""INSERT INTO shop_ai_events(code,action,created,version,staff_id,actor_role,reason,operation_id,fingerprint)
        VALUES (?,?,?,?,?,?,?,?,?)""",(code,action,now,version,actor.get('id') if actor else None,
        actor['role'] if actor else 'analysis',reason,operation,fingerprint))


def scan(worker,*,automatic=False,now=None):
    now=int(time.time()) if now is None else now
    with db() as conn:
        conn.execute('BEGIN')
        p=policy(conn);run=current_run(conn)
        if not p['enabled'] or (automatic and not p['automatic']):
            if automatic:return False
            raise HTTPException(409,'Pushok ИИ выключен')
        if not due(run,p,now,automatic):
            if automatic:return False
            raise HTTPException(429,'Повторите анализ через 30 секунд')
    # Никаких внешних запросов и изменений бизнес-таблиц.
    found=collect(p,worker,now)
    by_code={f['code']:f for f in found}
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        fresh=policy(conn);run=current_run(conn)
        if fresh['version']!=p['version'] or not fresh['enabled'] or (automatic and not fresh['automatic']):
            if automatic:return False
            raise HTTPException(409,'Политика изменилась во время анализа')
        if not due(run,fresh,now,automatic):
            if automatic:return False
            raise HTTPException(429,'Другой анализ уже завершён')
        previous={r['code']:r for r in conn.execute('SELECT * FROM shop_ai_findings')}
        for code,f in by_code.items():
            evidence=encoded(f['evidence']);old=previous.get(code)
            if old is None:
                conn.execute("""INSERT INTO shop_ai_findings
                    (code,category,severity,title,recommendation,evidence,state,version,first_seen,last_seen,occurrences)
                    VALUES (?,?,?,?,?,?,'open',1,?,?,1)""",
                    (code,f['category'],f['severity'],f['title'],f['recommendation'],evidence,now,now))
                event(conn,code,'detected',now,1)
            else:
                recurring=old['state']=='resolved'
                changed=recurring or any(old[k]!=f[k] for k in ('category','severity','title','recommendation')) or old['evidence']!=evidence
                version=old['version']+int(changed)
                conn.execute("""UPDATE shop_ai_findings SET category=?,severity=?,title=?,recommendation=?,evidence=?,
                    state=?,version=?,last_seen=?,occurrences=occurrences+? WHERE code=?""",
                    (f['category'],f['severity'],f['title'],f['recommendation'],evidence,
                     'open' if recurring else old['state'],version,now,int(recurring),code))
                if recurring:event(conn,code,'recurrence',now,version)
        for code,old in previous.items():
            if code not in by_code and old['state']!='resolved' and p[old['category']+'_enabled']:
                version=old['version']+1
                conn.execute("UPDATE shop_ai_findings SET state='resolved',version=? WHERE code=?",(version,code))
                event(conn,code,'resolved',now,version)
        conn.execute("""INSERT INTO shop_ai_state(id,last_scan,policy_version,source,scan_count)
            VALUES (1,?,?,?,1) ON CONFLICT(id) DO UPDATE SET last_scan=excluded.last_scan,
            policy_version=excluded.policy_version,source=excluded.source,scan_count=scan_count+1""",
            (now,p['version'],'automatic' if automatic else 'manual'))
    return True


def public_finding(row,p):
    return dict(row)|{'evidence':json.loads(row['evidence']),
                      'active':bool(p['enabled'] and p[row['category']+'_enabled'] and row['state']!='resolved')}


def status(actor):
    allowed=categories_for(actor)
    with db() as conn:
        conn.execute('BEGIN')
        p=policy(conn);run=current_run(conn);run.pop('id',None)
        findings=[public_finding(r,p) for r in conn.execute("""SELECT * FROM shop_ai_findings
            ORDER BY (state='resolved'),CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,code""")
            if r['category'] in allowed]
        return {'name':'Pushok ИИ','engine':ENGINE,'external_model_connected':False,
                'executes_business_actions':False,'policy':p,'run':run,
                'categories':list(allowed),'findings':findings}


def review(code,data,actor):
    fp=hashlib.sha256(encoded(data.model_dump(mode='json')).encode()).hexdigest()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        p=policy(conn)
        row=conn.execute('SELECT * FROM shop_ai_findings WHERE code=?',(code,)).fetchone()
        if not row or row['category'] not in categories_for(actor):
            raise HTTPException(404,'Рекомендация не найдена')
        old=conn.execute('SELECT * FROM shop_ai_events WHERE operation_id=?',(str(data.operation_id),)).fetchone()
        if old:
            if old['code']!=code or old['fingerprint']!=fp or old['staff_id']!=actor.get('id') or old['actor_role']!=actor['role']:
                raise HTTPException(409,'Идентификатор уже использован')
            return public_finding(row,p)
        if not p['enabled'] or not p[row['category']+'_enabled'] or row['state']=='resolved':
            raise HTTPException(409,'Рекомендация больше не активна')
        if data.version!=row['version']:raise HTTPException(409,'Рекомендация изменилась. Обновите раздел.')
        valid={
            'acknowledge':('acknowledged',('open',),('investigate',)),
            'dismiss':('dismissed',('open','acknowledged'),('expected','later','not_applicable')),
            'reopen':('open',('acknowledged','dismissed'),('revisit',)),
        }
        target,states,reasons=valid[data.action]
        if data.reason not in reasons:raise HTTPException(422,'Выберите допустимую причину')
        if row['state'] not in states:raise HTTPException(409,'Решение уже принято')
        version=row['version']+1
        conn.execute('UPDATE shop_ai_findings SET state=?,version=? WHERE code=?',(target,version,code))
        event(conn,code,data.action,int(time.time()),version,actor,data.reason,str(data.operation_id),fp)
        return public_finding(conn.execute('SELECT * FROM shop_ai_findings WHERE code=?',(code,)).fetchone(),p)


def history(actor,page=1):
    allowed=categories_for(actor)
    if not allowed:return {'events':[],'page':page,'page_size':25,'total':0}
    placeholders=','.join('?' for _ in allowed)
    with db() as conn:
        conn.execute('BEGIN')
        where=' FROM shop_ai_events e JOIN shop_ai_findings f ON f.code=e.code WHERE f.category IN ('+placeholders+')'
        total=conn.execute('SELECT count(*)'+where,allowed).fetchone()[0]
        rows=conn.execute('SELECT e.code,e.action,e.created,e.version,e.staff_id,e.actor_role,e.reason,f.title'+where+
            ' ORDER BY e.id DESC LIMIT 25 OFFSET ?',(*allowed,(page-1)*25)).fetchall()
        return {'events':[dict(r) for r in rows],'page':page,'page_size':25,'total':total}


async def worker():
    from . import service
    while True:
        try:
            # Анализ агрегатов не задерживает asyncio-обработчик платежей.
            await asyncio.to_thread(scan,dict(service.worker_health),automatic=True)
        except asyncio.CancelledError:raise
        except Exception as exc:log.warning('Pushok advisor scan failed: %s',type(exc).__name__)
        await asyncio.sleep(30)
