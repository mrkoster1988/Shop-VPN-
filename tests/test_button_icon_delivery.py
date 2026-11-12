"""Отказ декоративных emoji не должен блокировать обычное меню."""
import asyncio
import copy
import json

import httpx
import pytest

from app import config as c, service
from app.pushok_button_icons import without_icons


MENU = {'inline_keyboard': [[
    {'text': 'Купить', 'web_app': {'url': 'https://shop.example/shop'}, 'icon_custom_emoji_id': '123456789'},
    {'text': 'Поддержка', 'callback_data': 'shop:support', 'icon_custom_emoji_id': '123456790'},
], [{'text': 'Кабинет', 'url': 'https://shop.example/shop/login?ticket=fixture'}]]}


def api(monkeypatch, replies):
    monkeypatch.setattr(c, 'BOT_TOKEN', 'fixture-bot-token')
    original = httpx.AsyncClient
    sent = []
    def handler(request):
        assert request.url.path.endswith('/sendMessage')
        sent.append(json.loads(request.content))
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(service.httpx, 'AsyncClient', lambda **kwargs:
        original(**kwargs, transport=httpx.MockTransport(handler)))
    return sent


def test_explicit_refusal_retries_same_menu_without_icons(monkeypatch, caplog):
    before = copy.deepcopy(MENU)
    sent = api(monkeypatch, [httpx.Response(400,json={'ok':False,'error_code':400,'description':'fixture upstream detail'}),
                            httpx.Response(200,json={'ok':True,'result':{'message_id':1}})])
    asyncio.run(service.send(123,'Меню',reply_markup=MENU,disable_notification=True))
    assert len(sent) == 2 and MENU == before
    assert sent[0]['reply_markup'] == MENU
    assert sent[1] == {**sent[0], 'reply_markup': without_icons(MENU)}
    assert 'fixture upstream detail' not in caplog.text and 'fixture-bot-token' not in caplog.text


@pytest.mark.parametrize('response', [httpx.Response(code,json={'ok':False,'error_code':code}) for code in (401,403,429,500,502)] +
    [httpx.Response(400,text='not-json'),httpx.Response(400,json={'ok':True}),httpx.Response(400,json={'ok':False}),
     httpx.Response(200,json={'ok':False,'error_code':400}),httpx.ReadTimeout('fixture timeout')])
def test_ambiguous_or_non_validation_failure_is_not_retried(monkeypatch, response):
    sent = api(monkeypatch,[response])
    with pytest.raises((httpx.HTTPError,RuntimeError)):
        asyncio.run(service.send(123,'Меню',reply_markup=MENU))
    assert len(sent) == 1


def test_second_failure_stops_after_one_retry(monkeypatch):
    sent = api(monkeypatch,[httpx.Response(400,json={'ok':False,'error_code':400}) for _ in range(2)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(service.send(123,'Меню',reply_markup=MENU))
    assert len(sent) == 2


def test_no_icons_no_retry_and_success_no_duplicate(monkeypatch):
    sent = api(monkeypatch,[httpx.Response(400,json={'ok':False,'error_code':400}),httpx.Response(200,json={'ok':True})])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(service.send(123,'Меню',reply_markup=without_icons(MENU)))
    asyncio.run(service.send(123,'Меню',reply_markup=MENU))
    assert len(sent) == 2 and sent[1]['reply_markup'] == MENU


@pytest.mark.parametrize('markup',[None, 'json-string', {}, {'inline_keyboard':'bad'}, {'inline_keyboard':[['bad']]}, {'inline_keyboard':[]}])
def test_only_valid_inline_menu_is_stripped(markup):
    assert without_icons(markup) is None
