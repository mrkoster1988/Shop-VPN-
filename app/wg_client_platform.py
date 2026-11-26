"""Correlate a profile import with the same peer's subsequent endpoint observation."""
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import sqlite3
from functools import lru_cache
import time

from . import wgdashboard
from .db import db

WINDOW = 7 * 86400


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS wg_client_platform(
        peer TEXT NOT NULL, address TEXT NOT NULL, platform TEXT NOT NULL, imported INTEGER NOT NULL,
        PRIMARY KEY(peer,address,platform))""")
    conn.execute('CREATE INDEX IF NOT EXISTS wg_client_platform_time ON wg_client_platform(imported)')


def system(agent):
    ua = agent[:512].lower()
    if 'android' in ua or ua == 'pushok-flow-subscriptions':
        return 'android'
    if any(word in ua for word in ('iphone','ipad','ipod')) or ('macintosh' in ua and 'mobile/' in ua):
        return 'ios'
    if 'windows' in ua: return 'windows'
    if 'mac os' in ua or 'macintosh' in ua: return 'macos'
    if 'linux' in ua: return 'linux'
    return None


def address_key(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    key = os.getenv('ENCRYPTION_KEY','')
    if not ip.is_global or not key: return None
    return hmac.digest(key.encode(), b'wg-client-endpoint\0'+ip.packed,'sha256').hex()


@lru_cache(maxsize=2)
def proxy_addresses(host,minute):
    try:
        return {r[4][0] for r in socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)}
    except OSError:
        return set()


def request_address(request):
    source=request.client.host
    proxy=os.getenv('SHOP_PLATFORM_EDGE_PROXY','')
    if proxy and request.headers.get('x-forwarded-for') and source in proxy_addresses(proxy,int(time.monotonic())//60):
        # The installed Caddy sets X-Forwarded-For from its incoming connection.
        return request.headers['x-forwarded-for'].split(',')[-1].strip()
    return source


def observe_import(request, config):
    if not request.client or not isinstance(config,str): return
    platform = system(request.headers.get('user-agent',''))
    address = address_key(request_address(request))
    match = re.search(r'(?mi)^\s*PrivateKey\s*=\s*([A-Za-z0-9+/]{43}=)\s*$',config)
    if not platform or not address or not match: return
    try:
        peer = wgdashboard.WGDashboard.identity(match[1])
        now = int(time.time())
        with db() as conn:
            conn.execute('DELETE FROM wg_client_platform WHERE imported<?',(now-WINDOW,))
            conn.execute('INSERT OR REPLACE INTO wg_client_platform VALUES(?,?,?,?)',
                         (hashlib.sha256(peer.encode()).hexdigest(),address,platform,now))
    except (ValueError,wgdashboard.PanelError,sqlite3.Error):
        # An optional observation must not interrupt config delivery.
        return


def feed(request,document):
    for profile in document.get('profiles',[]):
        observe_import(request,profile.get('config'))
    return document


def current(conn, now):
    rows = conn.execute('SELECT * FROM wg_client_platform WHERE imported>=?',(now-WINDOW,))
    result = {}
    for row in rows:
        result.setdefault((row['peer'],row['address']),[]).append((row['platform'],row['imported']))
    return result


def match_peer(index, peer, endpoint, seen):
    if not endpoint or not seen: return None
    observations = index.get((hashlib.sha256(peer.encode()).hexdigest(),address_key(endpoint)),[])
    if len(observations) == 1 and observations[0][1] <= seen+2:
        return observations[0][0]
    return None
