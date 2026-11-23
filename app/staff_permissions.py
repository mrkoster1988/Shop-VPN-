"""Настраиваемые полномочия сотрудников. Неизвестные маршруты закрыты."""
import json
import re
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictStr
from .db import db

LABELS={
    'dashboard':'Сводка магазина',
    'customers':'Пользователи и подписки',
    'orders':'Заказы и повтор выдачи',
    'payments':'Ручное подтверждение и отмена оплаты',
    'catalog':'Тарифы и лимиты устройств',
    'promotions':'Промокоды, бонусы и пробный доступ',
    'starter':'Настройки автовыдачи WireGuard',
    'servers_read':'Состояние серверов и протоколы (просмотр)',
    'moderation':'Модерация заявок рекламы',
    'support':'Обращения и поддержка',
    'store_settings':'Настройки магазина и условия',
    'bot_menu':'Редактор меню Telegram',
    'analytics':'Финансовая аналитика',
    'diagnostics':'Диагностика',
}
ALLOWED={
    'administrator':frozenset(LABELS),
    'moderator':frozenset(('moderation','support')),
}
DEFAULTS={
    'administrator':frozenset(LABELS)-{'payments','bot_menu'},
    'moderator':frozenset(('moderation',)),
}
# Права разработчика на сотрудников, секреты, установку и обновление
# не входят в делегируемый набор. Не использовать startswith без границы.
RULES={
    'dashboard': [('GET',r'/dashboard-summary')],
    'customers': [('GET',r'/users(?:/[0-9]+)?')],
    'orders': [('GET',r'/orders'),('POST',r'/orders/[^/]+/retry')],
    'payments': [('POST',r'/orders/[^/]+/(?:confirm|reject)')],
    'catalog': [('GET',r'/plans'),('POST',r'/plans'),('PUT',r'/plans/[^/]+')],
    'promotions': [('GET',r'/(?:coupons|referrals|trial)'),('POST',r'/coupons'),
                   ('PUT',r'/(?:referrals|trial|coupons/[^/]+)')],
    'starter': [('GET',r'/flow/wireguard/auto(?:/batch)?'),('PUT',r'/flow/wireguard/auto'),
                ('POST',r'/flow/wireguard/auto/batch')],
    'servers_read': [('GET',r'/(?:servers|protocols)')],
    'moderation': [('GET',r'/moderation/requests'),('PUT',r'/moderation/requests/[^/]+')],
    'support': [('GET',r'/support/tickets(?:/[^/]+)?'),('POST',r'/support/tickets/[^/]+/messages'),
                ('PATCH',r'/support/tickets/[^/]+')],
    'store_settings': [('GET',r'/shop-settings'),('PUT',r'/shop-settings')],
    'bot_menu': [('GET',r'/bot-menu'),('PUT',r'/bot-menu')],
    'analytics': [('GET',r'/commerce-analytics')],
    'diagnostics': [('GET',r'/diagnostics')],
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


def permits(values,path,method):
    if path=='/api/admin/auth/me' and method in ('GET','HEAD'):
        return True
    if not path.startswith('/api/admin/'):
        return False
    tail=path[len('/api/admin'):]
    return any(verb==method and re.fullmatch(pattern,tail)
               for group in values for verb,pattern in RULES.get(group,()))
