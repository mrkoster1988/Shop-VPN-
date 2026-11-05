import pytest
from app.db import execute
from test_shop import client, register, order, confirm

@pytest.mark.parametrize('status,attempts,stage,needs_support',[
    ('pending',0,'waiting_payment',False),('checkout',0,'waiting_payment',False),
    ('paid',0,'queued',False),('paid',1,'retrying',False),('paid',2,'retrying',False),
    ('paid',3,'help',True),('paid',20,'help',True),('active',20,'ready',False),('canceled',5,'canceled',False),
])
def test_list_and_detail_progress_agree_without_exposing_provider_error(client,status,attempts,stage,needs_support):
    register(client);purchase=order(client)
    execute('UPDATE orders SET status=?,attempts=?,error=? WHERE id=?',(status,attempts,'private-provider-url-token-key',purchase['id']))
    detail=client.get('/api/orders/'+purchase['id'])
    listing=client.get('/api/me')
    progress=detail.json()['progress']
    assert progress==listing.json()['orders'][0]['progress']
    assert progress['stage']==stage and progress['needs_support'] is needs_support
    assert 'private-provider-url-token-key' not in detail.text+listing.text
    if status=='paid':assert 'Повторно оплачивать не нужно' in progress['message']
    if status!='pending' and status!='checkout':assert detail.json()['instructions']==''

@pytest.mark.parametrize('kind',['trial','referral'])
def test_non_purchase_progress_does_not_claim_payment(client,kind):
    register(client);purchase=order(client)
    execute("UPDATE orders SET status='paid',kind=?,attempts=3 WHERE id=?",(kind,purchase['id']))
    progress=client.get('/api/orders/'+purchase['id']).json()['progress']
    assert progress['needs_support']
    assert 'оплач' not in (progress['message']+progress['title']).lower()
    assert 'списан' not in progress['message'].lower()

def test_manual_instructions_disappear_after_confirmation(client,monkeypatch):
    monkeypatch.setenv('MANUAL_PAYMENT_TEXT','fixture-payment-instructions')
    register(client);purchase=order(client)
    assert purchase['instructions']=='fixture-payment-instructions'
    assert confirm(client,purchase['id']).status_code==200
    response=client.get('/api/orders/'+purchase['id']).json()
    assert response['instructions']=='' and response['progress']['stage']=='queued'

def test_progress_cannot_be_read_by_other_account_or_after_logout(client):
    register(client);purchase=order(client)
    client.post('/api/auth/logout')
    assert client.get('/api/orders/'+purchase['id']).status_code==401
    register(client,'other@example.com')
    assert client.get('/api/orders/'+purchase['id']).status_code==404
    assert client.get('/api/me').json()['orders']==[]
