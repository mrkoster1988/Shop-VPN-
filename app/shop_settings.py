"""Владелец-редактируемые публичные настройки магазина."""
import time
from fastapi import HTTPException
from pydantic import BaseModel,Field
from .db import db

class SettingsUpdate(BaseModel):
    model_config={'extra':'forbid'}
    support_text: str=Field(max_length=1000,strict=True)
    terms_text: str=Field(max_length=5000,strict=True)
    version: int=Field(ge=1,strict=True)


def get():
    with db() as conn:
        row=conn.execute('SELECT support_text,terms_text,version,updated FROM store_settings WHERE id=1').fetchone()
    if not row:
        raise HTTPException(503,'Настройки магазина ещё не инициализированы')
    return dict(row)


def terms():
    return get()['terms_text']


def terms_configured():
    return bool(terms().strip())


def public():
    row=get()
    return {'support':row['support_text'] or 'Напишите администратору магазина.',
            'terms':row['terms_text'] or 'Условия продажи ещё не заполнены владельцем магазина.'}


def update(data):
    now=int(time.time())
    changed=False
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT support_text,terms_text,version FROM store_settings WHERE id=1').fetchone()
        if not row:
            raise HTTPException(503,'Настройки магазина ещё не инициализированы')
        if row['version']!=data.version:
            raise HTTPException(409,'Настройки магазина уже изменены. Обновите раздел и повторите.')
        if row['support_text']!=data.support_text or row['terms_text']!=data.terms_text:
            conn.execute('UPDATE store_settings SET support_text=?,terms_text=?,version=version+1,updated=? WHERE id=1',
                         (data.support_text,data.terms_text,now))
            changed=True
    return get()
