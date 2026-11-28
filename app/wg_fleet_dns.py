"""Reconcile only owned wgN DNS records; no edits to unrelated zone records.

Cloudflare credentials are server-side environment variables scoped to one
zone. A durable marker recovers an ambiguous create without duplicating it.
Archive is necessary but not sufficient for deletion: all peer revocations
must also be confirmed locally before an owned record can be removed.
"""
import asyncio
import ipaddress
import os
import re
import secrets
import time

import httpx

from .db import db


API_BASE = 'https://api.cloudflare.com/client/v4'
RECHECK_SECONDS = 300
LEASE_SECONDS = 120
MAX_REPLY_BYTES = 1_048_576


class DNSFailure(ValueError):
    pass


def validate_endpoint_ip(value):
    if not isinstance(value, str) or not value or value != value.strip() or '%' in value:
        raise DNSFailure('InvalidWireGuardEndpointIP')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise DNSFailure('InvalidWireGuardEndpointIP') from None
    if (not address.is_global or address.is_multicast or address.is_unspecified
            or address.is_loopback or address.is_link_local or address.is_reserved
            or getattr(address, 'is_site_local', False)
            or getattr(address, 'ipv4_mapped', None) is not None):
        raise DNSFailure('PublicWireGuardEndpointRequired')
    return str(address)


def domain_suffix():
    value = os.environ.get('WG_FLEET_DOMAIN', 'pushok.shop').strip().lower().rstrip('.')
    if (len(value) > 220 or '.' not in value
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                   for part in value.split('.'))):
        raise DNSFailure('InvalidFleetDomain')
    return value


def _settings():
    token = os.environ.get('WG_FLEET_CLOUDFLARE_API_TOKEN', '').strip()
    zone = os.environ.get('WG_FLEET_CLOUDFLARE_ZONE_ID', '').strip()
    if not token and not zone:
        return None
    if (not re.fullmatch(r'[A-Za-z0-9_-]{20,512}', token)
            or not re.fullmatch(r'[a-fA-F0-9]{32}', zone)):
        raise DNSFailure('InvalidCloudflareConfiguration')
    return {'token': token, 'zone_id': zone.lower()}


