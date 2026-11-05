"""Synthetic data used only by the disposable Docker staging job. No provider calls."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time


def seed(data):
    from app import config as c,servers
    from app.db import init_db,db
    from app.security import password_hash,digest
    init_db();servers.import_environment();now=int(time.time())
    def insert(conn,table,**values):
        conn.execute('INSERT INTO '+table+'('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
    with db() as conn:
        for uid in (1,2,3):insert(conn,'users',id=uid,email='fixture'+str(uid)+'@example.invalid',password=password_hash(data['password']),created=now)
        for protocol in ('xui','wg'):
            profile=({'url':'http://127.0.0.1:9','username':'fixture','password':'fixture','inbound_id':1,'flow':'','link_template':'vless://{uuid}@example.invalid:443'} if protocol=='xui' else {'url':'http://127.0.0.1:9','token':'fixture-token-'+'a'*32})
            insert(conn,'servers',id='fixture-'+protocol,name='Fixture',protocol=protocol,state='active',health='ok',checked_at=now,settings=c.encrypt(json.dumps(profile)),created=now)
        insert(conn,'coupons',id='fixture-coupon',code='FIXTURE10',percent=10,enabled=1,version=1,created=now,updated=now)
        for oid,uid,kind,method,currency,amount,days,status in (
            ('purchase',1,'purchase','manual','RUB',180,30,'active'),
            ('friend',2,'purchase','manual','RUB',199,30,'active'),
            ('bonus',1,'referral','referral','BONUS',0,2,'paid'),
            ('trial',3,'trial','trial','XTR',0,1,'paid')):
            values=dict(id=oid,user_id=uid,protocol='xui' if kind!='trial' else 'wg',plan='month' if kind=='purchase' else kind,
                plan_name='Fixture',days=days,amount=amount,base_amount=199 if kind=='purchase' else 0,currency=currency,method=method,kind=kind,status=status,
                created=now,device_limit=4 if kind!='trial' else 0)
            if oid=='purchase':values.update(discount=19,promo_id='fixture-coupon',promo_code='FIXTURE10',promo_percent=10,promo_version=1)
            insert(conn,'orders',**values)
        insert(conn,'coupon_uses',coupon_id='fixture-coupon',user_id=1,order_id='purchase',created=now)
        insert(conn,'subscriptions',id=data['subscription'],user_id=1,protocol='xui',expires=now+86400*30,state='active',secret=c.encrypt(data['config']),credential_id=c.encrypt(data['credential']),server_id='fixture-xui',device_limit=4)
        insert(conn,'subscriptions',id=data['trial_subscription'],user_id=3,protocol='wg',expires=0,state='pending',server_id='fixture-wg',device_limit=0,access_source='trial')
        insert(conn,'trial_claims',user_id=3,order_id='trial',protocol='wg',created=now,policy_version=1,terms_snapshot='Fixture terms')
        insert(conn,'referral_codes',user_id=1,code='a'*24,created=now)
        insert(conn,'referral_bindings',invitee_id=2,inviter_id=1,days=7,min_rub=199,min_stars=150,policy_version=1,created=now)
        insert(conn,'referral_rewards',invitee_id=2,inviter_id=1,order_id='friend',days=7,created=now)
        insert(conn,'referral_redemptions',user_id=1,request_id=data['redemption'],subscription_id=data['subscription'],order_id='bonus',days=2,created=now)
        insert(conn,'devices',id=data['device'],subscription_id=data['subscription'],request_id=data['device'],name='Fixture device',state='active',expires=now+86400*30,secret=c.encrypt(data['config']),credential_id=c.encrypt(data['device']),created=now)
        insert(conn,'devices',id=data['pending_device'],subscription_id=data['subscription'],request_id=data['pending_device'],name='Pending fixture',state='pending',credential_id=c.encrypt(data['pending_device']),created=now)
        insert(conn,'primary_rotations',id='fixture-primary',subscription_id=data['subscription'],request_id=data['credential'],old_credential_id=c.encrypt(data['device']),new_credential_id=c.encrypt(data['credential']),state='completed',created=now)
        insert(conn,'device_rotations',id='fixture-rotation',device_id=data['device'],request_id=data['device'],old_credential_id=c.encrypt(data['credential']),new_credential_id=c.encrypt(data['device']),state='completed',created=now)
        insert(conn,'subscription_links',subscription_id=data['subscription'],token_hash=digest(data['link']),token_secret=c.encrypt(data['link']),enabled=1,created=now)
        insert(conn,'sessions',token=digest(data['session']),user_id=1,expires=now+86400)
        insert(conn,'tickets',token=digest(data['ticket']),user_id=1,expires=now+86400)
    return {'seeded':True}


def snapshot(database=None):
    path=Path(database) if database else Path(os.environ.get('DATA_DIR','data'))/'shop.db'
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        conn.execute('BEGIN')
        if conn.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or conn.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Invalid staging database')
        records={};business={};counts={};tables=[]
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            tables.append(name)
            quoted='"'+name.replace('"','""')+'"';rows=conn.execute('SELECT * FROM '+quoted).fetchall();counts[name]=len(rows)
            # Новые пустые таблицы рекламы не меняют данные старой схемы; их наличие проверяется отдельно.
            if name in ('ads_policy','ads_requests','ads_events','ads_payment_entries','ads_sites','ads_placements','ads_placement_actions','ads_bookings','ads_booking_actions','ads_expenses','ads_expense_actions') and not rows:continue
            if name not in ('sessions','tickets'):
                records[name]=sorted(json.dumps(row,ensure_ascii=True) for row in rows)
                if name=='users':
                    columns=[item[1] for item in conn.execute('PRAGMA table_info(users)')]
                    index=columns.index('password')
                    rows=[tuple(None if i==index else value for i,value in enumerate(row)) for row in rows]
                business[name]=sorted(json.dumps(row,ensure_ascii=True) for row in rows)
        table_digests={name:hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
            for name,rows in records.items()}
        return {'table_digests':table_digests,'digest':hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest(),
            'business_digest':hashlib.sha256(json.dumps(business,sort_keys=True).encode()).hexdigest(),'counts':counts,'tables':tables,
            'db_uid':path.stat().st_uid,'db_gid':path.stat().st_gid,'db_mode':stat.S_IMODE(path.stat().st_mode),
            'directory_mode':stat.S_IMODE(path.parent.stat().st_mode)}
    finally:conn.close()


def main():
    command=sys.argv[1]
    if command=='seed':result=seed(json.load(sys.stdin))
    elif command=='snapshot':result=snapshot(sys.argv[2] if len(sys.argv)>2 else None)
    elif command=='ticket':
        from app.db import execute
        from app.security import digest
        execute('INSERT INTO tickets VALUES (?,?,?)',(digest(json.load(sys.stdin)['ticket']),1,int(time.time())+600));result={'written':True}
    elif command=='sentinel':
        from app.db import execute
        execute("INSERT INTO meta(key,value) VALUES ('docker_staging_sentinel',?)",(json.load(sys.stdin)['value'],));result={'written':True}
    else:raise ValueError('Unknown staging command')
    print(json.dumps(result))

if __name__=='__main__':main()
