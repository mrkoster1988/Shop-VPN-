"""Owner deletion: remote first, reversible failures, immutable finance records."""
import asyncio
import time

from app import config as c, providers
from app.db import db, execute, one
from test_shop import client, register


def headers():
    return {'Authorization': 'Bearer ' + c.ADMIN_TOKEN}


def subscription(client, sid='6b300651-e466-466b-9140-eac2296b6b62', protocol='wg'):
    register(client)
    execute("""INSERT INTO subscriptions(
        id,user_id,protocol,expires,state,server_id,secret)
        VALUES (?,?,?,?,'active',?,?)""",
        (sid, 1, protocol, int(time.time())+86400,
         'test-wg' if protocol=='wg' else 'test-xui', c.encrypt('test-config')))
    return sid


def test_admin_revoke_and_delete_retains_financial_order(client, monkeypatch):
    sid=subscription(client)
    execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status) "
            "VALUES ('financial1',1,'wg','month',30,199,1,'active')")
    profile=client.get('/api/admin/users/1',headers=headers()).json()
    assert profile['subscriptions'][0]['can_delete'] is False
    called=[]
    async def revoke(remote):
        called.append(remote['id'])
    monkeypatch.setattr(providers,'disable',revoke)
    assert client.post('/api/admin/subscriptions/'+sid+'/revoke').status_code==401
    revoked=client.post('/api/admin/subscriptions/'+sid+'/revoke',headers=headers())
    assert revoked.status_code==200,revoked.text
    assert revoked.json()['status']=='revoked'
    visible=client.get('/api/admin/users/1',headers=headers()).json()
    assert visible['subscriptions'][0]['can_delete'] is True
    assert visible['subscriptions'][0]['delete_blocker'] is None
    assert called==[sid]
    saved=one('SELECT state,secret FROM subscriptions WHERE id=?',(sid,))
    assert saved['state']=='revoked' and saved['secret'] is None
    assert client.post('/api/admin/subscriptions/'+sid+'/revoke',headers=headers()).status_code==200
    assert called==[sid]  # An already confirmed revoke is idempotent.
    deleted=client.delete('/api/admin/subscriptions/'+sid,headers=headers())
    assert deleted.status_code==200,deleted.text
    assert one('SELECT id FROM subscriptions WHERE id=?',(sid,)) is None
    assert one("SELECT id,amount FROM orders WHERE id='financial1'")['amount']==199
    assert one('SELECT subscription_id FROM admin_subscription_revocations '
               'WHERE subscription_id=?',(sid,))['subscription_id']==sid


def test_remote_failure_never_deletes_active_access_record(client, monkeypatch):
    sid=subscription(client)
    async def unavailable(_):
        raise TimeoutError('never expose secret remote failure details')
    monkeypatch.setattr(providers,'disable',unavailable)
    result=client.post('/api/admin/subscriptions/'+sid+'/revoke',headers=headers())
    assert result.status_code==502
    assert 'secret' not in result.text
    saved=one('SELECT state,secret FROM subscriptions WHERE id=?',(sid,))
    assert saved['state']=='active' and saved['secret']
    assert client.delete('/api/admin/subscriptions/'+sid,headers=headers()).status_code==409


def test_unfulfilled_paid_order_blocks_cleanup(client, monkeypatch):
    sid=subscription(client)
    execute("INSERT INTO orders(id,user_id,protocol,plan,days,amount,created,status) "
            "VALUES ('unfulfilled1',1,'wg','month',30,199,1,'paid')")
    async def prohibited(_):
        raise AssertionError('remote must not be touched')
    monkeypatch.setattr(providers,'disable',prohibited)
    assert client.post('/api/admin/subscriptions/'+sid+'/revoke',
                       headers=headers()).status_code==409
    assert one('SELECT state FROM subscriptions WHERE id=?',(sid,))['state']=='active'


def test_autogrant_keeps_history_after_revoke(client, monkeypatch):
    sid=subscription(client)
    execute("""INSERT INTO flow_auto_grants(
        user_id,subscription_id,target_expires,policy_version,state,created,next_attempt)
        VALUES (1,?,9999999999,1,'active',1,1)""",(sid,))
    async def revoke(_):pass
    monkeypatch.setattr(providers,'disable',revoke)
    assert client.post('/api/admin/subscriptions/'+sid+'/revoke',
                       headers=headers()).status_code==200
    assert one('SELECT state FROM flow_auto_grants')['state']=='superseded'
    result=client.delete('/api/admin/subscriptions/'+sid,headers=headers())
    assert result.status_code==409
    assert one('SELECT id FROM subscriptions WHERE id=?',(sid,)) is not None


def test_delete_unused_plan_and_keep_default_or_sold_plans(client):
    assert client.delete('/api/admin/plans/month',headers=headers()).status_code==409
    data={'id':'sample','name':'Черновик','days':7,'price':0,
          'stars':0,'enabled':False,'device_limit':0}
    created=client.post('/api/admin/plans',json=data,headers=headers())
    assert created.status_code==201,created.text
    assert client.delete('/api/admin/plans/sample').status_code==401
    removed=client.delete('/api/admin/plans/sample',headers=headers())
    assert removed.status_code==200,removed.text
    assert one("SELECT id FROM plans WHERE id='sample'") is None


def test_unused_disabled_server_only(client):
    assert client.delete('/api/admin/servers/test-wg',headers=headers()).status_code==409
    execute("UPDATE servers SET state='disabled' WHERE id='test-wg'")
    assert client.delete('/api/admin/servers/test-wg',headers=headers()).status_code==200
    assert one("SELECT id FROM servers WHERE id='test-wg'") is None


def test_administrator_cannot_revoke_or_delete_even_with_customer_access(client):
    first=client.post('/api/admin/auth/bootstrap',headers=headers(),json={
        'email':'developer@example.org','password':'long-developer-password'})
    assert first.status_code==200,first.text
    made=client.post('/api/admin/staff',json={
        'email':'admin@example.org','password':'long-admin-password',
        'role':'administrator'})
    assert made.status_code==201,made.text
    client.post('/api/admin/auth/logout')
    logged=client.post('/api/admin/auth/login',json={
        'email':'admin@example.org','password':'long-admin-password'})
    assert logged.status_code==200,logged.text
    assert client.post('/api/admin/subscriptions/unknown/revoke').status_code==403
    assert client.delete('/api/admin/subscriptions/unknown').status_code==403
    assert client.delete('/api/admin/plans/month').status_code==403
    assert client.delete('/api/admin/servers/test-wg').status_code==403
