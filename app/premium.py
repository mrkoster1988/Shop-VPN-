"""Administrator-funded WireGuard access. A v1 Flow feed requires a positive timestamp."""
import json
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException

from . import config, providers, servers, service
from .db import db

# Reserved value in legacy numeric expiry fields. Never sent to the node's scheduler.
NO_EXPIRY = 253402214400
PLAN_ID = '@premium'  # outside the public Shop plan-ID grammar


def active(sub):
    return sub['state'] == 'active' and sub['expires'] == NO_EXPIRY


def pending(conn, sid):
    return conn.execute("""SELECT * FROM vpn_access_changes
        WHERE subscription_id=? AND action='tariff' AND applied IS NULL""", (sid,)).fetchone()


def choice(conn, data, sub, plan, current_version):
    """Persist the exact keys, terms and target before contacting WGDashboard."""
    rid = str(uuid5(NAMESPACE_URL, f"pushok/tariff/{sub['id']}/{data.version}/{data.plan_id}/{data.plan_version}"))
    job = pending(conn, sub['id'])
    if job:
        if job['request_id'] != rid:
            raise HTTPException(409, 'vpn_pending_operation')
        return dict(job)
    if data.version != current_version or sub['expires'] != data.before:
        raise HTTPException(409, 'vpn_policy_changed')
    gift = data.plan_id == PLAN_ID
    if (gift and data.plan_version != 1) or (not gift and not plan):
        raise HTTPException(409, 'vpn_offer_changed')
    service.check_access_order(conn, sub['user_id'], ending=True)
    if (sub['state'] not in ('active','revoked','expired') or sub['primary_key_state'] not in ('active','revoked')
            or conn.execute("SELECT 1 FROM vpn_access_changes WHERE subscription_id=? AND applied IS NULL",(sub['id'],)).fetchone()
            or conn.execute("SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'",(sub['id'],)).fetchone()
            or conn.execute("""SELECT 1 FROM orders WHERE user_id=? AND protocol='wg'
                AND (status='checkout' OR (status='pending' AND method NOT IN ('stars','free')))""",(sub['user_id'],)).fetchone()):
        raise HTTPException(409, 'vpn_pending_operation')
    from . import admin_cleanup, flow_owner_extension
    if admin_cleanup._unfinished_rotation(conn, sub['id']):
        raise HTTPException(409,'vpn_key_rotation')
    devices = flow_owner_extension._devices(conn, sub)
    limit = sub['device_limit'] if gift else plan['device_limit']
    if len(devices) > limit:
        raise HTTPException(409, 'vpn_profiles_in_use')
    try:
        _, profile = servers.for_subscription(sub)
    except Exception:
        raise HTTPException(409, 'vpn_node_unavailable') from None
    if profile.get('api') != 'wgdashboard' or (not gift and not profile.get('schedule_expiry')):
        raise HTTPException(409, 'vpn_expiry_schedule_required')
    new_key = sub['state']=='revoked' or sub['primary_key_state']=='revoked'
    facts = dict(plan_id=data.plan_id, plan_version=data.plan_version, version=data.version,
                 name='Premium' if gift else plan['name'], limit=limit,
                 days=0 if gift else plan['days'], free=bool(not gift and plan['is_free']),
                 stars=0 if gift or plan['is_free'] else plan['stars'],
                 renew=0 if gift else plan['free_auto_renew'], maximum=0 if gift else plan['free_max_renewals'],
                 key=str(uuid4()) if new_key else service.primary_identity(sub), new_key=new_key)
    target = NO_EXPIRY if gift else int(time.time()) + facts['days']*86400
    conn.execute("""INSERT INTO vpn_access_changes(request_id,subscription_id,actor,action,before_expiry,days,target,identity)
        VALUES(?,?,?,'tariff',?,?,?,?)""",(rid,sub['id'],str(data.actor),sub['expires'],facts['days'],target,json.dumps(facts)))
    return dict(pending(conn,sub['id']))


