"""Настройки применяются к реальным операциям, а не только к кнопкам витрины."""
import asyncio
import json
import time
import uuid

import pytest
from app import config as c, providers, runtime_settings, service
from app.db import execute, init_db, one
from test_shop import client, register, order, confirm, admin_headers

BASE='/api/admin/runtime/'


def payload(client,group,**changes):
    r=client.get(BASE+group,headers=admin_headers())
    assert r.status_code==200,r.text
    values=r.json();values.pop('updated')
    return values|changes


def save(client,group,**changes):
    r=client.put(BASE+group,headers=admin_headers(),json=payload(client,group,**changes))
    assert r.status_code==200,r.text
    return r.json()


def option(client,protocol):
    return next(x for x in client.get('/api/catalog').json()['protocols'] if x['id']==protocol)


def purchase(client,protocol='wg',plan='month',**changes):
    return client.post('/api/orders',json={'plan':plan,'protocol':protocol,**changes})


def test_default_and_independent_versioned_sections(client):
    assert one('SELECT count(*) AS n FROM shop_runtime_settings')['n']==0
    assert client.get(BASE+'appearance').status_code==401
    original=client.get('/api/catalog').json()
    assert original['appearance']['brand_name']=='pushok.vpn'
    assert 'purchase_text' not in original
    assert option(client,'wg')['available'] is True
    a=payload(client,'appearance',brand_name='Shop Test',theme='ocean',announcement='Объявление')
    first=client.put(BASE+'appearance',headers=admin_headers(),json=a)
    assert first.status_code==200 and first.json()['version']==1
    assert client.put(BASE+'appearance',headers=admin_headers(),json=a).status_code==409
    s=save(client,'sales',new_sales=False)
    assert s['version']==1
    assert save(client,'sales')['version']==1
    init_db();init_db()
    shown=client.get('/api/catalog').json()['appearance']
    assert shown['brand_name']=='Shop Test' and shown['theme']=='ocean'
    assert shown['announcement']=='Объявление' and 'version' not in shown
    assert option(client,'wg')['sale_reason']=='new_sales_paused'


@pytest.mark.parametrize('group,changes',[
    ('appearance',{'theme':'unknown'}),
    ('appearance',{'brand_name':'   '}),
    ('appearance',{'headline':'line\nbreak'}),
    ('sales',{'new_sales':'false'}),
    ('sales',{'enabled_protocols':['wg','wg']}),
    ('sales',{'enabled_protocols':['unknown']}),
    ('notifications',{'expiry_hours':169}),
    ('notifications',{'expiry_hours':True}),
    ('notifications',{'purchase_text':' '}),
])
def test_invalid_settings_do_not_persist(client,group,changes):
    response=client.put(BASE+group,headers=admin_headers(),json=payload(client,group,**changes))
    assert response.status_code==422
    assert one('SELECT count(*) AS n FROM shop_runtime_settings')['n']==0


def test_incomplete_write_and_corrupt_sales_fail_closed(client):
    assert client.put(BASE+'sales',headers=admin_headers(),json={'version':0,'new_sales':False}).status_code==422
    save(client,'sales',new_sales=False)
    execute("UPDATE shop_runtime_settings SET content=? WHERE section='sales'",(json.dumps({'new_sales':True}),))
    register(client)
    assert purchase(client).status_code==503
    assert one('SELECT count(*) AS n FROM orders')['n']==0


def test_paid_sales_pause_preserves_pending_order_and_fulfillment(client,monkeypatch):
    register(client);existing=order(client,'wg')
    save(client,'sales',new_sales=False,closed_notice='Продажи на паузе')
    assert option(client,'wg')['available'] is False
    assert option(client,'wg')['trial_available'] is True
    repeated=purchase(client)
    assert repeated.status_code==200 and repeated.json()['id']==existing['id']
    denied=purchase(client,'xui')
    assert denied.status_code==409 and denied.json()['detail']=='Продажи на паузе'
    assert client.post('/api/quote',json={'plan':'month','plan_version':1,'protocol':'xui'}).status_code==409
    assert one('SELECT count(*) AS n FROM orders')['n']==1
    async def provision(sub):return '[Interface]\n[Peer]\n'
    monkeypatch.setattr(providers,'provision',provision)
    assert confirm(client,existing['id']).status_code==200
    asyncio.run(service.fulfill(existing['id']))
    sub=one('SELECT * FROM subscriptions')
    assert sub['state']=='active'
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200
    assert option(client,'wg')['available'] is True
    save(client,'sales',renewals=False)
    assert option(client,'wg')['sale_reason']=='renewal_paused'
    assert purchase(client).status_code==409
    assert client.get('/api/subscriptions/'+sub['id']+'/config').status_code==200


def test_protocol_and_account_channel_controls(client,monkeypatch):
    register(client)
    save(client,'sales',enabled_protocols=['wg'],web_sales=False)
    assert option(client,'xui')['sale_reason']=='protocol_paused'
    assert option(client,'wg')['sale_reason']=='channel_paused'
    assert purchase(client).status_code==409
    save(client,'sales',web_sales=True,telegram_sales=False)
    assert option(client,'wg')['available'] is True
    execute('UPDATE users SET telegram_id=?',(123456789,))
    monkeypatch.setattr(c,'BOT_TOKEN','fixture-not-a-real-token')
    assert option(client,'wg')['sale_reason']=='channel_paused'
    assert purchase(client,accept_terms=True).status_code==409
    assert one('SELECT count(*) AS n FROM orders')['n']==0


def test_notifications_pause_resume_threshold_and_retry(client,monkeypatch):
    register(client);paid=order(client,'wg')
    uid=one('SELECT id FROM users')['id'];now=int(time.time())
    execute('UPDATE users SET telegram_id=? WHERE id=?',(123456789,uid))
    execute("UPDATE orders SET status='active' WHERE id=?",(paid['id'],))
    sid=str(uuid.uuid4())
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id) VALUES (?,?,'wg',?,'active','test-wg')",(sid,uid,now+48*3600))
    monkeypatch.setattr(c,'BOT_TOKEN','fixture-not-a-real-token')
    sent=[]
    async def deliver(tg_id,text,**kwargs):
        sent.append((tg_id,text))
    monkeypatch.setattr(service,'send',deliver)
    save(client,'notifications',order_ready_enabled=False,expiry_enabled=False)
    asyncio.run(service.notify_customers(now))
    assert sent==[] and one('SELECT notified FROM orders')['notified']==0
    save(client,'notifications',order_ready_enabled=True,expiry_enabled=True,expiry_hours=24,purchase_text='Профиль готов',expiry_text='Срок заканчивается')
    asyncio.run(service.notify_customers(now))
    assert sent==[(123456789,'Профиль готов')]
    assert one('SELECT reminder FROM subscriptions')['reminder']==0
    save(client,'notifications',expiry_hours=72)
    async def failed(*args,**kwargs):raise TimeoutError('fixture')
    monkeypatch.setattr(service,'send',failed)
    asyncio.run(service.notify_customers(now))
    assert one('SELECT reminder FROM subscriptions')['reminder']==0
    monkeypatch.setattr(service,'send',deliver)
    asyncio.run(service.notify_customers(now))
    asyncio.run(service.notify_customers(now))
    assert sent==[(123456789,'Профиль готов'),(123456789,'Срок заканчивается')]
    assert one('SELECT reminder FROM subscriptions')['reminder']==1
