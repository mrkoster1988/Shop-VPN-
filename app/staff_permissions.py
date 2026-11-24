"""Настраиваемые полномочия сотрудников. Неизвестные маршруты закрыты."""
import json
import re
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictStr
from .db import db

LABELS={
    'dashboard':'Сводка магазина',
    'customers':'Пользователи и подписки',
    'orders':'Заказы и повтор выдачи',
    'payments':'Ручное подтверждение и отмена оплаты',
    'payment_settings':'Настройки способов оплаты',
    'finance_read':'Финансовый журнал (просмотр)',
    'finance_write':'Учёт расходов, комиссий и подтверждённых возвратов',
    'catalog':'Тарифы и лимиты устройств',
    'promotions':'Промокоды, бонусы и пробный доступ',
    'starter':'Настройки автовыдачи WireGuard',
    'servers_read':'Состояние серверов и протоколы (просмотр)',
    'server_routing':'Управление распределением и мониторингом узлов',
    'moderation':'Модерация заявок рекламы',
    'support':'Обращения и поддержка',
    'store_settings':'Настройки магазина, условия и оформление',
    'sales_controls':'Правила продаж и продления',
    'notifications':'Шаблоны и расписание уведомлений',
    'bot_menu':'Редактор меню Telegram',
    'analytics':'Финансовая аналитика',
    'diagnostics':'Диагностика',
    'ai_read':'Pushok ИИ: рекомендации в разрешённых разделах',
    'ai_scan':'Pushok ИИ: запуск локального анализа',
    'ai_review':'Pushok ИИ: решения по рекомендациям',
}
ALLOWED={
    'administrator':frozenset(LABELS),
    'moderator':frozenset(('moderation','support')),
}
DEFAULTS={
    'administrator':frozenset(LABELS)-{'payments','bot_menu','sales_controls','notifications','payment_settings','finance_read','finance_write','server_routing','ai_read','ai_scan','ai_review'},
    'moderator':frozenset(('moderation',)),
}
# Права разработчика на сотрудников, секреты, установку и обновление
# не входят в делегируемый набор. Не использовать startswith без границы.
RULES={
    'dashboard': [('GET',r'/dashboard-summary')],
    'customers': [('GET',r'/users(?:/[0-9]+)?')],
    'orders': [('GET',r'/orders'),('POST',r'/orders/[^/]+/retry')],
    'payments': [('POST',r'/orders/[^/]+/(?:confirm|reject)')],
    'payment_settings': [('GET',r'/payment-policy'),('PUT',r'/payment-policy')],
    'finance_read': [('GET',r'/shop-finance')],
    'finance_write': [('POST',r'/shop-finance/entries'),('POST',r'/shop-finance/entries/[^/]+/void')],
    'catalog': [('GET',r'/plans'),('POST',r'/plans'),('PUT',r'/plans/[^/]+')],
    'promotions': [('GET',r'/(?:coupons|referrals|trial)'),('POST',r'/coupons'),
                   ('PUT',r'/(?:referrals|trial|coupons/[^/]+)')],
    'starter': [('GET',r'/flow/wireguard/auto(?:/batch)?'),('PUT',r'/flow/wireguard/auto'),
                ('POST',r'/flow/wireguard/auto/batch')],
    'servers_read': [('GET',r'/(?:servers|protocols|server-routing)'),('GET',r'/server-routing/history/[^/]+')],
    'server_routing': [('PUT',r'/server-routing/policy'),('PUT',r'/server-routing/nodes/[^/]+')],
    'moderation': [('GET',r'/moderation/requests'),('PUT',r'/moderation/requests/[^/]+')],
    'support': [('GET',r'/support/tickets(?:/[^/]+)?'),('POST',r'/support/tickets/[^/]+/messages'),
                ('PATCH',r'/support/tickets/[^/]+')],
    'store_settings': [('GET',r'/shop-settings'),('PUT',r'/shop-settings'),
                       ('GET',r'/runtime/appearance'),('PUT',r'/runtime/appearance')],
    'sales_controls': [('GET',r'/runtime/sales'),('PUT',r'/runtime/sales')],
    'notifications': [('GET',r'/runtime/notifications'),('PUT',r'/runtime/notifications')],
    'bot_menu': [('GET',r'/bot-menu'),('PUT',r'/bot-menu')],
    'analytics': [('GET',r'/commerce-analytics')],
    'diagnostics': [('GET',r'/diagnostics')],
    'ai_read': [('GET',r'/pushok-ai'),('GET',r'/pushok-ai/history')],
    'ai_scan': [('POST',r'/pushok-ai/scan')],
    'ai_review': [('POST',r'/pushok-ai/findings/[^/]+/review')],
}

