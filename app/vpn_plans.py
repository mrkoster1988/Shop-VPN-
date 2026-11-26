"""Cabinet access to the Shop catalogue and per-account renewal choice."""
import json
from uuid import UUID
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .db import db
from . import premium, service


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vpn_renewal_choices(
        subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
        plan_id TEXT REFERENCES plans(id) ON DELETE SET NULL, version INTEGER NOT NULL,
        actor TEXT NOT NULL, assigned_name TEXT, catalog_version INTEGER)""")


class Choice(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subscription_id: str=Field(min_length=1,max_length=100)
    subject: UUID
    before: int=Field(ge=0,strict=True)
    actor: UUID
    plan_id: str=Field(min_length=1,max_length=32)
    version: int=Field(ge=0,strict=True)
    plan_version: int=Field(ge=1,strict=True)


async def choose(data):
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sub=conn.execute("""SELECT s.* FROM subscriptions s JOIN flow_bridge_identities b ON b.shop_user_id=s.user_id
                WHERE s.id=? AND s.protocol='wg' AND b.mtproxy_uuid=?""",(data.subscription_id,str(data.subject))).fetchone()
            if not sub:raise HTTPException(404,'vpn_subscription_missing')
            plan=conn.execute('SELECT * FROM plans WHERE id=? AND enabled=1 AND free_traffic_gb=0 AND version=?',
                              (data.plan_id,data.plan_version)).fetchone()
            old=conn.execute('SELECT plan_id,version,catalog_version FROM vpn_renewal_choices WHERE subscription_id=?',(sub['id'],)).fetchone()
            if premium.pending(conn,sub['id']) or data.plan_id==premium.PLAN_ID or premium.active(sub):
                if (data.plan_id==premium.PLAN_ID and premium.active(sub) and not premium.pending(conn,sub['id'])):
                    return {'version':old['version'] if old else 0,'name':'Premium'}
                job=premium.choice(conn,data,dict(sub),plan,old['version'] if old else 0)
                sub=dict(sub)
            else:
                if not plan:raise HTTPException(409,'vpn_offer_changed')
                return apply_regular(conn,data,sub,plan,old)
        return await premium.apply(job,sub)


def apply_regular(conn,data,sub,plan,old):
    if old and old['plan_id']==data.plan_id and old['catalog_version']==data.plan_version:
        return {'version':old['version'],'name':plan['name']}
    if (old['version'] if old else 0)!=data.version:raise HTTPException(409,'vpn_policy_changed')
    if sub['expires']!=data.before:raise HTTPException(409,'vpn_policy_changed')
    service.check_access_order(conn,sub['user_id'],ending=True)
    if (sub['state']=='pending' or conn.execute('SELECT 1 FROM vpn_access_changes WHERE subscription_id=? AND applied IS NULL',(sub['id'],)).fetchone()
            or conn.execute("SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'",(sub['id'],)).fetchone()):
        raise HTTPException(409,'vpn_pending_operation')
    used=conn.execute("SELECT count(*) FROM devices WHERE subscription_id=? AND state!='revoked'",(sub['id'],)).fetchone()[0]
    if used>plan['device_limit']:raise HTTPException(409,'vpn_profiles_in_use')
    conn.execute('INSERT OR REPLACE INTO vpn_renewal_choices VALUES(?,?,?,?,?,?)',
                 (sub['id'],data.plan_id,data.version+1,str(data.actor),plan['name'],plan['version']))
    last_notice=conn.execute('SELECT coalesce(max(id),0) FROM cabinet_vpn_notices').fetchone()[0]
    conn.execute('''UPDATE subscriptions SET device_limit=?,access_source=?,free_plan_id=?,free_traffic_gb=0,
        free_auto_renew=?,free_max_renewals=?,free_renewals_used=0 WHERE id=?''',
        (plan['device_limit'],sub['access_source'] if sub['access_source']=='purchase' and not plan['is_free'] else 'admin',
         plan['id'] if plan['is_free'] else None,plan['free_auto_renew'],plan['free_max_renewals'],sub['id']))
    payload=json.dumps(dict(name=plan['name'],days=plan['days'],stars=0 if plan['is_free'] else plan['stars'],
                            device_limit=plan['device_limit']),ensure_ascii=False)
    # Enrich the trigger's event from this transaction rather than sending two notices.
    updated=conn.execute("""UPDATE cabinet_vpn_notices SET kind='renewal_plan',payload=?
        WHERE id>? AND kind='changed' AND subscription_id=?""",(payload,last_notice,sub['id'])).rowcount
    if not updated:
        conn.execute("""INSERT INTO cabinet_vpn_notices(user_id,subscription_id,kind,payload)
            VALUES(?,?,'renewal_plan',?)""",(sub['user_id'],sub['id'],payload))
    return {'version':data.version+1,'name':plan['name']}



async def catalog(action,body):
    # Reuse the Shop administrator's validation, optimistic version and change log.
    from . import main
    if action=='plans':
        result=await main.admin_plans()
        return {**result,'premium':{'id':premium.PLAN_ID,'name':'Premium','version':1,'lifetime':True}}
    if action=='plan-choice':return await choose(Choice.model_validate_json(body))
    data=json.loads(body)
    plan_id=data.pop('id')
    if action=='plan-create':return await main.create_plan(main.PlanCreate(id=plan_id,**data))
    return await main.update_plan(plan_id, main.PlanUpdate.model_validate(data))