def configuration_status():
    try:
        configured = _settings() is not None
        suffix = domain_suffix()
        return {'configured': configured, 'domain': suffix, 'error': None}
    except DNSFailure as error:
        return {'configured': False, 'domain': None, 'error': str(error)}


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS wg_fleet_dns_records (
        server_id TEXT PRIMARY KEY REFERENCES wg_fleet_nodes(server_id),
        domain TEXT NOT NULL UNIQUE, zone_id TEXT NOT NULL,
        owner_marker TEXT NOT NULL UNIQUE, record_id TEXT UNIQUE,
        record_type TEXT, content TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
        lease_id TEXT, lease_until INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, last_verified INTEGER NOT NULL DEFAULT 0)''')


def _desired(row):
    expected = f"wg{row['serial']}.{domain_suffix()}"
    if type(row['serial']) is not int or row['serial'] <= 0 or row['domain'] != expected:
        raise DNSFailure('UnmanagedFleetDomain')
    content = validate_endpoint_ip(row['endpoint_ip'])
    return {'name': expected, 'type': 'AAAA' if ':' in content else 'A',
            'content': content, 'proxied': False, 'ttl': 60}


def _claim(settings, now):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('''SELECT n.*,r.next_attempt,r.lease_until,r.last_verified
            FROM wg_fleet_nodes n LEFT JOIN wg_fleet_dns_records r ON r.server_id=n.server_id
            WHERE n.domain IS NOT NULL AND n.endpoint_ip IS NOT NULL
            AND COALESCE(r.next_attempt,0)<=? AND COALESCE(r.lease_until,0)<=?
            AND (n.dns_state NOT IN ('ready','deleted')
                 OR (n.archived=1 AND n.dns_state!='deleted')
                 OR (n.archived=0 AND COALESCE(r.last_verified,0)<=?))
            ORDER BY COALESCE(r.last_verified,0),n.serial LIMIT 1''',
            (now, now, now - RECHECK_SECONDS)).fetchall()
        if not rows:
            return None
        row = dict(rows[0])
        existing = conn.execute('SELECT * FROM wg_fleet_dns_records WHERE server_id=?', (row['server_id'],)).fetchone()
        if existing is None:
            conn.execute('''INSERT INTO wg_fleet_dns_records
                (server_id,domain,zone_id,owner_marker) VALUES (?,?,?,?)''',
                (row['server_id'], row['domain'], settings['zone_id'], 'pushok-fleet:' + secrets.token_hex(16)))
        lease = secrets.token_hex(16)
        conn.execute('UPDATE wg_fleet_dns_records SET lease_id=?,lease_until=? WHERE server_id=?',
                     (lease, now + LEASE_SECONDS, row['server_id']))
        owned = dict(conn.execute('SELECT * FROM wg_fleet_dns_records WHERE server_id=?', (row['server_id'],)).fetchone())
        return row, owned


def _still_current(row, owned, *, deleting=False):
    with db() as conn:
        current = conn.execute('''SELECT n.*,r.lease_id FROM wg_fleet_nodes n
            JOIN wg_fleet_dns_records r ON r.server_id=n.server_id WHERE n.server_id=?''',
            (row['server_id'],)).fetchone()
        if (not current or current['lease_id'] != owned['lease_id']
                or any(current[key] != row[key] for key in ('domain', 'endpoint_ip', 'archived'))):
            raise DNSFailure('DNSDesiredStateChanged')
        if deleting:
            if not current['archived']:
                raise DNSFailure('DNSDeletionNotConfirmed')
            if conn.execute("SELECT 1 FROM wg_fleet_placements WHERE server_id=? AND state!='revoked' LIMIT 1",
                            (row['server_id'],)).fetchone():
                raise DNSFailure('DNSDeletionNotConfirmed')
            if conn.execute("SELECT 1 FROM subscriptions WHERE server_id=? AND state IN ('active','pending') LIMIT 1",
                            (row['server_id'],)).fetchone():
                raise DNSFailure('DNSDeletionNotConfirmed')


def _remember_record(owned, record):
    with db() as conn:
        changed = conn.execute('''UPDATE wg_fleet_dns_records SET record_id=?,record_type=?,content=?
            WHERE server_id=? AND lease_id=?''',
            (record['id'], record['type'], record['content'], owned['server_id'], owned['lease_id'])).rowcount
        if changed != 1:
            raise DNSFailure('DNSDesiredStateChanged')
    owned['record_id'] = record['id']


def _finish(row, owned, *, state, error=None, now=None):
    now = int(time.time()) if now is None else now
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT lease_id,attempts FROM wg_fleet_dns_records WHERE server_id=?',
                               (row['server_id'],)).fetchone()
        if not current or current['lease_id'] != owned['lease_id']:
            return
        attempts = current['attempts'] + 1 if error else 0
        retry_at = now + min(900, 15 * 2 ** min(attempts, 6)) if error else 0
        conn.execute('''UPDATE wg_fleet_dns_records SET lease_id=NULL,lease_until=0,
            attempts=?,next_attempt=?,last_error=?,last_verified=CASE WHEN ? IS NULL THEN ? ELSE last_verified END
            WHERE server_id=? AND lease_id=?''',
            (attempts, retry_at, error, error, now, row['server_id'], owned['lease_id']))
        conn.execute('''UPDATE wg_fleet_nodes SET dns_state=? WHERE server_id=?
            AND domain=? AND endpoint_ip=? AND archived=?''',
            (state, row['server_id'], row['domain'], row['endpoint_ip'], row['archived']))


async def _request(client, settings, method, suffix='', **kwargs):
    try:
        response = await client.request(method, f"{API_BASE}/zones/{settings['zone_id']}/dns_records{suffix}",
            headers={'Authorization': 'Bearer ' + settings['token'], 'Content-Type': 'application/json'}, **kwargs)
        if response.status_code < 200 or response.status_code >= 300 or len(response.content) > MAX_REPLY_BYTES:
            raise DNSFailure('CloudflareRequestFailed')
        body = response.json()
        if not isinstance(body, dict) or body.get('success') is not True or 'result' not in body:
            raise DNSFailure('CloudflareInvalidResponse')
        return body
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, DNSFailure):
            raise
        raise DNSFailure('CloudflareRequestFailed') from None


async def _records(client, settings, name):
    body = await _request(client, settings, 'GET', params={'name': name, 'page': 1, 'per_page': 100})
    records = body['result']
    if (not isinstance(records, list) or len(records) > 100
            or any(not isinstance(row, dict) or row.get('name') != name
                   or not isinstance(row.get('id'), str) or not re.fullmatch(r'[a-fA-F0-9]{32}', row['id'])
                   for row in records)):
        raise DNSFailure('CloudflareInvalidResponse')
    info = body.get('result_info', {})
    if (not isinstance(info, dict) or type(info.get('total_count', len(records))) is not int
            or info.get('total_count', len(records)) != len(records)
            or info.get('total_pages', 1) not in (0, 1)):
        raise DNSFailure('DNSRecordConflict')
    return records


def _owned(record, row, owned):
    return (record.get('name') == row['domain'] and record.get('type') in ('A', 'AAAA')
            and record.get('comment') == owned['owner_marker']
            and (owned['record_id'] is None or record.get('id') == owned['record_id'])
            and isinstance(record.get('id'), str) and bool(re.fullmatch(r'[a-fA-F0-9]{32}', record['id'])))


def _matches(record, desired):
    try:
        content = str(ipaddress.ip_address(record.get('content', '')))
    except ValueError:
        return False
    return (content == desired['content'] and record.get('name') == desired['name']
            and record.get('type') == desired['type'] and record.get('proxied') is False
            and type(record.get('ttl')) is int and record['ttl'] == 60)


async def _sync_one(client, settings, row, owned):
    desired = _desired(row)
    if owned['zone_id'] != settings['zone_id'] or owned['domain'] != row['domain']:
        raise DNSFailure('DNSOwnershipConfigurationChanged')
    _still_current(row, owned, deleting=bool(row['archived']))
    records = await _records(client, settings, row['domain'])
    if len(records) > 1 or (records and not _owned(records[0], row, owned)):
        raise DNSFailure('DNSRecordConflict')
    record = records[0] if records else None
    if row['archived']:
        if record:
            _still_current(row, owned, deleting=True)
            await _request(client, settings, 'DELETE', '/' + record['id'])
            if await _records(client, settings, row['domain']):
                raise DNSFailure('DNSDeleteNotConfirmed')
        _finish(row, owned, state='deleted')
        return
    desired['comment'] = owned['owner_marker']
    if record:
        _remember_record(owned, record)
        if not _matches(record, desired):
            _still_current(row, owned)
            body = await _request(client, settings, 'PUT', '/' + record['id'], json=desired)
            if not isinstance(body['result'], dict) or not _owned(body['result'], row, owned):
                raise DNSFailure('DNSWriteNotConfirmed')
    else:
        # A deleted owned record may be recreated only under the same reserved
        # node name. Ownership recovery uses the persisted marker, not its IP.
        _still_current(row, owned)
        if owned['record_id'] is not None:
            # Its absence has just been confirmed. Persist a new-create intent
            # before POST so a lost response can be recovered by owner marker.
            with db() as conn:
                changed = conn.execute('UPDATE wg_fleet_dns_records SET record_id=NULL WHERE server_id=? AND lease_id=?',
                                       (row['server_id'], owned['lease_id'])).rowcount
                if changed != 1:
                    raise DNSFailure('DNSDesiredStateChanged')
            owned['record_id'] = None
        body = await _request(client, settings, 'POST', json=desired)
        created = body['result']
        recovery = {**owned, 'record_id': None}
        if not isinstance(created, dict) or not _owned(created, row, recovery):
            raise DNSFailure('DNSWriteNotConfirmed')
        _remember_record(owned, created)
    confirmed = await _records(client, settings, row['domain'])
    if (len(confirmed) != 1 or not _owned(confirmed[0], row, owned)
            or not _matches(confirmed[0], desired)):
        raise DNSFailure('DNSWriteNotConfirmed')
    _remember_record(owned, confirmed[0])
    _still_current(row, owned)
    _finish(row, owned, state='ready')


def _client():
    return httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False)


async def sync(max_jobs=4):
    """A bounded worker; call outside the shared provisioning service lock."""
    if type(max_jobs) is not int or not 0 <= max_jobs <= 16:
        raise ValueError('InvalidDNSBatchSize')
    try:
        settings = _settings()
    except DNSFailure:
        return 0
    if settings is None:
        return 0
    completed = 0
    async with _client() as client:
        for _ in range(max_jobs):
            claimed = _claim(settings, int(time.time()))
            if claimed is None:
                break
            row, owned = claimed
            try:
                async with asyncio.timeout(35):
                    await _sync_one(client, settings, row, owned)
                completed += 1
            except DNSFailure as error:
                code = str(error)
                state = 'conflict' if code in ('DNSRecordConflict', 'DNSOwnershipConfigurationChanged', 'UnmanagedFleetDomain') else 'error'
                _finish(row, owned, state=state, error=code)
            except Exception:
                _finish(row, owned, state='error', error='CloudflareRequestFailed')
    return completed
