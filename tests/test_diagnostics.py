import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
import pytest
from app import config as c,service,providers,diagnostics
from app.db import db,execute,one
from test_shop import client

spec=importlib.util.spec_from_file_location('status_cli',Path(__file__).parents[1]/'scripts/status.py')
cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)


def headers():return {'Authorization':'Bearer '+c.ADMIN_TOKEN}

def report(client):
    r=client.get('/api/admin/diagnostics',headers=headers());assert r.status_code==200,r.text
    return r.json()


def user():return execute("INSERT INTO users(email,created) VALUES ('private-customer@example.com',0)")

def order(uid,status='paid',**fields):
    oid=str(uuid.uuid4());values={'id':oid,'user_id':uid,'protocol':'xui','plan':'month','days':30,'amount':199,'status':status,'created':int(time.time())-1200,**fields}
    with db() as conn:conn.execute('INSERT INTO orders('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
    return oid


def test_admin_boundary_and_safe_snapshot_without_remote_calls(client,monkeypatch):
    assert client.get('/api/admin/diagnostics').status_code==403
    assert client.get('/api/admin/diagnostics',headers={'Authorization':'Bearer wrong'}).status_code==403
    uid=user();oid=order(uid,error='private-provider-url-and-key')
    before=list(sqlite3.connect(c.DB).iterdump())
    def forbidden(*a,**kw):raise AssertionError('Unexpected remote mutation')
    monkeypatch.setattr(providers,'probe_server',forbidden);monkeypatch.setattr(providers,'provision',forbidden)
    data=report(client);text=json.dumps(data)
    for secret in (oid,'private-customer@example.com','private-provider-url-and-key','https://panel.example',c.ADMIN_TOKEN,os.environ['ENCRYPTION_KEY']):assert secret not in text
    assert data['scope']=={'external_requests':False,'ciphertext_scan':False,'vpn_traffic_verified':False,'real_payments_verified':False,'backup_verified':False,'worker_process_only':True}
    assert data['queues']['issuance']['pending']==1 and data['queues']['issuance']['with_error']==1
    assert list(sqlite3.connect(c.DB).iterdump())==before
    assert client.get('/api/admin/diagnostics',headers=headers()).headers['cache-control']=='no-store'


def test_queues_are_distinct_and_due_counts_are_timed(client):
    uid=user();now=int(time.time())
    order(uid,next_attempt=now+1000,error='private');order(uid,next_attempt=0);order(uid,'active',error='old-private-error')
    order(uid,'pending',method='yookassa',payment_id='private-payment',next_attempt=0)
    order(uid,'pending',method='manual');order(uid,'checkout',method='stars')
    sid=str(uuid.uuid4());execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state) VALUES (?,?,'xui',?,'active')",(sid,uid,now-1200))
    for did in ('rotating','pending'):
        execute("INSERT INTO devices(id,subscription_id,request_id,name,state,created) VALUES (?,?,?,'Phone','pending',?)",(did,sid,did,now-100))
    execute("INSERT INTO device_rotations(id,device_id,request_id,old_credential_id,new_credential_id,created) VALUES ('job','rotating','r','private-old','private-new',?)",(now-100,))
    data=report(client)
    assert data['queues']['issuance']['pending']==2 and data['queues']['issuance']['due']==1
    assert data['queues']['payment_poll']['pending']==1 and data['queues']['devices']['pending']==1
    assert data['queues']['device_rotations']['pending']==1 and data['queues']['primary_rotations']['pending']==0
    assert data['expiry']['pending']==1 and data['expiry']['oldest_age_seconds']>=1200
    assert {'queues_paused','queue_errors','queue_wait_over_10m','expiry_wait_over_10m'}<=set(data['warnings'])


@pytest.mark.parametrize('values,expected',[
    ({},'not_observed'),
    ({'last_started':0,'last_success':0},'stale'),
    ({'last_started':'now','last_success':'now'},'recent_success'),
    ({'last_started':'now','running':True},'busy'),
    ({'last_started':0,'running':True},'slow'),
    ({'last_started':'future','running':True},'stale'),
    ({'last_started':'now','last_failure':'now'},'failed')])
def test_worker_observation_never_uses_old_database_heartbeat(client,monkeypatch,values,expected):
    monkeypatch.setenv('BACKGROUND_TASKS','true');now=int(time.time());service.reset_health()
    service.worker_health.update({k:now if v=='now' else now+1000 if v=='future' else v for k,v in values.items()})
    assert report(client)['worker']['state']==expected
    service.reset_health();assert report(client)['worker']['state']=='not_observed'


def test_worker_iteration_records_completion_failure_and_cancel(client,monkeypatch):
    monkeypatch.setenv('BACKGROUND_TASKS','true')
    async def ok():pass
    monkeypatch.setattr(service,'tick',ok);asyncio.run(service.worker_iteration())
    assert report(client)['worker']['state']=='recent_success'
    async def fail():raise RuntimeError('private remote response')
    monkeypatch.setattr(service,'tick',fail)
    with pytest.raises(RuntimeError):asyncio.run(service.worker_iteration())
    data=report(client);assert data['worker']['state']=='failed' and 'private remote response' not in json.dumps(data)
    monkeypatch.setattr(service,'tick',ok);asyncio.run(service.worker_iteration())
    assert report(client)['worker']['last_failure'] is None
    async def cancel():raise asyncio.CancelledError()
    monkeypatch.setattr(service,'tick',cancel)
    with pytest.raises(asyncio.CancelledError):asyncio.run(service.worker_iteration())
    assert not service.worker_health['running']


