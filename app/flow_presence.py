"""Read-only connection observations for the MTProxy cabinet."""
import asyncio
import hashlib
import hmac
import ipaddress
import math
import os
import re
import time

import httpx
from fastapi import HTTPException

from . import servers, service, wgdashboard
from . import wg_client_platform
from .db import db, all_rows

DOMAIN = 'pushok-vpn-presence-v1'
_lock = asyncio.Lock()
_cached = None
_until = 0.0


async def invalidate():
    global _until
    async with _lock:
        _until = 0


def authorize(header):
    secret = os.getenv('FLOW_SHOP_BRIDGE_SECRET', '')
    if not re.fullmatch(r'[0-9a-f]{64}', secret):
        raise HTTPException(503, 'Связь с кабинетом не настроена')
    match = re.fullmatch(r'([0-9]{10}):([0-9a-f]{64})', header or '')
    if not match or abs(time.time() - int(match[1])) > 45:
        raise HTTPException(401)
    expected = hmac.new(bytes.fromhex(secret), f'{DOMAIN}:{match[1]}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match[2]):
        raise HTTPException(401)


def peer_addresses(peer):
    # WGDashboard allowed_ip contains the addresses routed to this peer.
    # endpoint is the last WireGuard source endpoint, which may be behind NAT.
    tunnel = []
    allowed = peer.get('allowed_ip')
    for value in allowed.split(',') if isinstance(allowed, str) else ():
        try:
            ip = ipaddress.ip_interface(value.strip())
        except ValueError:
            continue
        if ip.network.prefixlen == ip.max_prefixlen and not ip.ip.is_unspecified and str(ip.ip) not in tunnel:
            tunnel.append(str(ip.ip))
    endpoint = peer.get('endpoint')
    match = re.fullmatch(r'(?:\[([a-fA-F0-9:._]+)\]|([0-9.]+)):([0-9]{1,5})', endpoint or '') if isinstance(endpoint, str) else None
    source = None
    if match and 0 < int(match[3]) <= 65535:
        try:
            address = ipaddress.ip_address(match[1] or match[2])
            if not address.is_unspecified:
                source = str(address)
        except ValueError:
            pass
    return {'tunnel_ips': tunnel, 'endpoint_ip': source}


def metrics(peer, sampled_at):
    age = re.fullmatch(r'(?:(\d{1,7}) days?, )?(\d{1,2}):([0-5]\d):([0-5]\d)', str(peer.get('latest_handshake', '')))
    seconds = None
    if age:
        days, hours, minutes, sec = (int(n or 0) for n in age.groups())
        seconds = days * 86400 + hours * 3600 + minutes * 60 + sec
    def byte_count(*fields):
        numbers = [peer.get(k) for k in fields]
        if all(type(n) in (int, float) and math.isfinite(n) and 0 <= n < 1e10 for n in numbers):
            return round(sum(numbers) * 1024 ** 3)
        return None
    return {'last_seen': sampled_at - seconds if seconds is not None else None,
            'online': seconds < 180 if seconds is not None else (
                False if peer.get('latest_handshake') in ('No Handshake', 'N/A') else None),
            'received': byte_count('total_sent', 'cumu_sent'),
            'sent': byte_count('total_receive', 'cumu_receive')}


async def snapshot(maintenance=False):
    from . import wg_activity
    global _cached, _until
    async with _lock:
        if not maintenance and _cached is not None and time.monotonic()<_until:return _cached
        now=int(time.time())
        subscriptions=all_rows("""SELECT s.*,u.telegram_id,b.mtproxy_uuid,x.request_id,x.days,x.before_expires,x.target_expires,c.version AS renewal_version,c.assigned_name,rp.name AS renewal_name,rp.id AS renewal_plan_id,
            EXISTS(SELECT 1 FROM flow_auto_grants g WHERE g.subscription_id=s.id
                   AND g.state IN ('pending','expired')) AS first_grant_waiting,
            (SELECT COALESCE(NULLIF(TRIM(o.plan_name),''),p.name) FROM orders o LEFT JOIN plans p ON p.id=o.plan
             WHERE o.user_id=s.user_id AND o.protocol=s.protocol AND o.status='active'
             AND o.kind NOT IN ('referral','free_renewal')
             ORDER BY o.target_expires DESC,o.created DESC,o.id DESC LIMIT 1) AS tariff
            FROM subscriptions s JOIN users u ON u.id=s.user_id LEFT JOIN flow_bridge_identities b ON b.shop_user_id=u.id
            LEFT JOIN vpn_renewal_choices c ON c.subscription_id=s.id
            LEFT JOIN plans rp ON rp.id=c.plan_id AND rp.enabled=1
            LEFT JOIN flow_owner_extensions x ON x.subscription_id=s.id AND x.status='pending'
            AND x.reason LIKE 'Кабинет MTProxy · %' WHERE s.protocol='wg'""")
        devices={}
        for device in all_rows("SELECT d.* FROM devices d JOIN subscriptions s ON s.id=d.subscription_id WHERE s.protocol='wg' AND d.state!='revoked'"):
            devices.setdefault(device['subscription_id'],[]).append(device)
        changes={r['subscription_id']:r for r in all_rows('SELECT * FROM vpn_access_changes WHERE applied IS NULL')}
        revoked={r['subscription_id']:r['revoked_at'] for r in all_rows('SELECT subscription_id,revoked_at FROM admin_subscription_revocations')}
        with db() as conn:
            platform_observations = wg_client_platform.current(conn,now)
        latest_endpoint = {}
        rows={}; accounts={}; nodes={}; unlinked={}; samples=[]; renames=[]
        for sub in subscriptions:
            sid=sub['id']
            state='expired' if sub['state']=='active' and sub['expires']<=now else sub['state']
            row={'state':state,'expires_at':sub['expires'] or None,'subscription_id':sid,'device_limit':sub['device_limit'],
                 'online':None,'last_seen':None,'received':None,'sent':None,'activity_known':False,
                 'legacy_reminder':bool(sub['reminder']),'expected':0,'observed':0,'has_recent':False}
            row.update(renewal_version=sub['renewal_version'] or 0,renewal_name=sub['renewal_name'],renewal_plan_id=sub['renewal_plan_id'])
            row['tariff_name']=sub['assigned_name'] or sub['tariff'] or (
                'Гостевой доступ' if sub['wg_configuration']==servers.GUEST_CONFIGURATION else
                'Пробный доступ' if sub['access_source']=='trial' else None)
            # An auto-grant reservation can exist before there is a profile to download.
            row['first_profile_unissued'] = bool(
                state in ('pending','expired') and sub['first_grant_waiting']
                and sub['secret'] is None and not devices.get(sid))
            rows[sid]=row
            if sub['mtproxy_uuid']:accounts[sub['mtproxy_uuid']]=row
            elif sub['telegram_id']:unlinked[str(sub['telegram_id'])]=row
            if sub['request_id']:
                row['pending_change']={'request_id':sub['request_id'],'value':sub['days'],'before':sub['before_expires'],
                     'action':'shorten' if sub['target_expires']<sub['before_expires'] else 'extend'}
            if sid in changes:
                operation=changes[sid]
                row['pending_change']={k:operation[k] for k in ('request_id','action')}
                row['pending_change'].update(value=operation['days'],before=operation['before_expiry'])
            if state=='revoked':row['revoked_at']=revoked.get(sid)
            if state!='active':continue
            for device in [None,*devices.get(sid,[])]:
                if device is None and sub['primary_key_state']=='revoked':continue
                row['expected']+=1
                if (device['state'] if device else sub['primary_key_state'])!='active':continue
                try:
                    node,profile=servers.for_subscription(sub)
                    ident=service.device_identity(device) if device else service.primary_identity(sub)
                    key=wgdashboard.WGDashboard.identity(wgdashboard.stored_private_key(node['id'],ident,create=False))
                except Exception:continue
                nodes.setdefault((node['id'],profile.get('configuration')),(profile,{}))[1][key]=(sub,device,ident)
        async def observe(client,profile,owners):
            panel=wgdashboard.WGDashboard(profile)
            try:groups=await panel.peer_rows(client)
            except (httpx.HTTPError,ValueError,wgdashboard.PanelError):return
            for field,peers in groups:
                if field!='configurationPeers':continue
                for peer in peers:
                    entry=owners.get(peer['id'])
                    if not entry:continue
                    sub,device,ident=entry
                    row=rows[sub['id']]
                    data=metrics(peer,now);samples.append((sub['id'],peer['id'],data))
                    if data['last_seen'] is not None or peer.get('latest_handshake')=='No Handshake':
                        row['observed']+=1
                    row['has_recent']|=data['online'] is True
                    addresses = peer_addresses(peer)
                    if device is None and sub['id'] not in latest_endpoint:
                        row.update(addresses)
                    seen = data['last_seen']
                    if seen and addresses['endpoint_ip'] and seen > latest_endpoint.get(sub['id'],0):
                        latest_endpoint[sub['id']] = seen
                        row.update(addresses)
                        row['endpoint_platform'] = wg_client_platform.match_peer(
                            platform_observations,peer['id'],addresses['endpoint_ip'],seen)
                    if maintenance:
                        name=wg_activity.display(sub['user_id'],ident,device['name'] if device else None)
                        old=peer.get('name','')
                        if old!=name and (not old or old.startswith(('pushok_','Pushok · '))):
                            renames.append((panel,peer['id'],old,name))
        async with httpx.AsyncClient(timeout=5,follow_redirects=False,trust_env=False) as client:
            if maintenance:
                try:await wg_activity.refresh_names(client)
                except (httpx.HTTPError,ValueError,KeyError):pass
            await asyncio.gather(*(observe(client,p,owners) for p,owners in nodes.values()))
            totals=wg_activity.record(samples,now)
            for sid,row in rows.items():
                total=totals.get(sid,{})
                seen=total.get('seen') or None
                expected=row.pop('expected')
                complete=row.pop('observed')==expected and expected>0
                row.update(received=total.get('received'),sent=total.get('sent'),last_seen=seen,
                    periods=total.get('periods'),activity_seen=seen,activity_known=complete,
                    ever_used=bool(seen or total.get('received') or total.get('sent')))
                recent=row.pop('has_recent')
                row['online']=True if recent else False if complete else None
        result={'sampled_at':now,'activity_schema':1,'accounts':accounts,
                'unlinked_telegram_ids':[int(tg) for tg in unlinked], 'unlinked_activity':unlinked}
        _cached=result
        _until=time.monotonic()+20
    if renames:
        async with httpx.AsyncClient(timeout=5,follow_redirects=False,trust_env=False) as client:
            for panel,key,old,name in renames:
                try:
                    async with service.lock:
                        await panel.rename_managed(client,key,old,name)
                except (wgdashboard.PanelError,httpx.HTTPError,ValueError,TypeError):
                    continue
    return result


async def worker():
    import logging
    while True:
        try:await snapshot(maintenance=True)
        except Exception as exc:logging.getLogger(__name__).warning('WG observations: %s',type(exc).__name__)
        await asyncio.sleep(300)
