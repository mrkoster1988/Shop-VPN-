import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from app import main
from app.db import one, execute, all_rows
from app.security import digest, password_valid
from test_shop import client, register, order

OLD='correct-horse-123'
NEW='new-correct-horse-456'
def change(client, **kwargs):
    return client.post('/api/auth/password',json={'current_password':OLD,'new_password':NEW},**kwargs)

def test_password_rotation_revokes_sessions_and_tickets_preserves_purchases(client):
    register(client)
    old=client.cookies.get('session')
    purchase=order(client)
    before=all_rows('SELECT * FROM orders')
    encoded=one('SELECT password FROM users')['password']
    client.post('/api/auth/login',json={'email':'user@example.com','password':OLD})
    second=client.cookies.get('session')
    execute('INSERT INTO tickets VALUES (?,?,?)',(digest('old-ticket'),1,9999999999))
    execute('INSERT INTO users(id,email,created) VALUES (2,?,0)',('other@example.com',))
    execute('INSERT INTO sessions VALUES (?,?,?)',(digest('other-session'),2,9999999999))
    assert client.get('/api/me').json()['can_change_password'] is True
    response=change(client)
    assert response.status_code==200 and response.json()=={'ok':True}
    fresh=client.cookies.get('session')
    assert fresh not in (old,second)
    assert len(all_rows('SELECT * FROM sessions WHERE user_id=1'))==1
    assert one('SELECT * FROM sessions WHERE token=?',(digest(fresh),))
    assert one('SELECT * FROM sessions WHERE user_id=2')
    assert not one('SELECT * FROM tickets WHERE user_id=1')
    assert all_rows('SELECT * FROM orders')==before
    assert one('SELECT password FROM users WHERE id=1')['password']!=encoded
    for cookie in (old,second):
        assert client.get('/api/me',headers={'Cookie':'session='+cookie}).status_code==401
    assert client.get('/login?ticket=old-ticket',follow_redirects=False).status_code==401
    assert client.post('/api/auth/login',json={'email':'user@example.com','password':OLD}).status_code==401
    assert client.post('/api/auth/login',json={'email':'user@example.com','password':NEW}).status_code==200
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=lax' in response.headers['set-cookie'].lower()

@pytest.mark.parametrize('payload,status',[
    ({'current_password':'wrong-secret','new_password':NEW},403),
    ({'current_password':OLD,'new_password':OLD},422),
    ({'current_password':OLD,'new_password':'short-secret'[:9]},422),
    ({'current_password':OLD,'new_password':'s'*129},422),
    ({'current_password':OLD,'new_password':NEW,'extra':'private-extra'},422),
    ({'current_password':123,'new_password':NEW},422),
])
def test_password_rejections_hide_input(client,payload,status):
    register(client)
    before=one('SELECT password FROM users')['password']
    response=client.post('/api/auth/password',json=payload)
    assert response.status_code==status
    for value in payload.values():
        if isinstance(value,str):assert value not in response.text
    assert one('SELECT password FROM users')['password']==before
    assert client.get('/api/me').status_code==200

def test_password_auth_origin_and_telegram_boundary(client):
    assert change(client).status_code==401
    register(client)
    assert change(client,headers={'Origin':'https://evil.example'}).status_code==403
    execute('UPDATE users SET email=NULL,password=NULL WHERE id=1')
    assert client.get('/api/me').json()['can_change_password'] is False
    assert change(client).status_code==409

def test_password_transaction_rolls_back_on_session_failure(client,monkeypatch):
    register(client)
    before=one('SELECT password FROM users')['password']
    execute('INSERT INTO tickets VALUES (?,?,?)',(digest('ticket'),1,9999999999))
    def fail(*args):raise sqlite3.OperationalError('synthetic session failure')
    monkeypatch.setattr(main,'session',fail)
    with pytest.raises(sqlite3.OperationalError):change(client)
    assert one('SELECT password FROM users')['password']==before
    assert one('SELECT * FROM tickets')
    assert client.get('/api/me').status_code==200

def test_inflight_old_login_cannot_create_session_after_change(client,monkeypatch):
    register(client)
    cookie=client.cookies.get('session')
    original=main.password_valid
    entered=threading.Event();release=threading.Event()
    count=0
    def blocked(password,encoded):
        nonlocal count
        count+=1
        if count==1:
            entered.set();assert release.wait(10)
        return original(password,encoded)
    monkeypatch.setattr(main,'password_valid',blocked)
    with ThreadPoolExecutor() as pool:
        pending=pool.submit(client.post,'/api/auth/login',json={'email':'user@example.com','password':OLD})
        try:
            assert entered.wait(5)
            assert change(client,headers={'Cookie':'session='+cookie}).status_code==200
        finally:release.set()
        assert pending.result().status_code==401
    assert len(all_rows('SELECT * FROM sessions'))==1

def test_logout_while_password_verification_is_pending(client,monkeypatch):
    register(client)
    original=main.password_valid
    entered=threading.Event();release=threading.Event()
    def blocked(*args):
        entered.set();assert release.wait(10);return original(*args)
    monkeypatch.setattr(main,'password_valid',blocked)
    with ThreadPoolExecutor() as pool:
        pending=pool.submit(change,client)
        try:
            assert entered.wait(5)
            assert client.post('/api/auth/logout').status_code==200
        finally:release.set()
        assert pending.result().status_code==401
    assert password_valid(OLD,one('SELECT password FROM users')['password'])
    assert not one('SELECT * FROM sessions')

def test_two_simultaneous_changes_only_one_commits(client,monkeypatch):
    register(client)
    cookie=client.cookies.get('session')
    original=main.password_hash
    barrier=threading.Barrier(2)
    def together(value):
        result=original(value);barrier.wait(timeout=10);return result
    monkeypatch.setattr(main,'password_hash',together)
    passwords=[NEW,'another-new-password-789']
    def send(password):
        return client.post('/api/auth/password',headers={'Cookie':'session='+cookie},json={'current_password':OLD,'new_password':password})
    with ThreadPoolExecutor() as pool:results=list(pool.map(send,passwords))
    assert sorted(r.status_code for r in results)==[200,401]
    winner=passwords[next(i for i,r in enumerate(results) if r.status_code==200)]
    assert password_valid(winner,one('SELECT password FROM users')['password'])
    assert len(all_rows('SELECT * FROM sessions'))==1
