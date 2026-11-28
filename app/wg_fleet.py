"""Durable WG placements underneath the existing account entitlement.

Subscriptions remain the billing authority. Placement IDs are technical peer
identities, never a second subscription or a reason to grant more time. Remote
work is retried with the same identity; only confirmed configs enter the feed.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid

from fastapi import HTTPException

from . import config as c, servers, wg_fleet_health as health
from .db import db, one, all_rows

log = logging.getLogger(__name__)


def create_schema(conn):
    statements = [
        '''CREATE TABLE IF NOT EXISTS wg_fleet_policy (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
            standby_count INTEGER NOT NULL DEFAULT 1 CHECK(standby_count=1),
            version INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL DEFAULT 0)''',
        'INSERT OR IGNORE INTO wg_fleet_policy(id) VALUES(1)',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_nodes (
            serial INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id TEXT NOT NULL UNIQUE REFERENCES servers(id),
            domain TEXT UNIQUE, endpoint_ip TEXT, dns_state TEXT NOT NULL DEFAULT 'unconfigured',
            archived INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_placements (
            id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            server_id TEXT NOT NULL REFERENCES servers(id), device_id TEXT REFERENCES devices(id) ON DELETE CASCADE,
            credential_id TEXT NOT NULL, identity_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('primary','standby','device','standby_device','retiring')),
            state TEXT NOT NULL CHECK(state IN ('pending','active','revoking','revoked')),
            secret TEXT, target_expires INTEGER NOT NULL, server_version INTEGER NOT NULL,
            retire_after INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT,
            created INTEGER NOT NULL, updated INTEGER NOT NULL,
            UNIQUE(server_id,identity_hash))''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS wg_fleet_current_role
            ON wg_fleet_placements(subscription_id,role)
            WHERE role IN ('primary','standby') AND state!='revoked' ''',
        '''CREATE INDEX IF NOT EXISTS wg_fleet_placement_queue
            ON wg_fleet_placements(state,next_attempt)''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_operations (
            id TEXT PRIMARY KEY, source_server_id TEXT NOT NULL REFERENCES servers(id),
            state TEXT NOT NULL DEFAULT 'preparing', lost INTEGER NOT NULL DEFAULT 0,
            deadline INTEGER NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
            error TEXT)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS wg_fleet_one_drain
            ON wg_fleet_operations(source_server_id) WHERE state!='completed' ''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_moves (
            operation_id TEXT NOT NULL REFERENCES wg_fleet_operations(id),
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'pending', error TEXT,
            PRIMARY KEY(operation_id,subscription_id))''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_requests (
            actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            response TEXT NOT NULL, created INTEGER NOT NULL,
            PRIMARY KEY(actor,request_id))''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_audit (
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT, created INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_probe_tokens (
            server_id TEXT PRIMARY KEY REFERENCES servers(id), probe_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, created INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_revocations (
            subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
            requested INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wg_fleet_failures (
            server_id TEXT PRIMARY KEY REFERENCES servers(id),
            consecutive INTEGER NOT NULL, reported_at INTEGER NOT NULL)''',
    ]
    for statement in statements:
        conn.execute(statement)
    health.create_schema(conn)
    from . import wg_fleet_dns
    wg_fleet_dns.create_schema(conn)


def policy(conn):
    row = dict(conn.execute('SELECT enabled,standby_count,version FROM wg_fleet_policy WHERE id=1').fetchone())
    row['enabled'] = bool(row['enabled'])
    return row


def enabled(conn):
    return policy(conn)['enabled']


def revocation_requested(conn, subscription_id):
    return bool(conn.execute('SELECT 1 FROM wg_fleet_revocations WHERE subscription_id=?', (subscription_id,)).fetchone())


def new_access(conn, subscription_id):
    """Called only after an authorized renewal/reissue is confirmed remotely."""
    conn.execute('DELETE FROM wg_fleet_revocations WHERE subscription_id=?', (subscription_id,))


def record_failure(conn, report):
    # A monitoring outage or missing metrics is not evidence that customer
    # traffic has failed. Two fresh datapath failures are required for failover.
    datapath = {'dns_failed','https_failed','handshake_missing',
                'handshake_stale','traffic_missing'}
    code = report.get('failure_code')
    failed = code in datapath or (code is None and (
        not report['dns_ok'] or not report['https_ok'] or not report['handshake_at']
        or not report['rx_delta_bytes'] or not report['tx_delta_bytes']))
    old = conn.execute('SELECT * FROM wg_fleet_failures WHERE server_id=?', (report['server_id'],)).fetchone()
    count = (old['consecutive'] if old and 0 <= report['finished_at']-old['reported_at'] <= health.HEALTH_TTL_SECONDS else 0)+1 if failed else 0
    conn.execute('''INSERT INTO wg_fleet_failures VALUES (?,?,?) ON CONFLICT(server_id)
        DO UPDATE SET consecutive=excluded.consecutive,reported_at=excluded.reported_at''',
        (report['server_id'],count,report['finished_at']))


def confirmed_failure(conn, sid, now=None):
    now = int(time.time()) if now is None else now
    row = conn.execute('SELECT * FROM wg_fleet_failures WHERE server_id=?', (sid,)).fetchone()
    return bool(row and row['consecutive']>=2 and 0<=now-row['reported_at']<=health.HEALTH_TTL_SECONDS)


def render_config(server_id, config):
    """Publish the allocated tunnel DNS name; panel addresses stay separate."""
    with db() as conn:
        node = conn.execute('SELECT domain,endpoint_ip,dns_state FROM wg_fleet_nodes WHERE server_id=?', (server_id,)).fetchone()
    if not node or not node['domain']:
        return config
    if node['dns_state'] != 'ready' or not node['endpoint_ip']:
        raise RuntimeError('FleetDnsNotReady')
    matches = list(re.finditer(r'(?mi)^\s*Endpoint\s*=\s*(\[[^\]]+\]|[^\s:]+):(\d+)\s*$', config))
    if len(matches)!=1 or not 1<=int(matches[0][2])<=65535:
        raise RuntimeError('FleetEndpointInvalid')
    match = matches[0]
    host = match[1].strip('[]')
    if host not in (node['domain'],node['endpoint_ip']):
        raise RuntimeError('FleetEndpointMismatch')
    return config[:match.start()] + f'Endpoint = {node["domain"]}:{match[2]}\n' + config[match.end():]


def _identity_hash(identity):
    return hashlib.sha256(identity.encode()).hexdigest()


def counts(conn, sid, now=None):
    now = int(time.time()) if now is None else now
    # Count subscription reservations once per node, including pending work.
    # Mirrored primary/device placements must not double the canonical slot.
    primary = {r[0] for r in conn.execute("""SELECT id FROM subscriptions WHERE server_id=?
        AND (state='pending' OR (state='active' AND expires>?))""", (sid, now))}
    placements = {r[0] for r in conn.execute("""SELECT DISTINCT subscription_id
        FROM wg_fleet_placements WHERE server_id=? AND state!='revoked'""", (sid,))}
    standby = conn.execute("""SELECT count(*) FROM wg_fleet_placements WHERE server_id=?
        AND role='standby' AND state IN ('pending','active')""", (sid,)).fetchone()[0]
    return {'occupied': len(primary), 'reserved': len(placements - primary),
            'primary_count': len(primary), 'standby_count': standby}


def candidates(conn, *, exclude=(), now=None, guest=False):
    """Ordered strict minimum selection, random among exact ties only."""
    now = int(time.time()) if now is None else now
    rows = []
    for raw in conn.execute("""SELECT s.*,COALESCE(r.accept_new,1) AS accept_new,
            COALESCE(n.archived,0) AS archived,n.domain,n.dns_state
        FROM servers s LEFT JOIN wg_fleet_nodes n ON n.server_id=s.id
        LEFT JOIN server_routing_nodes r ON r.server_id=s.id
        WHERE s.protocol='wg' AND s.state='active'"""):
        row = dict(raw)
        if row['id'] in exclude or row['archived'] or not row['accept_new']:
            continue
        if row['domain'] and row['dns_state'] != 'ready':
            continue
        try:
            profile = servers.decode(row)
        except Exception:
            continue
        if (profile.get('api') != 'wgdashboard' or not profile.get('schedule_expiry')
                or not profile.get('panel_timezone')):
            continue
        dedicated=profile.get('configuration') == servers.GUEST_CONFIGURATION
        if guest is not None and dedicated != guest:
            continue
        row.update(counts(conn, row['id'], now))
        rows.append(row)
    return health.ordered(conn,rows,now=now)


def _busy(conn, sub):
    sid = sub['id']
    return (revocation_requested(conn,sid) or sub.get('primary_key_state', 'active') != 'active'
        or conn.execute("SELECT 1 FROM primary_rotations WHERE subscription_id=? AND state NOT IN ('completed','canceled')", (sid,)).fetchone()
        or conn.execute("SELECT 1 FROM device_rotations WHERE device_id IN (SELECT id FROM devices WHERE subscription_id=?) AND state NOT IN ('completed','canceled')", (sid,)).fetchone()
        or conn.execute("SELECT 1 FROM flow_owner_extensions WHERE subscription_id=? AND status='pending'", (sid,)).fetchone()
        or conn.execute("SELECT 1 FROM vpn_access_changes WHERE subscription_id=? AND applied IS NULL", (sid,)).fetchone())


def _remember(conn, sub, node, identity, role, secret, expires, *, device_id=None, state='active'):
    now = int(time.time())
    fingerprint = _identity_hash(identity)
    existing = conn.execute('SELECT * FROM wg_fleet_placements WHERE server_id=? AND identity_hash=?',
                            (node['id'], fingerprint)).fetchone()
    pid = existing['id'] if existing else str(uuid.uuid4())
    if role == 'primary':
        conn.execute("""UPDATE wg_fleet_placements SET role='retiring',retire_after=?,updated=?
            WHERE subscription_id=? AND role='primary' AND id!=? AND state!='revoked'""", (now, now, sub['id'], pid))
    conn.execute('''INSERT INTO wg_fleet_placements
        (id,subscription_id,server_id,device_id,credential_id,identity_hash,role,state,
         secret,target_expires,server_version,created,updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(server_id,identity_hash) DO UPDATE SET
         role=excluded.role,state=excluded.state,secret=excluded.secret,
         target_expires=excluded.target_expires,server_version=excluded.server_version,
         updated=excluded.updated,error=NULL''',
        (pid, sub['id'], node['id'], device_id, c.encrypt(identity), fingerprint, role, state,
         secret, expires, node['version'], now, now))
    return pid


def retire_standby(conn, subscription_id, server_id, deadline):
    # A reserve belongs to the account AND to its technical device profiles.
    # Retiring only the parent would strand device peers on the removed node.
    conn.execute("""UPDATE wg_fleet_placements SET role='retiring',retire_after=?,next_attempt=0,
        state=CASE WHEN ?<=? THEN 'revoking' ELSE state END
        WHERE subscription_id=? AND server_id=? AND role IN ('standby','standby_device')
        AND state!='revoked'""", (deadline,deadline,int(time.time()),subscription_id,server_id))


def sync_primary(conn, sub):
    """Mirror already-confirmed existing peers; this never grants access."""
    from . import service
    if sub['state'] != 'active' or not sub.get('secret') or sub.get('primary_key_state') != 'active':
        return
    node = conn.execute('SELECT * FROM servers WHERE id=?', (sub['server_id'],)).fetchone()
    if not node or servers.decode(dict(node)).get('api') != 'wgdashboard':
        return
    node = dict(node)
    identity=service.primary_identity(sub)
    old=conn.execute("SELECT identity_hash FROM wg_fleet_placements WHERE subscription_id=? AND role='primary' AND state!='revoked'",(sub['id'],)).fetchone()
    if old and old['identity_hash']!=_identity_hash(identity):
        # A user key rotation also invalidates the previously exported backup
        # credential. Otherwise an old downloaded feed could retain access.
        for backup in conn.execute("SELECT server_id FROM wg_fleet_placements WHERE subscription_id=? AND role='standby' AND state!='revoked'",(sub['id'],)).fetchall():
            retire_standby(conn,sub['id'],backup['server_id'],0)
    _remember(conn, sub, node, identity, 'primary', sub['secret'], sub['expires'])
    for raw in conn.execute("SELECT * FROM devices WHERE subscription_id=? AND state='active' AND secret IS NOT NULL", (sub['id'],)):
        device = dict(raw)
        identity=service.device_identity(device)
        old=conn.execute("SELECT identity_hash FROM wg_fleet_placements WHERE device_id=? AND role='device' AND state!='revoked'",(device['id'],)).fetchone()
        if old and old['identity_hash']!=_identity_hash(identity):
            conn.execute("""UPDATE wg_fleet_placements SET role='retiring',state='revoking',retire_after=0,next_attempt=0
                WHERE device_id=? AND role IN ('device','standby_device') AND state!='revoked'""",(device['id'],))
        _remember(conn, sub, node, identity, 'device', device['secret'], device['expires'], device_id=device['id'])


def reserve_standby(conn, sub, *, now=None):
    """Caller holds BEGIN IMMEDIATE: selection and reservation are indivisible."""
    if not conn.in_transaction:
        raise RuntimeError('FleetReservationRequiresTransaction')
    now = int(time.time()) if now is None else now
    existing = conn.execute("""SELECT * FROM wg_fleet_placements WHERE subscription_id=?
        AND role='standby' AND state!='revoked'""", (sub['id'],)).fetchone()
    if existing:
        return dict(existing) if existing['state'] in ('pending','active') else None
    exclude = [r[0] for r in conn.execute("SELECT DISTINCT server_id FROM wg_fleet_placements WHERE subscription_id=? AND state!='revoked'", (sub['id'],))]
    exclude.append(sub['server_id'])
    options = candidates(conn, exclude=exclude, now=now, guest=bool(sub.get('wg_configuration')))
    if not options:
        return None
    node = options[0]
    pid = _remember(conn, sub, node, str(uuid.uuid4()), 'standby', None, sub['expires'], state='pending')
    return dict(conn.execute('SELECT * FROM wg_fleet_placements WHERE id=?', (pid,)).fetchone())


def reserve_device_standbys(conn, sub, standby):
    """Technical device keys share the entitlement and the selected standby."""
    from . import service
    if not standby:
        return
    node = dict(conn.execute('SELECT * FROM servers WHERE id=?', (standby['server_id'],)).fetchone())
    for raw in conn.execute("SELECT * FROM devices WHERE subscription_id=? AND state='active' AND expires>?", (sub['id'],int(time.time()))).fetchall():
        device = dict(raw)
        identity = service.device_identity(device)
        saved = conn.execute('SELECT state FROM wg_fleet_placements WHERE server_id=? AND identity_hash=?', (node['id'],_identity_hash(identity))).fetchone()
        if not saved or saved['state']=='revoked':
            _remember(conn,sub,node,identity,'standby_device',None,min(device['expires'],sub['expires']),device_id=device['id'],state='pending')


def feed_profiles(conn, subscription_id, profiles, expires, *, device_id=None):
    """A feed read has no remote side effects and never allocates another peer."""
    role = 'standby_device' if device_id else 'standby'
    rows = conn.execute('''SELECT p.*,s.version,s.state AS node_state,n.archived
        FROM wg_fleet_placements p JOIN servers s ON s.id=p.server_id
        LEFT JOIN wg_fleet_nodes n ON n.server_id=s.id
        WHERE p.subscription_id=? AND p.role=? AND p.state='active'
        AND p.device_id IS ? AND p.target_expires=? AND p.secret IS NOT NULL
        ORDER BY p.created,p.id''', (subscription_id, role, device_id, expires)).fetchall()
    for row in rows:
        if row['node_state'] != 'active' or row['archived'] or row['server_version'] != row['version']:
            continue
        # Previously prepared configs remain useful when monitoring or a common
        # probe target fails. A draining/retired node is withdrawn above; the
        # client separately verifies its own path before calling it connected.
        try:
            config = c.decrypt(row['secret'])
        except Exception:
            continue
        if not config.startswith('[Interface]') or len(config) > 65536:
            continue
        return profiles + [{'id': row['id'], 'name': 'Pushok · резерв',
                            'protocol': 'wireguard', 'config': config}]
    return profiles


def carry_version(conn, sid, before, after):
    """Metadata-only changes keep the same measured tunnel incarnation."""
    conn.execute('UPDATE wg_fleet_probes SET server_version=? WHERE server_id=? AND server_version=?', (after, sid, before))
    conn.execute('UPDATE wg_fleet_health SET server_version=? WHERE server_id=? AND server_version=?', (after, sid, before))
    conn.execute('UPDATE wg_fleet_placements SET server_version=? WHERE server_id=? AND server_version=?', (after, sid, before))


def drain(conn, sid, version, *, deadline_hours=24, lost=False, operation_id=None):
    now = int(time.time())
    previous = conn.execute("SELECT id FROM wg_fleet_operations WHERE source_server_id=? AND state!='completed'", (sid,)).fetchone()
    if previous:
        return operation(conn, previous['id'])
    node = conn.execute("SELECT * FROM servers WHERE id=? AND protocol='wg'", (sid,)).fetchone()
    if not node:
        raise HTTPException(404, 'Сервер не найден')
    if node['version'] != version:
        raise HTTPException(409, 'Сервер уже изменён. Обновите список.')
    if type(deadline_hours) is not int or not 1 <= deadline_hours <= 720:
        raise HTTPException(422, 'Переходный период: от 1 до 720 часов.')
    oid = operation_id or str(uuid.uuid4())
    # Adopt every known issued identity before migration. Expired local rows
    # are not proof that their remote peers were actually removed.
    from . import service
    for raw in conn.execute("SELECT * FROM subscriptions WHERE server_id=? AND protocol='wg'",(sid,)).fetchall():
        sub=dict(raw)
        if sub['state']=='active':
            sync_primary(conn,sub)
        elif sub.get('secret') and sub['state']!='revoked':
            _remember(conn,sub,dict(node),service.primary_identity(sub),'retiring',sub['secret'],sub['expires'],state='revoking')
            for d in conn.execute("SELECT * FROM devices WHERE subscription_id=? AND state!='revoked'",(sub['id'],)).fetchall():
                device=dict(d)
                _remember(conn,sub,dict(node),service.device_identity(device),'retiring',device['secret'],device['expires'],device_id=device['id'],state='revoking')
    conn.execute("UPDATE servers SET state='draining',version=version+1 WHERE id=?", (sid,))
    carry_version(conn, sid, version, version+1)
    conn.execute('INSERT INTO wg_fleet_operations(id,source_server_id,lost,deadline,created,updated) VALUES (?,?,?,?,?,?)',
                 (oid, sid, int(lost), now if lost else now + deadline_hours * 3600, now, now))
    conn.execute("""INSERT INTO wg_fleet_moves(operation_id,subscription_id)
        SELECT ?,id FROM subscriptions WHERE server_id=? AND protocol='wg'
          AND (state='pending' OR (state='active' AND expires>?))""", (oid, sid, now))
    return operation(conn, oid)


def operation(conn, oid):
    raw = conn.execute('SELECT * FROM wg_fleet_operations WHERE id=?', (oid,)).fetchone()
    if not raw:
        raise HTTPException(404, 'Операция не найдена')
    row = dict(raw)
    states = {r['state']: r['n'] for r in conn.execute('SELECT state,count(*) n FROM wg_fleet_moves WHERE operation_id=? GROUP BY state', (oid,))}
    remaining = conn.execute("SELECT count(*) FROM wg_fleet_placements WHERE server_id=? AND state!='revoked'", (row['source_server_id'],)).fetchone()[0]
    return {k: row[k] for k in ('id', 'source_server_id', 'state', 'error')} | {
        'total': sum(states.values()), 'completed': states.get('completed', 0),
        'failed': states.get('blocked', 0), 'deadline': row['deadline'],
        'lost': bool(row['lost']), 'pending_revocations': remaining}


def _remote(placement, sub):
    return {**sub, 'id': c.decrypt(placement['credential_id']),
            'server_id': placement['server_id'], 'expires': placement['target_expires'],
            'credential_id': placement['credential_id'], 'secret': placement['secret']}


async def revoke_placements(sub, *, include_primary=True):
    """Confirm ALL remote revocations before owner cleanup can erase evidence."""
    from . import providers
    with db() as conn:
        # Persist the intent before remote IO. An unreachable standby must not
        # cause reconciliation to issue another peer while revocation retries.
        conn.execute('INSERT OR IGNORE INTO wg_fleet_revocations VALUES (?,?)', (sub['id'],int(time.time())))
        conn.execute('UPDATE subscriptions SET free_auto_renew=0 WHERE id=?', (sub['id'],))
    rows = all_rows("SELECT * FROM wg_fleet_placements WHERE subscription_id=? AND state!='revoked'", (sub['id'],))
    failed = False
    for row in rows:
        if not include_primary and row['role'] in ('primary', 'device'):
            continue
        with db() as conn:
            conn.execute("UPDATE wg_fleet_placements SET state='revoking' WHERE id=?", (row['id'],))
        try:
            await asyncio.wait_for(providers.disable(_remote(row, sub)), timeout=35)
        except Exception:
            failed = True
            with db() as conn:
                conn.execute("UPDATE wg_fleet_placements SET error='RemoteRevokeUnconfirmed',next_attempt=? WHERE id=?", (int(time.time())+60, row['id']))
        else:
            with db() as conn:
                conn.execute("UPDATE wg_fleet_placements SET state='revoked',secret=NULL,error=NULL,updated=? WHERE id=?", (int(time.time()), row['id']))
    if failed:
        raise RuntimeError('FleetRemoteRevokeUnconfirmed')


def confirmed_primary_revocation(conn, subscription_id):
    conn.execute("""UPDATE wg_fleet_placements SET state='revoked',secret=NULL,error=NULL,updated=?
        WHERE subscription_id=? AND role IN ('primary','device')""",(int(time.time()),subscription_id))


async def _apply_placement(pid):
    from . import providers
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = conn.execute('SELECT * FROM wg_fleet_placements WHERE id=?', (pid,)).fetchone()
        if not raw or raw['state'] == 'revoked' or raw['next_attempt'] > now:
            return
        p = dict(raw)
        sub = dict(conn.execute('SELECT * FROM subscriptions WHERE id=?', (p['subscription_id'],)).fetchone())
        node = dict(conn.execute('SELECT * FROM servers WHERE id=?', (p['server_id'],)).fetchone())
        if (p['state']=='revoking' and p['attempts']==0 and not p['secret']
                and not conn.execute('SELECT 1 FROM wgdashboard_keys WHERE server_id=? AND credential_id=?',
                                     (p['server_id'],c.decrypt(p['credential_id']))).fetchone()):
            # Allocation was reserved but no network attempt was started. The
            # provider stores its key BEFORE remote creation; nothing was issued.
            conn.execute("UPDATE wg_fleet_placements SET state='revoked',error=NULL,updated=? WHERE id=?", (now,pid))
            return
        if p['state'] != 'revoking':
            if revocation_requested(conn,sub['id']) or sub['state'] != 'active' or sub['expires'] <= now:
                conn.execute("UPDATE wg_fleet_placements SET state='revoking' WHERE id=?", (pid,))
                p['state'] = 'revoking'
            elif node['state'] != 'active' or not health.health_summary(conn, node['id'], node['version'])['healthy']:
                return
            else:
                p['target_expires'] = sub['expires']
                if p['device_id']:
                    device = conn.execute('SELECT state,expires FROM devices WHERE id=?', (p['device_id'],)).fetchone()
                    if not device or device['state'] != 'active' or device['expires'] <= now:
                        conn.execute("UPDATE wg_fleet_placements SET state='revoking' WHERE id=?", (pid,))
                        p['state'] = 'revoking'
                    else:
                        p['target_expires'] = min(sub['expires'], device['expires'])
        # Persist the exact term before the network call. Lost acknowledgments
        # cannot lead to a new credential or a different remote deadline.
        conn.execute('UPDATE wg_fleet_placements SET attempts=attempts+1,next_attempt=?,target_expires=? WHERE id=?',
                     (now+60, p['target_expires'], pid))
    try:
        if p['state'] == 'revoking':
            await asyncio.wait_for(providers.disable(_remote(p, sub)), timeout=35)
            with db() as conn:
                conn.execute("UPDATE wg_fleet_placements SET state='revoked',secret=NULL,error=NULL,updated=? WHERE id=?", (int(time.time()), pid))
            return
        raw_config = await asyncio.wait_for(providers.provision(_remote(p, sub)), timeout=35)
        if not isinstance(raw_config, str) or not raw_config.startswith('[Interface]') or len(raw_config) > 65536:
            raise RuntimeError('FleetConfigInvalid')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM subscriptions WHERE id=?', (sub['id'],)).fetchone()
            current_node = conn.execute('SELECT version,state FROM servers WHERE id=?', (node['id'],)).fetchone()
            current_p = conn.execute('SELECT state,role FROM wg_fleet_placements WHERE id=?', (pid,)).fetchone()
            if (not current or current['state'] != 'active' or current['expires'] != sub['expires']
                    or current['primary_key_state'] != 'active' or current['expires'] <= int(time.time())
                    or current_node['version'] != node['version'] or current_node['state'] != 'active'
                    or revocation_requested(conn,sub['id'])
                    or current_p['state'] == 'revoking' or current_p['role'] != p['role']):
                conn.execute("UPDATE wg_fleet_placements SET state='revoking',next_attempt=0,error='AssignmentChanged' WHERE id=?", (pid,))
                return
            if not health.health_summary(conn, node['id'], node['version'])['healthy']:
                return
            conn.execute("""UPDATE wg_fleet_placements SET state='active',secret=?,server_version=?,
                target_expires=?,next_attempt=0,error=NULL,updated=? WHERE id=?""",
                (c.encrypt(raw_config), node['version'], p['target_expires'], int(time.time()), pid))
    except Exception:
        with db() as conn:
            conn.execute('UPDATE wg_fleet_placements SET error=?,next_attempt=?,updated=? WHERE id=?',
                         ('RemoteRevokeUnconfirmed' if p['state']=='revoking' else 'PeerPreparationFailed', now+60, now, pid))


def _prepare_moves(conn, now):
    """Populate migration work without changing any account or trial deadline."""
    from . import service
    for operation_row in conn.execute("SELECT * FROM wg_fleet_operations WHERE state!='completed'").fetchall():
        op = dict(operation_row)
        for raw in conn.execute("""SELECT s.* FROM wg_fleet_moves m JOIN subscriptions s ON s.id=m.subscription_id
                WHERE m.operation_id=? AND m.state!='completed'""", (op['id'],)).fetchall():
            sub = dict(raw)
            if sub['server_id'] != op['source_server_id'] or sub['state'] in ('revoked', 'expired') or (sub['state']=='active' and sub['expires'] <= now):
                conn.execute("UPDATE wg_fleet_moves SET state='completed',error=NULL WHERE operation_id=? AND subscription_id=?", (op['id'], sub['id']))
                continue
            if _busy(conn, sub):
                continue
            if sub['state'] == 'pending':
                # A pending remote call could already have created a peer. Its
                # old identity must first be tracked for eventual revocation.
                old = conn.execute('SELECT * FROM servers WHERE id=?', (sub['server_id'],)).fetchone()
                _remember(conn, sub, dict(old), service.primary_identity(sub), 'retiring', sub['secret'], sub['expires'], state='revoking')
                options = candidates(conn, exclude=(sub['server_id'],), now=now, guest=bool(sub.get('wg_configuration')))
                if options:
                    conn.execute('UPDATE subscriptions SET server_id=?,credential_id=? WHERE id=?',
                                 (options[0]['id'], c.encrypt(str(uuid.uuid4())), sub['id']))
                    conn.execute("UPDATE wg_fleet_moves SET state='completed',error=NULL WHERE operation_id=? AND subscription_id=?", (op['id'], sub['id']))
                continue
            standby = reserve_standby(conn, sub, now=now)
            if not standby:
                conn.execute("UPDATE wg_fleet_moves SET state='blocked',error='NoHealthyCapacity' WHERE operation_id=? AND subscription_id=?", (op['id'], sub['id']))
                continue
            target = dict(conn.execute('SELECT * FROM servers WHERE id=?', (standby['server_id'],)).fetchone())
            devices = [dict(r) for r in conn.execute("SELECT * FROM devices WHERE subscription_id=? AND state='active'", (sub['id'],))]
            ready_devices = []
            for device in devices:
                ident = service.device_identity(device)
                p = conn.execute('SELECT * FROM wg_fleet_placements WHERE server_id=? AND identity_hash=?', (target['id'], _identity_hash(ident))).fetchone()
                if not p:
                    _remember(conn, sub, target, ident, 'standby_device', None, min(device['expires'], sub['expires']), device_id=device['id'], state='pending')
                elif p['state']=='active' and p['target_expires']==min(device['expires'], sub['expires']) and p['server_version']==target['version']:
                    ready_devices.append(dict(p))
            if (standby['state'] != 'active' or standby['target_expires'] != sub['expires']
                    or len(ready_devices) != len(devices) or standby['server_version'] != target['version']
                    or target['state'] != 'active' or not health.health_summary(conn, target['id'], target['version'])['healthy']):
                conn.execute("UPDATE wg_fleet_moves SET state='pending',error=NULL WHERE operation_id=? AND subscription_id=?", (op['id'], sub['id']))
                continue
            conn.execute("""UPDATE wg_fleet_placements SET role='retiring',retire_after=?,updated=?
                WHERE subscription_id=? AND server_id=? AND state!='revoked'""", (op['deadline'], now, sub['id'], op['source_server_id']))
            conn.execute("UPDATE wg_fleet_placements SET role='primary' WHERE id=?", (standby['id'],))
            conn.execute('UPDATE subscriptions SET server_id=?,credential_id=?,secret=? WHERE id=?',
                         (target['id'], standby['credential_id'], standby['secret'], sub['id']))
            for p in ready_devices:
                conn.execute("UPDATE wg_fleet_placements SET role='device' WHERE id=?", (p['id'],))
                conn.execute('UPDATE devices SET secret=? WHERE id=?', (p['secret'], p['device_id']))
            conn.execute("UPDATE wg_fleet_moves SET state='completed',error=NULL WHERE operation_id=? AND subscription_id=?", (op['id'], sub['id']))


async def reconcile(max_jobs=20):
    """Called under service.lock; DB reservations survive process restarts."""
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        active_policy = enabled(conn)
        # Disabling allocation never stops expiry/revocation of issued peers.
        for raw in conn.execute("SELECT * FROM subscriptions WHERE protocol='wg'").fetchall():
            sub = dict(raw)
            revoking = revocation_requested(conn,sub['id'])
            if sub['primary_key_state']=='rotating':
                # A failed revoke on the primary must not leave a reachable
                # backup key from the old downloaded feed usable indefinitely.
                conn.execute("""UPDATE wg_fleet_placements SET state='revoking',next_attempt=0,
                    role=CASE WHEN role='primary' THEN role ELSE 'retiring' END,retire_after=0
                    WHERE subscription_id=? AND role IN ('primary','standby','standby_device')
                    AND state IN ('active','pending')""",(sub['id'],))
            if active_policy and not revoking and sub['state']=='active' and sub['expires']>now:
                sync_primary(conn, sub)
                if not _busy(conn, sub):
                    standby = conn.execute("SELECT * FROM wg_fleet_placements WHERE subscription_id=? AND role='standby' AND state IN ('active','pending')", (sub['id'],)).fetchone()
                    if standby:
                        node = conn.execute('SELECT * FROM servers WHERE id=?', (standby['server_id'],)).fetchone()
                        status = health.health_summary(conn, node['id'], node['version'], now=now)
                        replacements = candidates(conn,exclude=(sub['server_id'],node['id']),now=now) if confirmed_failure(conn,node['id'],now) else []
                        if node['state'] != 'active' or bool(replacements) or status['status']=='configuration_changed':
                            operation=conn.execute("SELECT deadline FROM wg_fleet_operations WHERE source_server_id=? AND state!='completed'",(node['id'],)).fetchone()
                            deadline=operation['deadline'] if operation else now+86400 if node['state']=='draining' else now
                            retire_standby(conn,sub['id'],node['id'],deadline)
                    standby = reserve_standby(conn, sub, now=now)
                    reserve_device_standbys(conn, sub, standby)
            if revoking or sub['state'] in ('revoked','expired') or (sub['state']=='active' and sub['expires']<=now):
                conn.execute("UPDATE wg_fleet_placements SET state='revoking',next_attempt=0 WHERE subscription_id=? AND state IN ('active','pending')", (sub['id'],))
            else:
                conn.execute("""UPDATE wg_fleet_placements SET state='pending',next_attempt=0 WHERE subscription_id=?
                    AND role='standby' AND state='active' AND target_expires!=?""", (sub['id'], sub['expires']))
                conn.execute("""UPDATE wg_fleet_placements SET state='pending',next_attempt=0 WHERE subscription_id=?
                    AND role='standby_device' AND state='active' AND target_expires!=MIN(?,
                    (SELECT expires FROM devices WHERE id=wg_fleet_placements.device_id))""", (sub['id'],sub['expires']))
        if active_policy:
            for node in conn.execute("SELECT s.* FROM servers s JOIN wg_fleet_probes p ON p.server_id=s.id WHERE s.state='active'").fetchall():
                state = health.health_summary(conn, node['id'], node['version'], now=now)['status']
                bound = conn.execute("SELECT 1 FROM subscriptions WHERE server_id=? AND state='active' AND expires>?", (node['id'], now)).fetchone()
                alternate = bool(candidates(conn,exclude=(node['id'],),now=now)) if bound and state=='unhealthy' else False
                if not alternate and bound and state=='unhealthy':
                    for ready in conn.execute("""SELECT DISTINCT s.id,s.version FROM wg_fleet_placements p
                        JOIN servers s ON s.id=p.server_id JOIN subscriptions sub ON sub.id=p.subscription_id
                        WHERE sub.server_id=? AND p.role='standby' AND p.state='active' AND s.state='active'
                        AND p.target_expires=sub.expires AND p.server_version=s.version""",(node['id'],)):
                        if health.health_summary(conn,ready['id'],ready['version'],now=now)['healthy']:
                            alternate=True
                            break
                if bound and alternate and state=='unhealthy' and confirmed_failure(conn,node['id'],now):
                    drain(conn, node['id'], node['version'], lost=True)
            _prepare_moves(conn, now)
        conn.execute("UPDATE wg_fleet_placements SET state='revoking',next_attempt=0 WHERE role='retiring' AND state IN ('active','pending') AND retire_after<=?", (now,))
        # A revoked technical device may still have an old migration placement.
        conn.execute("""UPDATE wg_fleet_placements SET state='revoking',next_attempt=0 WHERE device_id IN
            (SELECT id FROM devices WHERE state IN ('revoked','revoking','rotating') OR (state='active' AND expires<=?))
            AND state IN ('active','pending')""", (now,))
        jobs = [r[0] for r in conn.execute("""SELECT id FROM wg_fleet_placements
            WHERE state IN ('pending','revoking') AND next_attempt<=?
            ORDER BY (state='revoking') DESC,created,id LIMIT ?""", (now, max_jobs))]
    for pid in jobs:
        await _apply_placement(pid)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if active_policy:
            _prepare_moves(conn, int(time.time()))
        for op in conn.execute("SELECT * FROM wg_fleet_operations WHERE state!='completed'").fetchall():
            remaining = conn.execute("SELECT 1 FROM wg_fleet_moves WHERE operation_id=? AND state!='completed'", (op['id'],)).fetchone()
            peers = conn.execute("SELECT 1 FROM wg_fleet_placements WHERE server_id=? AND state!='revoked'", (op['source_server_id'],)).fetchone()
            # Never archive a disappeared machine on the strength of a local
            # delete. Confirm remote revocation first, including returning nodes.
            state = 'preparing' if remaining else 'revoking' if peers else 'completed'
            error = 'NoHealthyCapacity' if conn.execute("SELECT 1 FROM wg_fleet_moves WHERE operation_id=? AND state='blocked'", (op['id'],)).fetchone() else None
            if state=='revoking' and conn.execute("SELECT 1 FROM wg_fleet_placements WHERE server_id=? AND error='RemoteRevokeUnconfirmed' AND state!='revoked'", (op['source_server_id'],)).fetchone():
                error='RemoteRevokeUnconfirmed'
            conn.execute('UPDATE wg_fleet_operations SET state=?,error=?,updated=? WHERE id=?', (state, error, int(time.time()), op['id']))
            if state=='completed':
                conn.execute("UPDATE servers SET state='disabled',version=version+1 WHERE id=?", (op['source_server_id'],))
                conn.execute('UPDATE wg_fleet_nodes SET archived=1 WHERE server_id=?', (op['source_server_id'],))


async def worker():
    from . import service
    while True:
        try:
            async with service.lock:
                await reconcile()
        except Exception as exc:
            log.warning('WG fleet reconcile error=%s', type(exc).__name__)
        await asyncio.sleep(15)


async def dns_worker():
    from . import wg_fleet_dns
    while True:
        try:
            await wg_fleet_dns.sync()
        except Exception as exc:
            log.warning('WG fleet DNS error=%s',type(exc).__name__)
        await asyncio.sleep(30)
