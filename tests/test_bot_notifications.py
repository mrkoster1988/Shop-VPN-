"""Уведомления открывают Mini App без секретов и сохраняют повтор после сбоя."""
import asyncio
import time
import pytest
from app import service, config as c
from app.db import execute, one
from test_shop import client
from test_devices import issued

def prepare(issued):
    client,issue,_=issued
    sub=issue()
    execute('UPDATE users SET telegram_id=123')
    execute('UPDATE subscriptions SET expires=?,reminder=0',(int(time.time())+3600,))
    return sub

@pytest.mark.parametrize('kind',['purchase','trial','referral'])
def test_notification_buttons_and_no_duplicate_delivery(issued,monkeypatch,kind):
    prepare(issued)
    execute('UPDATE orders SET kind=?',(kind,))
    monkeypatch.setattr(c,'BOT_TOKEN','test-token')
    monkeypatch.setattr(c,'PUBLIC_URL','https://shop.example/custom')
    sent=[]
    async def send(tg_id,text,**kwargs):sent.append((tg_id,text,kwargs))
    monkeypatch.setattr(service,'send',send)
    asyncio.run(service.tick())
    assert len(sent)==2 and all(item[0]==123 for item in sent)
    buttons=[item[2]['reply_markup']['inline_keyboard'][0][0] for item in sent]
    assert [b['text'] for b in buttons]==['Открыть кабинет','Продлить VPN']
    assert all(b['web_app']['url']=='https://shop.example/custom?view=account' for b in buttons)
    assert one('SELECT count(*) n FROM tickets')['n']==0
    asyncio.run(service.tick())
    assert len(sent)==2

def test_disabled_bot_or_failed_send_keeps_notifications_pending(issued,monkeypatch):
    prepare(issued)
    async def failed(*args,**kwargs):raise RuntimeError('Delivery failed')
    monkeypatch.setattr(service,'send',failed)
    asyncio.run(service.tick())
    assert one('SELECT notified FROM orders')['notified']==0
    assert one('SELECT reminder FROM subscriptions')['reminder']==0
    monkeypatch.setattr(c,'BOT_TOKEN','test-token')
    asyncio.run(service.tick())
    assert one('SELECT notified FROM orders')['notified']==0
    assert one('SELECT reminder FROM subscriptions')['reminder']==0
    sent=[]
    async def delivered(*args,**kwargs):sent.append(args)
    monkeypatch.setattr(service,'send',delivered)
    asyncio.run(service.tick())
    assert len(sent)==2
    assert one('SELECT notified FROM orders')['notified']==1
    assert one('SELECT reminder FROM subscriptions')['reminder']==1

def test_email_only_account_is_not_marked_as_telegram_delivery(issued,monkeypatch):
    prepare(issued)
    execute('UPDATE users SET telegram_id=NULL')
    monkeypatch.setattr(c,'BOT_TOKEN','test-token')
    async def unexpected(*args,**kwargs):raise AssertionError('No recipient')
    monkeypatch.setattr(service,'send',unexpected)
    asyncio.run(service.tick())
    assert one('SELECT notified FROM orders')['notified']==0
    assert one('SELECT reminder FROM subscriptions')['reminder']==0
