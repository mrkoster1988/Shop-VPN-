"""Регрессия редактора Telegram: роли, валидация, конкурентность и старые кнопки."""
import copy

import pytest
from pydantic import ValidationError
from app import bot_menu, service
from app.db import one, init_db
from test_shop import client


def bootstrap(client):
    result = client.post('/api/admin/auth/bootstrap',
        headers={'Authorization': 'Bearer ' + 'a'*48},
        json={'email':'owner@example.com','password':'long-owner-password'})
    assert result.status_code == 200
    return result


def test_default_menu_preserves_existing_telegram_buttons(client):
    result = bot_menu.get()
    assert result['version'] == 1
    user = service.telegram_account(5678)
    rows = service.bot_menu(user['id'])['inline_keyboard']
    assert rows[0][0]['web_app']['url']
    assert rows[1][0]['url'].endswith('') and '/login?ticket=' in rows[1][0]['url']
    assert [button['callback_data'] for button in rows[2]] == ['shop:support','shop:terms']
    assert one('SELECT count(*) AS n FROM tickets')['n'] == 1
    init_db()
    assert bot_menu.get()['version'] == 1


def test_owner_only_versioned_update_and_noop(client):
    assert client.get('/api/admin/bot-menu').status_code == 401
    bootstrap(client)
    saved = client.get('/api/admin/bot-menu').json()
    assert saved['version'] == 1
    payload = {'buttons': copy.deepcopy(saved['buttons']), 'version': 1}
    payload['buttons'][2]['text'] = 'Написать в поддержку'
    payload['buttons'][2]['row'] = 4
    payload['buttons'][3]['row'] = 4
    result = client.put('/api/admin/bot-menu',json=payload)
    assert result.status_code == 200, result.text
    assert result.json()['version'] == 2
    user = service.telegram_account(5678)
    rows = service.bot_menu(user['id'])['inline_keyboard']
    assert rows[2][0]['text'] == 'Написать в поддержку'
    assert client.put('/api/admin/bot-menu',json=payload).status_code == 409
    assert client.put('/api/admin/bot-menu',json={**payload,'version':2}).json()['version'] == 2
    assert client.get('/api/admin/bot-menu').json()['buttons'][2]['text'] == 'Написать в поддержку'


def test_optional_link_and_hidden_account_do_not_create_ticket(client):
    bootstrap(client)
    state = client.get('/api/admin/bot-menu').json()
    buttons=state['buttons']
    buttons[1]['enabled']=False
    buttons[4].update({'enabled':True,'text':'Пушок в Telegram','url':'https://t.me/push_canal','row':2})
    result=client.put('/api/admin/bot-menu',json={'buttons':buttons,'version':state['version']})
    assert result.status_code == 200, result.text
    user=service.telegram_account(5678)
    rows=service.bot_menu(user['id'])['inline_keyboard']
    assert rows[1][0] == {'text':'Пушок в Telegram','url':'https://t.me/push_canal'}
    assert one('SELECT count(*) AS n FROM tickets')['n'] == 0


@pytest.mark.parametrize('change', [
    lambda b:b[0].update(enabled=False),
    lambda b:b[0].update(id='link1'),
    lambda b:b[0].update(text='<bad>\n'),
    lambda b:b[4].update(enabled=True,url='http://example.org'),
    lambda b:b[4].update(enabled=True,url='https://user:secret@example.org'),
    lambda b:b[4].update(enabled=True,url='https://localhost/admin'),
    lambda b:b[4].update(enabled=True,url='https://example.org/#secret'),
    lambda b:b[4].update(enabled=True,url='https://example.org:8443'),
    lambda b:b[4].update(enabled=True,url='https://example.org',row=3),
    lambda b:b[0].update(url='https://example.org'),
    lambda b:b[1].update(row=True),
])
def test_invalid_menu_is_rejected_without_mutation(client,change):
    bootstrap(client)
    before=client.get('/api/admin/bot-menu').json()
    buttons=copy.deepcopy(before['buttons'])
    change(buttons)
    result=client.put('/api/admin/bot-menu',json={'buttons':buttons,'version':before['version']})
    assert result.status_code == 422
    assert client.get('/api/admin/bot-menu').json() == before


def test_staff_cannot_read_or_change_menu(client):
    from app import staff
    bootstrap(client)
    owner=one('SELECT id FROM staff_users WHERE role=?',('owner',))
    staff.create_staff('help@example.com','long-support-password','support',owner['id'])
    assert client.post('/api/admin/auth/logout').status_code == 200
    logged=client.post('/api/admin/auth/login',json={'email':'help@example.com','password':'long-support-password'})
    assert logged.status_code == 200
    assert client.get('/api/admin/bot-menu').status_code == 403
    assert client.put('/api/admin/bot-menu',json={'version':1,'buttons':bot_menu.DEFAULT_BUTTONS}).status_code == 403
