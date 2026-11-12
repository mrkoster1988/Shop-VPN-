"""Редактор Telegram-меню без внешних запросов и изменения платежных сценариев."""
import json
import re
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import config as c
from .db import db
from .pushok_button_icons import decorate, configured_icons

IDS = ('buy', 'account', 'support', 'terms', 'link1', 'link2')
LABELS = ('Купить / продлить VPN', 'Открыть кабинет в браузере',
          'Поддержка', 'Условия продажи', 'Наш канал', 'Новости')
DEFAULT_BUTTONS = [
    {'id': key, 'text': label, 'enabled': key not in ('link1', 'link2'),
     'row': (1 if key == 'buy' else 2 if key == 'account' else
             3 if key in ('support', 'terms') else 4 if key == 'link1' else 5),
     'url': ''}
    for key, label in zip(IDS, LABELS)
]
DOMAIN = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$')


def valid_link(value):
    """Разрешаем только обычные HTTPS-ссылки с публично именуемым доменом."""
    if not value or value != value.strip() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return False
    try:
        u = urlsplit(value)
        return (u.scheme == 'https' and u.netloc and not u.username and not u.password
                and not u.fragment and (u.port is None or u.port == 443)
                and bool(u.hostname and DOMAIN.fullmatch(u.hostname))
                and all(part not in ('localhost', 'local', 'internal') for part in u.hostname.split('.')))
    except ValueError:
        return False


class MenuButton(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Literal['buy', 'account', 'support', 'terms', 'link1', 'link2']
    text: str = Field(min_length=1, max_length=60, strict=True)
    enabled: bool = Field(strict=True)
    row: int = Field(ge=1, le=6, strict=True)
    url: str = Field(default='', max_length=512, strict=True)

    @field_validator('text')
    @classmethod
    def label(cls, value):
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Укажите название кнопки без пробелов по краям и управляющих символов')
        return value

    @model_validator(mode='after')
    def link_rule(self):
        if self.id in IDS[:4] and self.url:
            raise ValueError('Встроенные действия не принимают URL')
        if self.id in IDS[4:] and (self.enabled or self.url) and not valid_link(self.url):
            raise ValueError('Для внешней кнопки укажите корректную HTTPS-ссылку')
        return self


class MenuUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, strict=True)
    buttons: list[MenuButton] = Field(min_length=6, max_length=6)

    @model_validator(mode='after')
    def structure(self):
        found = [item.id for item in self.buttons]
        if len(set(found)) != len(IDS) or set(found) != set(IDS):
            raise ValueError('Кнопки должны содержать ровно шесть разных действий')
        if not next(item for item in self.buttons if item.id == 'buy').enabled:
            raise ValueError('Основную кнопку покупки нельзя скрыть')
        rows = {}
        for item in self.buttons:
            if item.enabled:
                rows[item.row] = rows.get(item.row, 0) + 1
        if any(count > 2 for count in rows.values()):
            raise ValueError('В одной строке не больше двух активных кнопок')
        return self


def get():
    with db() as conn:
        row = conn.execute('SELECT content,version,updated FROM bot_menu_settings WHERE id=1').fetchone()
    if not row:
        raise HTTPException(503, 'Меню бота пока не инициализировано')
    return {'buttons': json.loads(row['content']), 'version': row['version'], 'updated': row['updated']}


def update(data):
    payload = json.dumps([button.model_dump() for button in data.buttons],
                         ensure_ascii=False, separators=(',', ':'))
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT content,version FROM bot_menu_settings WHERE id=1').fetchone()
        if not row:
            raise HTTPException(503, 'Меню бота пока не инициализировано')
        if row['version'] != data.version:
            raise HTTPException(409, 'Меню уже изменено. Обновите страницу перед сохранением')
        if json.loads(row['content']) != json.loads(payload):
            conn.execute('UPDATE bot_menu_settings SET content=?,version=version+1,updated=? WHERE id=1',
                         (payload, int(time.time())))
    return get()


def keyboard(user_id, make_ticket):
    """Создаёт приватную кнопку входа только при необходимости, без предпросмотра токена."""
    rows = {}
    icons = configured_icons()
    for item in get()['buttons']:
        if not item['enabled']:
            continue
        kind, label = item['id'], item['text']
        if kind == 'buy':
            button = {'text': label, 'web_app': {'url': c.PUBLIC_URL}}
        elif kind == 'account':
            button = {'text': label, 'url': make_ticket(user_id)}
        elif kind in ('support', 'terms'):
            button = {'text': label, 'callback_data': 'shop:' + kind}
        else:
            button = {'text': label, 'url': item['url']}
        rows.setdefault(item['row'], []).append(decorate(button, kind, icons))
    return {'inline_keyboard': [rows[key] for key in sorted(rows)]}