async def apply(job, sub):
    from . import flow_owner_extension
    facts=json.loads(job['identity'])
    if facts['days'] and job['target']<=int(time.time()):
        with db() as conn:
            job['target']=int(time.time())+facts['days']*86400
            conn.execute("UPDATE vpn_access_changes SET target=? WHERE request_id=? AND applied IS NULL",(job['target'],job['request_id']))
    gift=facts['plan_id']==PLAN_ID
    with db() as conn:
        devices=flow_owner_extension._devices(conn,sub)
    async def configure(identity, existing, name=None):
        value=await providers.provision({**sub,'id':identity,'expires':job['target'],
            'require_existing_wg_key':existing,'wg_device_name':name})
        if not isinstance(value,str) or not value.startswith('[Interface]') or '[Peer]' not in value:
            raise ValueError('invalid profile')
        return config.encrypt(value)
    try:
        primary=await configure(facts['key'],not facts['new_key'])
        extra=[]
        for device in devices:
            secret=await configure(service.device_identity(device),True,device['name'])
            extra.append((secret,job['target'],device['id']))
    except Exception:
        raise HTTPException(502,'vpn_node_unconfirmed') from None
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        floor=conn.execute('SELECT coalesce(max(id),0) FROM cabinet_vpn_notices').fetchone()[0]
        if not conn.execute("""UPDATE subscriptions SET state='active',primary_key_state='active',secret=?,credential_id=?,
            expires=?,device_limit=?,access_source='admin',reminder=0,free_plan_id=?,free_traffic_gb=0,free_auto_renew=?,
            free_max_renewals=?,free_renewals_used=0,rotation_error=NULL WHERE id=? AND expires=? AND state=?""",
            (primary,config.encrypt(facts['key']),job['target'],facts['limit'],facts['plan_id'] if facts['free'] else None,
             facts['renew'],facts['maximum'],sub['id'],job['before_expiry'],sub['state'])).rowcount:
            raise HTTPException(409,'vpn_policy_changed')
        conn.executemany('UPDATE devices SET secret=?,expires=?,error=NULL WHERE id=? AND state=\'active\'', extra)
        conn.execute("DELETE FROM admin_subscription_revocations WHERE subscription_id=?",(sub['id'],))
        from . import wg_fleet
        wg_fleet.new_access(conn,sub['id'])
        conn.execute("""UPDATE flow_auto_grants SET state='active',target_expires=? WHERE subscription_id=?
            AND state IN ('active','expired')""",(job['target'],sub['id']))
        conn.execute('INSERT OR REPLACE INTO vpn_renewal_choices VALUES(?,?,?,?,?,?)',
            (sub['id'],None if gift else facts['plan_id'],facts['version']+1,job['actor'],
             None if gift else facts['name'],facts['plan_version']))
        kind='premium' if gift else 'renewal_plan'
        payload=json.dumps(dict(name=facts['name'],days=facts['days'],stars=facts['stars'],device_limit=facts['limit'],
                                new_expiry=None if gift else job['target']),ensure_ascii=False)
        updated=conn.execute("""UPDATE cabinet_vpn_notices SET kind=?,payload=?
            WHERE id>? AND subscription_id=? AND kind='changed'""",(kind,payload,floor,sub['id'])).rowcount
        if not updated:
            conn.execute('INSERT INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload) VALUES(?,?,?,?)',
                         (sub['user_id'],sub['id'],kind,payload))
        conn.execute('UPDATE vpn_access_changes SET applied=? WHERE request_id=?',(int(time.time()),job['request_id']))
    return {'name':facts['name'],'version':facts['version']+1}


async def resume_pending():
    """Recover a confirmed administrator intent after a timeout or a restart."""
    from .db import all_rows, one
    for row in all_rows("SELECT request_id FROM vpn_access_changes WHERE action='tariff' AND applied IS NULL"):
        async with service.lock:
            job=one("SELECT * FROM vpn_access_changes WHERE request_id=? AND applied IS NULL",(row['request_id'],))
            if not job:
                continue
            sub=one('SELECT * FROM subscriptions WHERE id=?',(job['subscription_id'],))
            if not sub or sub['expires']!=job['before_expiry']:
                continue
            try:
                await apply(job,sub)
            except HTTPException:
                continue


def checkout_blocked(uid):
    from .db import one
    return bool(one("""SELECT 1 FROM subscriptions s WHERE user_id=? AND protocol='wg'
        AND ((state='active' AND expires=?) OR EXISTS (SELECT 1 FROM vpn_access_changes a
        WHERE a.subscription_id=s.id AND a.action='tariff' AND a.applied IS NULL))""",(uid,NO_EXPIRY)))
