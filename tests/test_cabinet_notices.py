import asyncio
import time
from uuid import uuid4

from app import cabinet_notices as notices, config, service
from app.db import execute, one, db
from test_shop import client
from test_wg_peer_diagnostics import setup_user, SID


def test_changes_and_expiry_use_cabinet_without_shop_bot(client,monkeypatch):
    setup_user(client)
    now = int(time.time())
    execute('UPDATE users SET telegram_id=781 WHERE id=1')
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(str(uuid4()),781,now))
    monkeypatch.setattr(config,'BOT_TOKEN','')
    execute("UPDATE subscriptions SET device_limit=9,expires=? WHERE id=?",(now+600,SID))
    items = notices.claim(now)
    assert len(items) == 2
    assert all(i['telegram_id']==781 and i['text'].startswith('Pushok ИИ') for i in items)
    assert '→ 9' in items[0]['text'] and 'МСК' in items[0]['text']
    assert items[1]['renew'] and 'МСК' in items[1]['text']
    assert not notices.claim(now+10)
    assert client.post('/api/flow/cabinet-notices/claim',json={}).status_code==403
    old=items[1]
    execute('UPDATE subscriptions SET expires=? WHERE id=?',(now+30*86400,SID))
    assert not any(i['renew'] for i in notices.claim(now+100))
    assert one('SELECT id FROM cabinet_vpn_notices WHERE id=?',(old['id'],)) is None
    execute("UPDATE subscriptions SET state='revoked' WHERE id=?",(SID,))
    assert any('отозвана' in i['text'] for i in notices.claim(now+101))
    with db() as conn:
        count=conn.execute('SELECT count(*) FROM cabinet_vpn_notices').fetchone()[0]
        conn.execute('UPDATE subscriptions SET reminder=0 WHERE id=?',(SID,))
        assert conn.execute('SELECT count(*) FROM cabinet_vpn_notices').fetchone()[0]==count


def test_claim_token_ack_and_repeat_do_not_duplicate_reminder(client,monkeypatch):
    import hashlib, hmac, json
    setup_user(client)
    now=int(time.time())
    execute('UPDATE users SET telegram_id=782 WHERE id=1')
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(str(uuid4()),782,now))
    key='ab'*32
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',key)
    def post(action, obj):
        data=json.dumps(obj).encode()
        raw=notices.DOMAIN+str(now).encode()+b':'+action.encode()+b':'+data
        sig=hmac.new(bytes.fromhex(key),raw,hashlib.sha256).hexdigest()
        return client.post('/api/flow/cabinet-notices/'+action,content=data,
            headers={'X-Pushok-Management':str(now)+':'+sig,'Content-Type':'application/json'})
    [event]=post('claim',{}).json()['items']
    ack={k:event[k] for k in ('id','lease')}
    post('finish',{**ack,'lease':'0'*32,'status':'sent'})
    assert one('SELECT reminder FROM subscriptions')['reminder']==0
    assert post('finish',{**ack,'status':'sent'}).json()=={'ok':True}
    assert post('finish',{**ack,'status':'sent'}).json()=={'ok':True}
    assert post('claim',{}).json()=={'items':[]}
    assert one('SELECT reminder FROM subscriptions')['reminder']==1
    execute('UPDATE subscriptions SET expires=expires+86400 WHERE id=?',(SID,))
    assert one('SELECT reminder FROM subscriptions')['reminder']==0


def test_plan_notice_tracks_recipient_and_resumes_only_unsent_tariffs(client,monkeypatch):
    import hashlib,hmac,json
    from app import flow_presence
    setup_user(client)
    uid,subject,now,key=784,str(uuid4()),int(time.time()),'ab'*32
    execute('UPDATE users SET telegram_id=? WHERE id=1',(uid,))
    execute('INSERT INTO flow_bridge_identities VALUES (?,?,1,?)',(subject,uid,now))
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET',key)
    def exchange(action,body):
        raw=json.dumps(body).encode()
        signature=hmac.new(bytes.fromhex(key),notices.DOMAIN+str(now).encode()+b':'+action.encode()+b':'+raw,hashlib.sha256).hexdigest()
        return client.post('/api/flow/cabinet-notices/'+action,content=raw,
            headers={'X-Pushok-Management':str(now)+':'+signature})
    def record(status):
        item=next(x for x in notices.claim(now+2,True) if x['category']=='premium')
        assert item['telegram_id']==uid
        assert exchange('finish',dict(id=item['id'],lease=item['lease'],status=status)).status_code==200
    def state():
        asyncio.run(flow_presence.invalidate())
        return asyncio.run(flow_presence.snapshot())['accounts'][subject]['plan_notice']
    execute("INSERT INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload) VALUES(1,?,'premium','{}')",(SID,))
    record('waiting_chat')
    assert state()['delivery']=='waiting_chat' and 'Оплата за счёт заведения' in state()['text']
    assert exchange('resume-chat',{'telegram_id':uid+1}).is_success
    assert not notices.claim(now+2,True)
    assert exchange('resume-chat',{'telegram_id':uid}).is_success
    record('blocked')
    assert state()['delivery']=='blocked'
    assert exchange('resume-chat',{'telegram_id':uid}).is_success
    record('sent')
    assert state()['delivery']=='sent'
    assert exchange('resume-chat',{'telegram_id':uid}).is_success
    assert not notices.claim(now+4,True)
    execute("UPDATE cabinet_vpn_notices SET outcome=NULL WHERE kind='premium'")
    assert state()['delivery']=='unknown'
