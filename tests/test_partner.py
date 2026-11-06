import hashlib
import hmac
import json
import time
import uuid

import pytest

from app import config as c
from app.db import one,execute
from test_shop import client,register,admin_headers


SECRET='test-partner-secret-with-at-least-32-bytes'


@pytest.fixture
def buyer(client):
    register(client)
    return client


def coupon(client,code='PARTNER15'):
    response=client.post('/api/admin/coupons',headers=admin_headers(),json={
        'code':code,'percent':15,'enabled':True,'max_uses':100,'expires_at':0,
        'plan_id':None,'protocol':'all'})
    assert response.status_code==201,response.text
    return response.json()


def set_partner(monkeypatch,code='PARTNER15'):
    monkeypatch.setattr(c,'PARTNER_WEBHOOK_SECRET',SECRET)
    monkeypatch.setattr(c,'PARTNER_PROMO_CODE',code)


def event_request(client,event,*,timestamp=None,secret=SECRET):
    raw=json.dumps(event,separators=(',',':')).encode()
    stamp=str(int(time.time()) if timestamp is None else timestamp)
    signature='sha256='+hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    return client.post('/api/partner/events',content=raw,headers={
        'Content-Type':'application/json','X-Partner-Timestamp':stamp,'X-Partner-Signature':signature})


def make_event(tg=123456789):
    return {'event_id':str(uuid.uuid4()),'telegram_id':tg,'event_type':'service_purchase_paid'}


def quote(client,code='PARTNER15'):
    return client.post('/api/quote',json={
        'plan':'month','protocol':'xui','plan_version':1,'promo_code':code})


def link_telegram_id(telegram_id):
    row=one('SELECT id FROM users ORDER BY id LIMIT 1')
    execute('UPDATE users SET telegram_id=? WHERE id=?',(telegram_id,row['id']))


def test_signed_partner_event_grants_discount_once_to_matching_telegram_user(buyer,monkeypatch):
    set_partner(monkeypatch);coupon(buyer);link_telegram_id(123456789)
    assert quote(buyer).status_code==409
    event=make_event()
    first=event_request(buyer,event);assert first.status_code==200 and first.json()['accepted']
    discount=quote(buyer);assert discount.status_code==200
    assert (discount.json()['promo_percent'],discount.json()['discount'],discount.json()['amount'])==(15,29,170)
    replay=event_request(buyer,event);assert replay.status_code==200 and replay.json()['replayed']
    stale_replay=event_request(buyer,event,timestamp=int(time.time())-301)
    assert stale_replay.status_code==401
    assert one('SELECT count(*) AS n FROM partner_events')['n']==1
    placed=buyer.post('/api/orders',json={
        'plan':'month','protocol':'xui','plan_version':1,'promo_code':'PARTNER15',
        'promo_version':discount.json()['promo_version'],'quoted_amount':discount.json()['amount'],
        'quote_currency':discount.json()['currency'],'accept_terms':True})
    assert placed.status_code==200,placed.text
    assert quote(buyer).status_code==409


def test_signed_event_does_not_grant_to_another_telegram_identity(buyer,monkeypatch):
    set_partner(monkeypatch);coupon(buyer)
    event_request(buyer,make_event(111111111))
    link_telegram_id(222222222)
    assert quote(buyer).status_code==409


def test_partner_signature_timestamp_and_event_id_are_checked(buyer,monkeypatch):
    set_partner(monkeypatch);coupon(buyer);event=make_event()
    raw=json.dumps(event,separators=(',',':')).encode()
    stamp=str(int(time.time()))
    bad=buyer.post('/api/partner/events',content=raw,headers={
        'Content-Type':'application/json','X-Partner-Timestamp':stamp,'X-Partner-Signature':'sha256='+'0'*64})
    assert bad.status_code==401
    stale=event_request(buyer,event,timestamp=int(time.time())-301)
    assert stale.status_code==401
    first=event_request(buyer,event);assert first.status_code==200
    altered={**event,'telegram_id':987654321}
    conflict=event_request(buyer,altered)
    assert conflict.status_code==409
    assert one('SELECT count(*) AS n FROM partner_events')['n']==1


def test_partner_endpoint_fails_closed_when_configuration_or_coupon_is_unavailable(buyer,monkeypatch):
    event=make_event()
    monkeypatch.setattr(c,'PARTNER_WEBHOOK_SECRET','')
    monkeypatch.setattr(c,'PARTNER_PROMO_CODE','PARTNER15')
    assert event_request(buyer,event).status_code==503
    set_partner(monkeypatch)
    assert event_request(buyer,event).status_code==503
    assert one('SELECT count(*) AS n FROM partner_events')['n']==0


def test_non_partner_coupon_still_works_for_accounts_without_telegram(buyer,monkeypatch):
    set_partner(monkeypatch)
    coupon(buyer,code='PUBLIC10')
    response=quote(buyer,code='PUBLIC10')
    assert response.status_code==200 and response.json()['promo_percent']==15
