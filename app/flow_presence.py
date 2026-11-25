"""Read-only connection observations for the MTProxy cabinet."""
import asyncio
import hashlib
import hmac
import math
import os
import re
import time

import httpx
from fastapi import HTTPException

from . import servers, service, wgdashboard
from .db import all_rows

DOMAIN = 'pushok-vpn-presence-v1'
_lock = asyncio.Lock()
_cached = None
_until = 0.0


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


async def snapshot():
    global _cached, _until
    async with _lock:
        if _cached is not None and time.monotonic() < _until:
            return _cached
        subscriptions = all_rows("""SELECT s.id,s.server_id,s.user_id,s.protocol,s.credential_id,
            s.expires,s.state,s.primary_key_state,b.mtproxy_uuid FROM subscriptions s
            JOIN flow_bridge_identities b ON b.shop_user_id=s.user_id WHERE s.protocol='wg'""")
        now = int(time.time())
        accounts, nodes = {}, {}
        for sub in subscriptions:
            subject = sub['mtproxy_uuid']
            state = sub['state']
            if state == 'active' and int(sub['expires'] or 0) <= now:
                state = 'expired'
            accounts[subject] = {'state': state, 'expires_at': sub['expires'] or None,
                'online': None, 'last_seen': None, 'received': None, 'sent': None}
            if state != 'active' or sub['primary_key_state'] != 'active':
                continue
            try:
                node, profile = servers.for_subscription(sub)
                key = wgdashboard.stored_private_key(node['id'], service.primary_identity(sub), create=False)
                public_key = wgdashboard.WGDashboard.identity(key)
            except Exception:
                continue
            nodes.setdefault(node['id'], (profile, {}))[1][public_key] = subject
        async def observe(client, profile, owners):
            try:
                peers = await wgdashboard.WGDashboard(profile).peer_rows(client)
                for field, rows in peers:
                    if field == 'configurationPeers':
                        for peer in rows:
                            if peer['id'] in owners:
                                accounts[owners[peer['id']]].update(metrics(peer, now))
            except (httpx.HTTPError, ValueError, wgdashboard.PanelError):
                return
        async with httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False) as client:
            await asyncio.gather(*(observe(client, profile, owners) for profile, owners in nodes.values()))
        _cached = {'sampled_at': now, 'accounts': accounts}
        _until = time.monotonic() + 20
        return _cached
