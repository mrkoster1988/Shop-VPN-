"""Local aggregate report. No provider requests, identifiers or raw error messages."""
import os
import time
import sqlite3
from contextlib import closing
from . import config as c, shop_settings

VERSION='v0.051'
QUEUE_SPECS={
    'issuance':('orders',"status='paid'"),
    'payment_poll':('orders',"status='pending' AND method='yookassa' AND payment_id IS NOT NULL"),
    'devices':('devices',"state IN ('pending','revoking') AND NOT EXISTS (SELECT 1 FROM device_rotations r WHERE r.device_id=devices.id AND r.state NOT IN ('completed','canceled'))"),
    'device_rotations':('device_rotations',"state NOT IN ('completed','canceled')"),
    'primary_rotations':('primary_rotations',"state NOT IN ('completed','canceled')")}


def report(worker):
    now=int(time.time());warnings=[]
    def warn(code):
        if code not in warnings:warnings.append(code)
    try:c.cipher();key_valid=True
    except Exception:key_valid=False;warn('encryption_key_invalid')
    enabled=os.getenv('BACKGROUND_TASKS','true')=='true' and os.getenv('MAINTENANCE_MODE')!='true'
    last=worker.get('last_success');started=worker.get('last_started');failed=worker.get('last_failure')
    if not enabled:worker_state='disabled'
    elif failed is not None and (last is None or failed>=last):worker_state='failed'
    elif started is None:worker_state='not_observed'
    elif started>now:worker_state='stale'
    elif worker.get('running'):
        worker_state='busy' if now-started<=180 else 'slow'
    elif last is None or now-last>180 or last>now:worker_state='stale'
    else:worker_state='recent_success'
    if worker_state in ('disabled','failed','not_observed','slow','stale'):warn('worker_'+worker_state)
    with closing(sqlite3.connect(c.DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as conn:
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('BEGIN')
        queues={}
        for name,(table,where) in QUEUE_SPECS.items():
            row=conn.execute('SELECT count(*),coalesce(sum(next_attempt<=?),0),coalesce(sum(error IS NOT NULL),0),min(created) FROM '+table+' WHERE '+where,(now,)).fetchone()
            queues[name]={'pending':row[0],'due':row[1],'with_error':row[2],
                'oldest_age_seconds':max(0,now-row[3]) if row[3] is not None else None}
            if row[2]:warn('queue_errors')
            if name!='payment_poll' and row[3] is not None and now-row[3]>=600:warn('queue_wait_over_10m')
        expiration=conn.execute("SELECT count(*),min(expires) FROM subscriptions WHERE state='active' AND expires<=?",(now,)).fetchone()
        expired={'pending':expiration[0],'oldest_age_seconds':max(0,now-expiration[1]) if expiration[1] is not None else None}
        if expiration[0] and now-expiration[1]>=600:warn('expiry_wait_over_10m')
        if not enabled and (any(q['pending'] for q in queues.values()) or expiration[0]):warn('queues_paused')
        protocols={}
        for protocol in ('xui','wg'):
            rows=conn.execute("""SELECT state,health,checked_at,capacity,
                (SELECT count(*) FROM subscriptions s WHERE s.server_id=v.id AND
                 (s.state='pending' OR (s.state='active' AND s.expires>?))) AS occupied
                FROM servers v WHERE protocol=?""",(now,protocol)).fetchall()
            enabled_rows=[r for r in rows if r[0] in ('active','draining')]
            assignable=[r for r in rows if r[0]=='active' and r[1]!='error' and (r[3]==0 or r[4]<r[3])]
            recent=[r for r in enabled_rows if r[1]=='ok' and r[2] is not None and 0<=now-r[2]<=86400]
            protocols[protocol]={'registered':len(rows),'enabled':len(enabled_rows),'assignable':len(assignable),
                'cached_api_ok_24h':len(recent),'cached_api_error':sum(r[1]=='error' for r in enabled_rows)}
            if enabled_rows and len(recent)<len(enabled_rows):warn('server_probe_not_recent_ok')
            if enabled_rows and not assignable:warn('no_new_assignments_'+protocol)
        plans=conn.execute('SELECT count(*) FROM plans WHERE enabled=1').fetchone()[0]
        if plans==0:warn('no_active_plans')
        if not any(p['assignable'] for p in protocols.values()):warn('no_assignable_servers')
    mode=c.PAYMENT_MODE if c.PAYMENT_MODE in ('manual','yookassa') else 'invalid'
    payment_configured=mode=='manual' or (mode=='yookassa' and bool(os.getenv('YOOKASSA_SHOP_ID') and os.getenv('YOOKASSA_SECRET')))
    if not payment_configured:warn('payment_settings_incomplete')
    telegram=bool(c.BOT_TOKEN);stars_ready=bool(telegram and c.BOT_USERNAME and shop_settings.terms_configured())
    if telegram and not stars_ready:warn('telegram_sales_settings_incomplete')
    return {'format':1,'release':VERSION,'generated_at':now,'status':'needs_attention' if warnings else 'local_checks_ok',
        'warnings':warnings,'worker':{'state':worker_state,'last_success':last,'last_started':started,'last_failure':failed},
        'queues':queues,'expiry':expired,'servers':protocols,'settings':{'encryption_key_format_valid':key_valid,
        'payment_mode':mode,'payment_configured':payment_configured,'telegram_configured':telegram,
        'telegram_sales_configured':stars_ready,'enabled_plans':plans},
        'scope':{'external_requests':False,'ciphertext_scan':False,'vpn_traffic_verified':False,
                 'real_payments_verified':False,'backup_verified':False,'worker_process_only':True}}
