"""Account-scoped Telegram Mini App checkout, delegated by the cabinet."""
import hashlib
import time
from typing import Literal
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import cabinet_stars, config, flow_identity_bridge, payment_policy, providers, runtime_settings, shop_settings
from .cabinet_vpn import authorize
from .db import all_rows, db

DOMAIN = b'pushok-miniapp-vpn-checkout-v1:'


class PlanChoice(BaseModel):
    model_config = {'extra': 'forbid'}
    plan: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,31}$')
    plan_version: int = Field(ge=1, strict=True)
    amount: int = Field(gt=0, le=100000, strict=True)
    policy_version: int = Field(ge=0, strict=True)
    accept_terms: Literal[True]
    request_id: UUID


class Identity(BaseModel):
    model_config = {'extra': 'forbid'}
    subject: UUID
    telegram_id: int = Field(gt=0,lt=2**52,strict=True)
    nonce: UUID


class Gift(Identity):
    gift_id: UUID
    days: Literal[30,90]


class Checkout(Identity):
    choice: PlanChoice


class Status(Identity):
    order_id: UUID


def router_for(purchase_model, purchase, order_status):
    router = APIRouter(prefix='/api/flow/miniapp-vpn',include_in_schema=False)

    async def account(action, identity, request):
        raw = await request.body()
        signature = request.headers.get('X-Pushok-Management','')
        authorize(action.encode()+b':'+raw,signature,DOMAIN)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            uid = flow_identity_bridge.bind_identity_locked(conn,{
                'subject':str(identity.subject),'telegram_id':identity.telegram_id,
                'nonce_hash':hashlib.sha256(identity.nonce.bytes).hexdigest(),'expires':int(time.time())+100})
            return dict(conn.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())

    def reply(value):
        return JSONResponse(value,headers={'Cache-Control':'private, no-store'})

    @router.post('/gift')
    async def gift(data: Gift, request: Request):
        from . import service, premium
        user = await account('gift',data,request)
        uid = user['id']
        from . import flow_auto_provision
        if flow_auto_provision.enqueue(uid) == 'pending':
            return reply({'expires':None})
        oid = str(uuid5(NAMESPACE_URL, 'pushok:hall-gift:'+str(data.gift_id)))
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            order = conn.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if order and (order['user_id']!=uid or order['days']!=data.days or order['method']!='hall_gift'):
                raise HTTPException(409,'gift_conflict')
            if not order:
                if premium.checkout_blocked(uid):
                    raise HTTPException(409,'premium_subscription')
                sub=conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND protocol='wg'",(uid,)).fetchone()
                if sub and sub['state'] not in ('active','expired'):
                    raise HTTPException(409,'subscription_unavailable')
                service.check_access_order(conn,uid,ending=True)
                conn.execute("INSERT INTO orders(id,user_id,protocol,plan,plan_name,days,amount,base_amount,currency,method,status,created,kind,device_limit) VALUES (?,?,'wg','hall_gift','Подарок за приглашения',?,0,0,'BONUS','hall_gift','paid',?,'referral',?)",
                             (oid,uid,data.days,int(time.time()),sub['device_limit'] if sub else 0))
        await service.fulfill(oid)
        with db() as conn:
            order=conn.execute('SELECT status,target_expires FROM orders WHERE id=?',(oid,)).fetchone()
        return reply({'expires':order['target_expires'] if order['status']=='active' else None})

    @router.post('/plans')
    async def plans(identity: Identity, request: Request):
        user = await account('plans',identity,request)
        from . import premium
        if premium.checkout_blocked(user['id']):
            return reply({'plans':[],'available':False,'premium':True,'terms':shop_settings.terms()})
        items = all_rows('SELECT id,name,days,stars AS amount,version FROM plans WHERE enabled=1 AND is_free=0 AND stars>0 ORDER BY position,id')
        options,_ = runtime_settings.catalog_options(user,providers.available)
        policy = payment_policy.catalog(user)
        enabled = (any(o['id']=='wg' and o['available'] for o in options) and policy['payment_available']
                   and (bool(config.BOT_TOKEN) or cabinet_stars.available(user,'wg')) and shop_settings.terms_configured())
        preference = all_rows("""SELECT p.id FROM vpn_renewal_choices c JOIN subscriptions s ON s.id=c.subscription_id
            JOIN plans p ON p.id=c.plan_id WHERE s.user_id=? AND s.protocol='wg' AND p.enabled=1 AND p.is_free=0""",(user['id'],))
        return reply({'preferred_plan':preference[0]['id'] if preference else None,'plans':items,'available':bool(enabled),'issuer':'cabinet' if cabinet_stars.available(user,'wg') else 'shop','terms':shop_settings.terms(),
                      'policy_version':policy['payment_policy_version']})

    @router.post('/checkout')
    async def checkout(data: Checkout, request: Request):
        user = await account('checkout',data,request)
        c = data.choice
        args = purchase_model(protocol='wg',plan=c.plan,plan_version=c.plan_version,quoted_amount=c.amount,
                              quote_currency='XTR',payment_policy_version=c.policy_version,accept_terms=True)
        oid = str(uuid5(NAMESPACE_URL,f'pushok:miniapp:wg:{user["id"]}:{c.request_id}'))
        row = await purchase(args,user,order_id=oid,cabinet_checkout=True)
        return reply({k:row[k] for k in ('id','status','payment_url','amount','currency')})

    @router.post('/status')
    async def status(data: Status, request: Request):
        user = await account('status',data,request)
        row = await order_status(str(data.order_id),user)
        if row['protocol']!='wg' or row['method']!='stars':
            raise HTTPException(404)
        return reply({k:row[k] for k in ('id','status','amount','currency','payment_url')})

    return router