def test_server_capacity_and_probe_age_are_not_live_vpn_checks(client):
    uid=user();now=int(time.time())
    execute("UPDATE servers SET checked_at=?,capacity=1 WHERE protocol='xui'",(now,))
    execute("UPDATE servers SET state='disabled' WHERE protocol='wg'")
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,server_id) VALUES ('s',?,'xui',?,'active','test-xui')",(uid,now+86400))
    data=report(client);assert data['servers']['xui']['assignable']==0 and data['servers']['xui']['cached_api_ok_24h']==1
    assert 'no_assignable_servers' in data['warnings']
    execute('UPDATE servers SET checked_at=?',(now-86401,))
    assert 'server_probe_not_recent_ok' in report(client)['warnings']
    execute("UPDATE servers SET checked_at=?,health='error' WHERE protocol='xui'",(now,))
    assert report(client)['servers']['xui']['cached_api_error']==1


def test_setting_failures_only_reveal_booleans(client,monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY','private-invalid-key');monkeypatch.setattr(c,'PAYMENT_MODE','yookassa')
    monkeypatch.delenv('YOOKASSA_SECRET',raising=False);monkeypatch.setenv('YOOKASSA_SHOP_ID','private-shop-id')
    monkeypatch.setattr(c,'BOT_TOKEN','private-bot-token');monkeypatch.setattr(c,'BOT_USERNAME','');monkeypatch.delenv('TERMS_TEXT',raising=False)
    data=report(client);text=json.dumps(data)
    assert {'encryption_key_invalid','payment_settings_incomplete','telegram_sales_settings_incomplete'}<=set(data['warnings'])
    assert 'private-' not in text
    monkeypatch.setattr(c,'PAYMENT_MODE','private-unknown-mode');assert report(client)['settings']['payment_mode']=='invalid'


def test_no_claim_of_readiness_when_local_queues_are_empty(client,monkeypatch):
    now=int(time.time());execute('UPDATE servers SET checked_at=?',(now,))
    monkeypatch.setenv('BACKGROUND_TASKS','true');service.worker_health.update(last_started=now,last_success=now,last_failure=None,running=False)
    data=report(client);assert data['status']=='local_checks_ok'
    assert not data['scope']['vpn_traffic_verified'] and not data['scope']['real_payments_verified']
    monkeypatch.setenv('BACKGROUND_TASKS','false');assert 'worker_disabled' in report(client)['warnings']


def test_diagnostics_shop_prefix(client,monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(c,'BASE_PATH','/shop')
    with TestClient(create_app('/shop')) as page:
        assert page.get('/shop/api/admin/diagnostics',headers=headers()).json()['format']==1


@pytest.mark.parametrize('url',['https://example.com/shop','http://127.0.0.1.evil/shop','http://user:pass@127.0.0.1/shop','http://127.0.0.1/shop?token=x','http://localhost/shop','http://127.0.0.1/a%2Fb'])
def test_cli_refuses_token_transmission_outside_literal_loopback(url):
    with pytest.raises(ValueError):cli.fetch_report(url,'a'*48)


def test_cli_disables_proxies_redirects_and_bounds_response(monkeypatch,capsys):
    captured={}
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,size):return json.dumps({'format':1,'status':'needs_attention'}).encode()
    class Opener:
        def open(self,request,timeout):captured['request']=request;assert timeout==10;return Reply()
    def opener(*handlers):
        assert handlers[0].proxies=={} and handlers[1].redirect_request(None) is None
        return Opener()
    monkeypatch.setattr(cli,'build_opener',opener);monkeypatch.setenv('ADMIN_TOKEN','a'*48)
    assert cli.main(['--url','http://127.0.0.1:8123/shop'])==1
    assert captured['request'].full_url=='http://127.0.0.1:8123/shop/api/admin/diagnostics'
    assert captured['request'].headers['Authorization']=='Bearer '+'a'*48
    assert 'a'*48 not in capsys.readouterr().out
    monkeypatch.setattr(Reply,'read',lambda self,size:b'x'*262145)
    with pytest.raises(ValueError):cli.fetch_report('http://127.0.0.1','a'*48)


def test_cli_failure_is_redacted_and_does_not_create_database(tmp_path):
    env={**os.environ,'ADMIN_TOKEN':'private-token-'+'a'*40,'DATA_DIR':str(tmp_path/'must-not-exist')}
    result=subprocess.run([sys.executable,cli.__file__,'--url','http://127.0.0.1:1'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==2 and not (tmp_path/'must-not-exist').exists()
    assert 'private-token' not in result.stdout+result.stderr and 'Traceback' not in result.stderr


def test_cli_token_file_rejects_group_permissions(tmp_path,monkeypatch):
    token=tmp_path/'token';token.write_text('a'*48);token.chmod(0o644)
    with pytest.raises(ValueError):cli.main(['--token-file',str(token)])
    token.chmod(0o600);monkeypatch.setattr(cli,'fetch_report',lambda url,key:{'format':1,'status':'local_checks_ok'})
    assert cli.main(['--token-file',str(token)])==0


def test_database_error_is_redacted(client,monkeypatch):
    def fail(worker):raise sqlite3.OperationalError('private-database-path-and-token')
    monkeypatch.setattr(diagnostics,'report',fail)
    response=client.get('/api/admin/diagnostics',headers=headers())
    assert response.status_code==503 and 'private-' not in response.text


def test_missing_database_is_not_recreated_by_report(client):
    c.DB.unlink()
    response=client.get('/api/admin/diagnostics',headers=headers())
    assert response.status_code==503 and not c.DB.exists()
