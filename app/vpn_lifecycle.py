"""Reissue a revoked subscription with a fresh primary key and a fixed expiry on retries."""
import time
from uuid import uuid4
from fastapi import HTTPException
from . import admin_cleanup, config, providers, service, servers
from .db import db


def install(conn):
    conn.executescript("""CREATE TABLE IF NOT EXISTS vpn_access_changes(
        request_id TEXT PRIMARY KEY,subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        actor TEXT NOT NULL, action TEXT NOT NULL, before_expiry INTEGER NOT NULL, days INTEGER NOT NULL,
        target INTEGER NOT NULL, identity TEXT NOT NULL, applied INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS vpn_access_pending ON vpn_access_changes(subscription_id) WHERE applied IS NULL;""")


async def change(cmd):
    sid=cmd.subscription_id; rid=str(cmd.request_id); days=cmd.value
    if (cmd.action in ('expire','revoke') and days!=0) or (cmd.action=='restore' and not 1<=days<=365):
        raise HTTPException(422,'vpn_days_range')
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job=conn.execute('SELECT * FROM vpn_access_changes WHERE request_id=?',(rid,)).fetchone()
            if job and (job['subscription_id'],job['action'],job['before_expiry'],job['days'])!=(
                sid,cmd.action,cmd.before,days):raise HTTPException(409,'vpn_changed')
            if job and job['applied']:return
            raw=conn.execute('SELECT * FROM subscriptions WHERE id=?',(sid,)).fetchone()
            if not raw:raise HTTPException(404,'vpn_subscription_missing')
            sub=dict(raw)
            if sub['expires']!=cmd.before or (sub['state']=='revoked')!=(cmd.action=='restore'):
                raise HTTPException(409,'vpn_changed')
            if cmd.action=='expire' and sub['state']!='active' and not (job and sub['state']=='expired'):
                raise HTTPException(409,'vpn_changed')
            if not job:
                service.check_access_order(conn, sub['user_id'], ending=cmd.action in ('expire', 'revoke'))
            if admin_cleanup._unfinished_rotation(conn,sid):
                raise HTTPException(409,'vpn_key_rotation')
            if cmd.action=='restore':
                try:
                    _,profile=servers.for_subscription(sub)
                except Exception:
                    raise HTTPException(409,'vpn_node_unavailable') from None
                if profile.get('api')!='wgdashboard' or not profile.get('schedule_expiry'):
                    raise HTTPException(409,'vpn_expiry_schedule_required')
            if conn.execute("SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'",(sid,)).fetchone():
                raise HTTPException(409,'vpn_pending_operation')
            if not job:
                if conn.execute('SELECT 1 FROM vpn_access_changes WHERE subscription_id=? AND applied IS NULL',(sid,)).fetchone():
                    raise HTTPException(409,'vpn_pending_operation')
                conn.execute('INSERT INTO vpn_access_changes VALUES(?,?,?,?,?,?,?,?,NULL)',
                    (rid,sid,str(cmd.actor),cmd.action,cmd.before,days,int(time.time())+days*86400,str(uuid4())))
                job=conn.execute('SELECT * FROM vpn_access_changes WHERE request_id=?',(rid,)).fetchone()
        if cmd.action=='expire':
            try:
                await service.disable_subscription_peers(sub)
            except Exception:
                raise HTTPException(502,'vpn_node_unconfirmed') from None
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                end=min(sub['expires'],job['target'])
                if not conn.execute("""UPDATE subscriptions SET state='expired',expires=?,free_auto_renew=0
                        WHERE id=? AND state IN ('active','expired') AND expires=?""",(end,sid,cmd.before)).rowcount:
                    raise HTTPException(409,'vpn_changed')
                conn.execute("UPDATE devices SET expires=MIN(expires,?) WHERE subscription_id=? AND state='active'",(end,sid))
                conn.execute("UPDATE flow_auto_grants SET state='expired',target_expires=? WHERE subscription_id=? AND state='active'",(end,sid))
                conn.execute('UPDATE vpn_access_changes SET applied=? WHERE request_id=?',(int(time.time()),rid))
            return
        if cmd.action=='revoke':
            await admin_cleanup.revoke_locked(sid,request_id=rid)
            return
        if job['target']<=time.time():
            await providers.disable({**sub,'id':job['identity']})
            with db() as conn:conn.execute('DELETE FROM vpn_access_changes WHERE request_id=? AND applied IS NULL',(rid,))
            raise HTTPException(409,'vpn_changed')
        conf=await providers.provision({**sub,'id':job['identity'],'expires':job['target']})
        if not isinstance(conf,str) or not conf.startswith('[Interface]') or '[Peer]' not in conf:raise HTTPException(503)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("""UPDATE subscriptions SET state='active',primary_key_state='active',
                credential_id=?,secret=?,expires=?,reminder=0,rotation_error=NULL WHERE id=? AND state='revoked' AND expires=?""",
                (config.encrypt(job['identity']),config.encrypt(conf),job['target'],sid,cmd.before)).rowcount:
                raise HTTPException(409,'vpn_changed')
            conn.execute('DELETE FROM admin_subscription_revocations WHERE subscription_id=?',(sid,))
            conn.execute('UPDATE vpn_access_changes SET applied=? WHERE request_id=?',(int(time.time()),rid))
