"""Durable WireGuard byte deltas and names for Shop-managed peers."""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

from .db import db,all_rows,one

MOSCOW=ZoneInfo('Europe/Moscow')


def install(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS wg_peer_owners(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            label TEXT NOT NULL, checked INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS wg_observed_peers(subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            peer TEXT NOT NULL, rx INTEGER, tx INTEGER, total_rx INTEGER, total_tx INTEGER, seen INTEGER NOT NULL DEFAULT 0,
            sampled INTEGER NOT NULL, PRIMARY KEY(subscription_id,peer));
        CREATE TABLE IF NOT EXISTS wg_daily_usage(subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            day TEXT NOT NULL,rx INTEGER NOT NULL,tx INTEGER NOT NULL,PRIMARY KEY(subscription_id,day));
    """)


def clean(text):
    return ' '.join(''.join(c if c.isalnum() or c in ' @._-' else ' ' for c in str(text or '')).split())


def display(uid,identity,device=None):
    row=one("""SELECT u.telegram_id,o.label,EXISTS(SELECT 1 FROM flow_guest_trials g WHERE g.user_id=u.id) AS guest
        FROM users u LEFT JOIN wg_peer_owners o ON o.user_id=u.id WHERE u.id=?""",(uid,)) if uid else None
    tag=hashlib.sha256(identity.encode()).hexdigest()[:6]
    if not row:return 'Pushok · Профиль '+tag
    account=('TG '+str(row['telegram_id'])) if row['telegram_id'] else ('Гость ' if row['guest'] else 'Shop ')+str(uid)
    owner=clean(row['label'])[:30]
    part=clean(device)[:14] if device else 'Основной'
    return f'Pushok · {owner+" · " if owner else ""}{account} · {part} {tag}'[:80]


async def refresh_names(client):
    key=os.getenv('FLOW_SHOP_BRIDGE_SECRET','')
    if not re.fullmatch('[0-9a-f]{64}',key):return
    now=int(time.time())
    rows=all_rows("""SELECT DISTINCT u.id,u.telegram_id FROM users u JOIN subscriptions s ON s.user_id=u.id
        LEFT JOIN wg_peer_owners o ON o.user_id=u.id WHERE s.protocol='wg' AND u.telegram_id>0
        AND coalesce(o.checked,0)<? ORDER BY coalesce(o.checked,0),u.id LIMIT 200""",(now-86400,))
    if not rows:return
    raw=json.dumps({'ids':[r['telegram_id'] for r in rows]},separators=(',',':')).encode()
    stamp=str(now)
    sig=hmac.digest(bytes.fromhex(key),b'pushok-vpn-settings-v1:'+stamp.encode()+b':owners:'+raw,'sha256').hex()
    r=await client.post('https://pushok.shop/api/v1/vpn/peer-owners',content=raw,
        headers={'Content-Type':'application/json','X-Pushok-Management':stamp+':'+sig})
    r.raise_for_status()
    owners=r.json()['owners']
    with db() as conn:
        for row in rows:
            data=owners.get(str(row['telegram_id'])) or {}
            name=clean(data.get('name'))
            handle=clean(data.get('username'))
            label=(' @'+handle if handle else '').strip()
            if name:label+=' '+name
            conn.execute('INSERT OR REPLACE INTO wg_peer_owners VALUES(?,?,?)',(row['id'],label.strip()[:60],now))


def record(samples,now):
    day=datetime.fromtimestamp(now,MOSCOW).date()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for sid,key,data in samples:
            if not conn.execute('SELECT 1 FROM subscriptions WHERE id=?',(sid,)).fetchone():continue
            peer=hashlib.sha256(key.encode()).hexdigest()
            old=conn.execute('SELECT * FROM wg_observed_peers WHERE subscription_id=? AND peer=?',(sid,peer)).fetchone()
            if old and old['sampled']>=now:continue
            values=[]; deltas=[]
            for field,col in [('received','rx'),('sent','tx')]:
                value=data[field]; previous=old[col] if old else None
                delta=0 if value is None or previous is None else value-previous if value>=previous else value
                total=old['total_'+col] if old else None
                values.append((previous if value is None else value,value if total is None else total+delta))
                deltas.append(delta)
            seen=max(data['last_seen'] or 0,old['seen'] if old else 0)
            conn.execute('INSERT OR REPLACE INTO wg_observed_peers VALUES(?,?,?,?,?,?,?,?)',
                (sid,peer,values[0][0],values[1][0],values[0][1],values[1][1],seen,now))
            if any(deltas):
                conn.execute("""INSERT INTO wg_daily_usage VALUES(?,?,?,?) ON CONFLICT(subscription_id,day)
                    DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx""",(sid,day.isoformat(),*deltas))
        conn.execute('DELETE FROM wg_daily_usage WHERE day<?',((day-timedelta(days=30)).isoformat(),))
        totals=conn.execute("""SELECT subscription_id,sum(total_rx) AS received,sum(total_tx) AS sent,
            max(seen) AS seen FROM wg_observed_peers GROUP BY subscription_id""").fetchall()
        days=conn.execute('SELECT * FROM wg_daily_usage').fetchall()
    result={r['subscription_id']:dict(r) for r in totals}
    for r in result.values():
        r['periods']={p:[0,0] for p in ('today','week','month')} if r['received'] is not None or r['sent'] is not None else None
    for d in days:
        row=result.get(d['subscription_id'])
        if not row:continue
        for p,length in [('today',1),('week',7),('month',30)]:
            if (day-timedelta(days=length-1)).isoformat()<=d['day']<=day.isoformat():
                row['periods'][p][0]+=d['rx'];row['periods'][p][1]+=d['tx']
    return result
