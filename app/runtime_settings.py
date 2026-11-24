"""Независимые версии эксплуатационных настроек; без записи секретов окружения."""
import json
import time
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, ValidationError
from . import protocols
from .db import db

PROTOCOLS=('xui','wg','vmess','hysteria2','tuic')


class TextSettings(BaseModel):
    model_config={'extra':'forbid'}

    @field_validator('*',mode='before')
    @classmethod
    def safe_text(cls,value):
        if isinstance(value,str):
            if any(ord(ch)<32 and ch!='\n' or ord(ch)==127 for ch in value):
                raise ValueError('Текст не должен содержать управляющие символы')
            return value.strip()
        return value


class Appearance(TextSettings):
    brand_name:str=Field(default='pushok.vpn',min_length=1,max_length=60,strict=True)
    headline:str=Field(default='',max_length=100,strict=True)
    description:str=Field(default='',max_length=500,strict=True)
    announcement:str=Field(default='',max_length=500,strict=True)
    theme:Literal['pushok','ocean','graphite','violet']='pushok'

    @field_validator('brand_name','headline')
    @classmethod
    def one_line(cls,value):
        if '\n' in value:raise ValueError('Введите текст в одну строку')
        return value


class Sales(TextSettings):
    new_sales:bool=Field(default=True,strict=True)
    renewals:bool=Field(default=True,strict=True)
    web_sales:bool=Field(default=True,strict=True)
    telegram_sales:bool=Field(default=True,strict=True)
    enabled_protocols:list[protocols.SalesProtocol]=Field(default_factory=lambda:list(PROTOCOLS),max_length=len(PROTOCOLS))
    closed_notice:str=Field(default='Продажа этого подключения временно приостановлена. Обратитесь в поддержку.',min_length=1,max_length=500,strict=True)

    @field_validator('enabled_protocols')
    @classmethod
    def unique_protocols(cls,values):
        if len(values)!=len(set(values)):raise ValueError('Протоколы не должны повторяться')
        return [p for p in PROTOCOLS if p in values]


class Notifications(TextSettings):
    order_ready_enabled:bool=Field(default=True,strict=True)
    expiry_enabled:bool=Field(default=True,strict=True)
    expiry_hours:int=Field(default=24,ge=1,le=168,strict=True)
    purchase_text:str=Field(default='Оплата подтверждена. VPN готов! Подключение находится в личном кабинете.',min_length=1,max_length=1000,strict=True)
    trial_text:str=Field(default='Пробный VPN готов! Настройки находятся в личном кабинете.',min_length=1,max_length=1000,strict=True)
    bonus_text:str=Field(default='Бонусные дни добавлены к подписке.',min_length=1,max_length=1000,strict=True)
    expiry_text:str=Field(default='Срок вашей подписки заканчивается. Продлите её в магазине.',min_length=1,max_length=1000,strict=True)


class AppearanceUpdate(Appearance):
    version:int=Field(ge=0,strict=True)

class SalesUpdate(Sales):
    version:int=Field(ge=0,strict=True)

class NotificationsUpdate(Notifications):
    version:int=Field(ge=0,strict=True)


MODELS={'appearance':Appearance,'sales':Sales,'notifications':Notifications}


def read(conn,group):
    model=MODELS[group]
    row=conn.execute('SELECT content,version,updated FROM shop_runtime_settings WHERE section=?',(group,)).fetchone()
    if not row:
        return model().model_dump() | {'version':0,'updated':0}
    try:
        values=model.model_validate_json(row['content']).model_dump()
    except (ValidationError,ValueError):
        # Не возвращаем расширенные настройки продаж при повреждении сохранённой политики.
        raise HTTPException(503,'Настройки раздела временно недоступны') from None
    return values | {'version':row['version'],'updated':row['updated']}


def get(group):
    with db() as conn:return read(conn,group)


def update(group,data):
    model=MODELS[group]
    values=model.model_validate(data.model_dump(exclude={'version'})).model_dump()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current=read(conn,group)
        if current['version']!=data.version:
            raise HTTPException(409,'Настройки уже изменены. Обновите раздел.')
        if any(current[k]!=v for k,v in values.items()):
            conn.execute("""INSERT INTO shop_runtime_settings(section,content,version,updated) VALUES (?,?,?,?)
                ON CONFLICT(section) DO UPDATE SET content=excluded.content,version=excluded.version,updated=excluded.updated""",
                (group,json.dumps(values,ensure_ascii=False,separators=(',',':')),data.version+1,int(time.time())))
        return read(conn,group)


def sales_decision(conn,user,protocol,policy=None):
    """Правила новых платных заказов; существующий заказ не переоценивается."""
    p=policy if policy is not None else read(conn,'sales')
    if protocol not in p['enabled_protocols']:
        return False,'protocol_paused'
    if user and user['telegram_id']:
        if not p['telegram_sales']:return False,'channel_paused'
    elif not p['web_sales']:
        return False,'channel_paused'
    existing=bool(user and conn.execute('SELECT 1 FROM subscriptions WHERE user_id=? AND protocol=?',
                                        (user['id'],protocol)).fetchone())
    if existing and not p['renewals']:return False,'renewal_paused'
    if not existing and not p['new_sales']:return False,'new_sales_paused'
    return True,None


def require_sale(conn,user,protocol):
    p=read(conn,'sales')
    allowed,_=sales_decision(conn,user,protocol,p)
    if not allowed:raise HTTPException(409,p['closed_notice'])


def catalog_options(user,available):
    """Снимок коммерческой политики. Пробный доступ имеет свою отдельную политику."""
    with db() as conn:
        conn.execute('BEGIN')
        p=read(conn,'sales')
        result=[]
        for item in protocols.sales_options():
            ok,reason=sales_decision(conn,user,item['id'],p)
            # Избегаем внешних вызовов из транзакции; available читает локальный реестр.
            ready=available(item['id'],user['id'] if user else None)
            result.append({**item,'available':bool(ok and ready),'sales_allowed':ok,
                           'sale_reason':reason,'trial_available':bool(ready)})
        return result,p['closed_notice']