class PolicyUpdate(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    permissions:list[StrictStr]=Field(max_length=len(LABELS))


def policy(conn, role):
    if role not in ALLOWED:
        raise HTTPException(404,'Роль не найдена')
    row=conn.execute('SELECT permissions,version FROM staff_role_policies WHERE role=?',(role,)).fetchone()
    if not row:
        return {'role':role,'version':0,'permissions':sorted(DEFAULTS[role]),'allowed':sorted(ALLOWED[role])}
    try:
        values=json.loads(row['permissions'])
        if not isinstance(values,list) or any(type(v) is not str for v in values) or not set(values)<=ALLOWED[role]:
            raise ValueError('Invalid permissions')
    except (TypeError,ValueError):
        # Повреждённая политика не возвращает расширенные права по умолчанию.
        values=[]
    return {'role':role,'version':row['version'],'permissions':sorted(set(values)),'allowed':sorted(ALLOWED[role])}


def get_all():
    with db() as conn:
        return {'labels':LABELS,'roles':[policy(conn,r) for r in ALLOWED]}


def update(role,data):
    if role not in ALLOWED:
        raise HTTPException(404,'Роль не найдена')
    values=set(data.permissions)
    if len(values)!=len(data.permissions) or not values<=ALLOWED[role]:
        raise HTTPException(422,'Недопустимое полномочие для этой роли')
    if 'payments' in values and 'orders' not in values:
        raise HTTPException(422,'Для подтверждения оплаты включите доступ к заказам')
    if 'finance_write' in values and 'finance_read' not in values:
        raise HTTPException(422,'Для учёта операций разрешите просмотр финансового журнала')
    if 'server_routing' in values and 'servers_read' not in values:
        raise HTTPException(422,'Для управления распределением разрешите просмотр серверов')
    if {'ai_scan','ai_review'} & values and 'ai_read' not in values:
        raise HTTPException(422,'Для работы с рекомендациями разрешите просмотр Pushok ИИ')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=policy(conn,role)
        if old['version']!=data.version:
            raise HTTPException(409,'Права уже изменены. Обновите раздел.')
        conn.execute("""INSERT INTO staff_role_policies(role,permissions,version,updated)
            VALUES (?,?,?,?) ON CONFLICT(role) DO UPDATE SET permissions=excluded.permissions,
            version=excluded.version,updated=excluded.updated""",
            (role,json.dumps(sorted(values),separators=(',',':')),data.version+1,int(time.time())))
        return policy(conn,role)


def permissions(conn,role):
    return policy(conn,role)['permissions'] if role in ALLOWED else []


class IndividualUpdate(BaseModel):
    model_config={'extra':'forbid'}
    version:int=Field(ge=0,strict=True)
    role_version:int=Field(ge=0,strict=True)
    role:Literal['administrator','moderator']
    inherit:bool=Field(strict=True)
    permissions:list[StrictStr]=Field(max_length=len(LABELS))


def _individual_row(conn,staff_id):
    return conn.execute('SELECT permissions,version,updated FROM staff_permission_limits WHERE staff_id=?',
                        (staff_id,)).fetchone()


def _limit_values(row):
    if not row or row['permissions'] is None:return None
    try:
        values=json.loads(row['permissions'])
        if not isinstance(values,list) or any(type(v) is not str for v in values) or not set(values)<=set(LABELS):
            raise ValueError('Invalid individual limits')
        return sorted(set(values))
    except (TypeError,ValueError):
        # При повреждении ограничения закрываем все делегированные полномочия.
        return []


def effective_permissions(conn,role,staff_id):
    allowed=set(permissions(conn,role))
    values=_limit_values(_individual_row(conn,staff_id))
    if values is not None:allowed.intersection_update(values)
    if 'orders' not in allowed:allowed.discard('payments')
    if 'finance_read' not in allowed:allowed.discard('finance_write')
    if 'servers_read' not in allowed:allowed.discard('server_routing')
    if 'ai_read' not in allowed:allowed.difference_update(('ai_scan','ai_review'))
    return sorted(allowed)


def individual(conn,staff_id):
    row=conn.execute("""SELECT a.role FROM staff_role_assignments a
        JOIN staff_users u ON u.id=a.staff_id WHERE u.id=?""",(staff_id,)).fetchone()
    if not row or row['role'] not in ALLOWED:
        raise HTTPException(404,'Настройки сотрудника недоступны для этой роли')
    role=row['role'];saved=_individual_row(conn,staff_id);values=_limit_values(saved)
    role_policy=policy(conn,role)
    allowed=role_policy['permissions']
    return {'staff_id':staff_id,'role':role,'version':saved['version'] if saved else 0,'role_version':role_policy['version'],
            'inherit':values is None,'allowed':allowed,'permissions':values or [],
            'effective':effective_permissions(conn,role,staff_id),'labels':LABELS}


def individual_get(staff_id):
    with db() as conn:
        conn.execute('BEGIN')
        return individual(conn,staff_id)


def individual_update(staff_id,data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old=individual(conn,staff_id)
        if old['role']!=data.role or old['version']!=data.version or old['role_version']!=data.role_version:
            raise HTTPException(409,'Роль или полномочия изменились. Обновите карточку.')
        values=set(data.permissions)
        if len(values)!=len(data.permissions) or not values<=set(old['allowed']) or (data.inherit and values):
            raise HTTPException(422,'Индивидуальные права могут только ограничивать текущую роль')
        if 'payments' in values and 'orders' not in values:
            raise HTTPException(422,'Для подтверждения оплаты разрешите просмотр заказов')
        if 'finance_write' in values and 'finance_read' not in values:
            raise HTTPException(422,'Для учёта операций разрешите просмотр финансового журнала')
        if 'server_routing' in values and 'servers_read' not in values:
            raise HTTPException(422,'Для управления распределением разрешите просмотр серверов')
        if {'ai_scan','ai_review'} & values and 'ai_read' not in values:
            raise HTTPException(422,'Для работы с рекомендациями разрешите просмотр Pushok ИИ')
        encoded=None if data.inherit else json.dumps(sorted(values),separators=(',',':'))
        conn.execute("""INSERT INTO staff_permission_limits(staff_id,permissions,version,updated)
            VALUES (?,?,?,?) ON CONFLICT(staff_id) DO UPDATE SET permissions=excluded.permissions,
            version=excluded.version,updated=excluded.updated""",
            (staff_id,encoded,data.version+1,int(time.time())))
        return individual(conn,staff_id)


def invalidate_on_role_change(conn,staff_id):
    # Даже если роль затем вернётся, запрос старой версии не сможет снять ограничения.
    conn.execute("""INSERT INTO staff_permission_limits(staff_id,permissions,version,updated)
        VALUES (?,NULL,1,?) ON CONFLICT(staff_id) DO UPDATE SET version=version+1,
        updated=excluded.updated""",(staff_id,int(time.time())))


def permits(values,path,method):
    if path=='/api/admin/auth/me' and method in ('GET','HEAD'):
        return True
    if not path.startswith('/api/admin/'):
        return False
    tail=path[len('/api/admin'):]
    return any(verb==method and re.fullmatch(pattern,tail)
               for group in values for verb,pattern in RULES.get(group,()))
