"""Individual feeds reuse the paid device lifecycle, not the primary peer."""
import asyncio
import base64
import hashlib
import time
import uuid
from urllib.parse import urlsplit

import pytest

from app import config as c, providers, service
from app.db import one, execute, init_db
from test_shop import client, register
from test_devices import issued, add


def config(identity):
    private=base64.b64encode(hashlib.sha256(('fixture-'+identity).encode()).digest()).decode()
    public=base64.b64encode(bytes([7])*32).decode()
    return ('[Interface]\nPrivateKey = '+private+'\nAddress = 10.99.0.2/32\n'
            '[Peer]\nPublicKey = '+public+'\nEndpoint = 203.0.113.10:51820\n'
            'AllowedIPs = 0.0.0.0/0\n')


@pytest.fixture
def setup_devices(issued,monkeypatch):
    client,issue,calls=issued
    async def provision(sub):
        calls.append(dict(sub))
        return config(sub['id'])
    monkeypatch.setattr(providers,'provision',provision)
    sub=issue('wg')
    phone=add(client,sub['id'],'Phone').json()
    tablet=add(client,sub['id'],'Tablet').json()
    asyncio.run(service.process_devices())
    return client,issue,calls,sub,phone['id'],tablet['id']


def path(did):
    return '/api/flow/wireguard/devices/'+did


def change(client,did,action,version):
    return client.put(path(did)+'/link',json={'action':action,'version':version})


def enable(client,did,version=0):
    r=change(client,did,'enable',version)
    assert r.status_code==200,r.text
    return r.json()


def feed(client,link):
    return client.get(urlsplit(link['url']).path)


def test_only_selected_device_config_and_stable_revision_after_renewal(setup_devices):
    client,issue,_,sub,a,b=setup_devices
    assert client.get(path(a)+'/link').json()=={'enabled':False,'version':0,'url':None}
    first=enable(client,a);second=enable(client,b)
    assert first['url']!=second['url']
    assert enable(client,a,0)==first
    doc=feed(client,first).json()
    assert doc['schema']=='pushok-flow-subscription' and doc['version']==1 and doc['revision']==1
    assert doc['profiles']==[{'id':a,'name':'Pushok WireGuard','protocol':'wireguard','config':config(a)}]
    assert feed(client,second).json()['profiles'][0]['config']==config(b)
    assert client.get(path(a)).json()==doc
    assert doc['profiles'][0]['config']!=config(sub['id'])
    row=one('SELECT * FROM flow_wireguard_device_links WHERE device_id=?',(a,))
    raw=first['url'].rsplit('/',1)[-1]
    assert row['token_hash']!=raw and row['token_secret']!=raw and c.decrypt(row['token_secret'])==raw
    issue('wg');init_db()
    renewed=feed(client,first).json()
    assert renewed['revision']==2 and renewed['expires_at']>doc['expires_at']
    assert renewed['profiles']==doc['profiles']
    assert feed(client,first).json()==renewed


def test_rotate_device_key_invalidates_only_its_old_link(setup_devices):
    client,_,_,sub,a,b=setup_devices
    first=enable(client,a);other=enable(client,b)
    request={'request_id':str(uuid.uuid4())}
    assert client.post('/api/devices/'+a+'/rotate',json=request).status_code==201
    assert feed(client,first).status_code==404
    assert change(client,a,'enable',2).status_code==409
    assert feed(client,other).json()['profiles'][0]['config']==config(b)
    asyncio.run(service.process_devices())
    new=enable(client,a,2)
    assert new['url']!=first['url'] and new['version']==3
    assert feed(client,new).json()['profiles'][0]['config']!=config(a)
    assert feed(client,first).status_code==404
    assert client.post('/api/devices/'+a+'/rotate',json=request).status_code==200
    assert feed(client,new).status_code==200
    assert client.get('/api/subscriptions/'+sub['id']+'/config').json()['config']==config(sub['id'])


def test_rotate_url_disable_url_and_revoke_peer_are_distinct(setup_devices):
    client,_,calls,_,a,b=setup_devices
    old=enable(client,a);other=enable(client,b)
    rotated=change(client,a,'rotate',1).json()
    assert rotated['version']==2 and feed(client,old).status_code==404
    assert change(client,a,'rotate',1).status_code==409
    assert change(client,a,'disable',2).json()=={'enabled':False,'version':3,'url':None}
    assert feed(client,rotated).status_code==404
    assert client.get('/api/devices/'+a+'/config').status_code==200
    live=enable(client,a,3)
    first=feed(client,live).json()
    assert client.delete('/api/devices/'+a).status_code==200
    assert feed(client,live).status_code==409
    asyncio.run(service.process_devices())
    final=feed(client,live).json()
    assert final['state']=='revoked' and final['profiles']==[] and final['revision']>first['revision']
    assert any(x.get('disabled') and x['id']==a for x in calls)
    assert feed(client,other).json()['state']=='active'
    assert change(client,a,'enable',4).status_code==409


def test_parent_expiry_and_corrupt_data_fail_without_leaking(setup_devices):
    client,_,_,sub,a,_=setup_devices
    link=enable(client,a);doc=feed(client,link).json()
    execute('UPDATE subscriptions SET expires=? WHERE id=?',(int(time.time())-5,sub['id']))
    expired=feed(client,link).json()
    assert expired['state']=='expired' and expired['profiles']==[] and expired['revision']==doc['revision']+1
    execute('UPDATE subscriptions SET expires=? WHERE id=?',(sub['expires'],sub['id']))
    execute('UPDATE devices SET secret=? WHERE id=?',('corrupt-private-fixture',a))
    r=feed(client,link)
    assert r.status_code==503 and 'corrupt-private-fixture' not in r.text


def test_owner_isolation_trial_and_private_headers(setup_devices):
    client,_,_,sub,a,b=setup_devices
    link=enable(client,a)
    r=feed(client,link)
    assert r.headers['cache-control']=='no-store' and r.headers['referrer-policy']=='no-referrer'
    assert r.headers['x-robots-tag']=='noindex, nofollow, noarchive'
    client.post('/api/auth/logout')
    assert client.get(path(a)+'/link').status_code==401
    register(client,'other@example.com')
    for did in (a,b):
        assert client.get(path(did)).status_code==404
        assert client.get(path(did)+'/link').status_code==404
        assert change(client,did,'enable',0).status_code==404
    assert feed(client,link).status_code==200
    execute("UPDATE subscriptions SET access_source='trial' WHERE id=?",(sub['id'],))
    assert feed(client,link).status_code==404
    assert client.get('/flow/wireguard/device/short').status_code==404


def test_pending_and_other_protocol_cannot_issue_links(issued):
    client,issue,_=issued
    sub=issue('xui')
    d=add(client,sub['id']).json()
    assert change(client,d['id'],'enable',0).status_code==404
    wg=issue('wg')
    pending=add(client,wg['id']).json()
    assert change(client,pending['id'],'enable',0).status_code==409
    assert one('SELECT count(*) AS n FROM flow_wireguard_device_links')['n']==0
