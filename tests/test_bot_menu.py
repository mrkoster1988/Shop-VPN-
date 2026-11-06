"""Справочные кнопки используют текущие настройки без входа в аккаунт."""
import asyncio
import pytest
from app import service, config as c
from app.db import execute, one
from test_shop import client

def query(action='shop:support', **extra):
    return {'id':'callback-test','from':{'id':123,'is_bot':False},
            'message':{'date':1,'chat':{'id':123,'type':'private'}},'data':action,**extra}

def test_menu(client):
    user=service.telegram_account(123)
    keyboard=service.bot_menu(user['id'])['inline_keyboard']
    assert keyboard[0][0]['web_app']['url']==c.PUBLIC_URL
    assert keyboard[1][0]['url'].startswith(c.PUBLIC_URL)
    assert [(b['text'],b['callback_data']) for b in keyboard[2]]==[
        ('Поддержка','shop:support'),('Условия продажи','shop:terms')]

def test_current_settings_and_long_terms(client,monkeypatch):
    sent=[]
    async def send(tg_id,text,**kwargs):sent.append((tg_id,text))
    monkeypatch.setattr(service,'send',send)
    execute("UPDATE store_settings SET support_text='Новый контакт',terms_text=?",('🐾'*5000,))
    asyncio.run(service.answer_info_callback(query()))
    assert sent==[(123,'Новый контакт')]
    sent.clear()
    asyncio.run(service.answer_info_callback(query('shop:terms')))
    assert ''.join(text for _,text in sent)=='🐾'*5000
    assert all(len(text.encode('utf-16-le'))//2<=4096 for _,text in sent)
    assert one('SELECT count(*) n FROM tickets')['n']==0
    assert one('SELECT count(*) n FROM users')['n']==0

@pytest.mark.parametrize('bad',[
    query('untrusted'),query(message={'date':1,'chat':{'id':123,'type':'group'}}),
    query(message={'date':1,'chat':{'id':999,'type':'private'}}),
    query(message={'date':0,'chat':{'id':123,'type':'private'}}),
    query(message=None),query(**{'from':{'id':123,'is_bot':True}}),
])
def test_invalid_context(client,monkeypatch,bad):
    async def unexpected(*args,**kwargs):raise AssertionError('Не должно быть сообщения')
    monkeypatch.setattr(service,'send',unexpected)
    asyncio.run(service.answer_info_callback(bad))

def test_polling_routes_callback_and_saves_offset_after_expired_ack(client,monkeypatch):
    sent=[];requests=[]
    monkeypatch.setattr(c,'BOT_TOKEN','test-token')
    async def send(tg_id,text,**kwargs):sent.append((tg_id,text))
    monkeypatch.setattr(service,'send',send)
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'ok':True,'result':[{'update_id':10,'callback_query':query()}]}
    class API:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,*,json):
            requests.append((url.rsplit('/',1)[-1],json))
            if url.endswith('/answerCallbackQuery'):raise RuntimeError('Query expired')
            if len([r for r in requests if r[0]=='getUpdates'])>1:raise asyncio.CancelledError()
            return Response()
    monkeypatch.setattr(service.httpx,'AsyncClient',API)
    with pytest.raises(asyncio.CancelledError):asyncio.run(service.bot_loop())
    assert 'callback_query' in requests[0][1]['allowed_updates']
    assert any(name=='answerCallbackQuery' for name,_ in requests)
    assert len(sent)==1 and sent[0][0]==123
    assert one("SELECT value FROM meta WHERE key='bot_offset'")['value']=='11'

def test_empty_terms(client,monkeypatch):
    sent=[]
    async def send(tg_id,text,**kwargs):sent.append(text)
    monkeypatch.setattr(service,'send',send)
    execute("UPDATE store_settings SET terms_text=''")
    asyncio.run(service.send_shop_info(123,'terms'))
    assert sent==['Условия продажи ещё не заполнены владельцем магазина.']
