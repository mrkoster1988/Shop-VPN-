import asyncio, time, uuid
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from agent import main as agent

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(agent,'DATA',tmp_path)
    monkeypatch.setattr(agent,'TOKEN','w'*48)
    monkeypatch.setattr(agent,'LOCK',asyncio.Lock())
    monkeypatch.setenv('WG_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('WG_ENDPOINT','vpn.example.com:51820')
    calls=[]
    def fake_wg(*args):
        calls.append(args)
        return 'server-public-key' if args[0]=='show' else ''
    monkeypatch.setattr(agent,'wg',fake_wg)
    with TestClient(agent.app) as test:
        test.headers['Authorization']='Bearer '+'w'*48
        yield test,calls

def test_peer_idempotency_and_revocation(client):
    test,calls=client; sid=str(uuid.uuid4()); expiry=int(time.time())+86400
    first=test.put('/peers/'+sid,json={'expires':expiry})
    assert first.status_code==200,first.text
    second=test.put('/peers/'+sid,json={'expires':expiry+86400})
    assert first.json()['config']==second.json()['config']
    assert 'Address = 10.77.0.2/32' in first.json()['config']
    with agent.conn() as db:
        private=db.execute('SELECT private_key FROM peers').fetchone()[0]
        assert agent.cipher().decrypt(private.encode()).decode() in first.json()['config']
        assert private not in first.json()['config']
    assert test.delete('/peers/'+sid).status_code==200 and calls[-1][-1]=='remove'
    with agent.conn() as db: assert db.execute('SELECT expires FROM peers').fetchone()[0]==0

def test_validation_and_auth(client):
    test,calls=client
    assert test.put('/peers/not-a-uuid',json={'expires':123}).status_code==422
    assert test.put('/peers/'+str(uuid.uuid4()),json={'expires':1}).status_code==422
    test.headers['Authorization']='Bearer invalid'
    assert test.get('/health').status_code==403
