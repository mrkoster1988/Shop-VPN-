"""Authenticated observations of real WireGuard traffic and strict fleet selection.

Only the dedicated probe API may call ``ingest``. A panel response, historic
transfer counter, or a customer's old handshake is never a successful probe.
Call ``select`` inside the same write transaction that reserves a placement;
this module deliberately does not create subscriptions or allocate peers.
"""
from fractions import Fraction
from itertools import groupby
import math
import re
import secrets
import time


HEALTH_TTL_SECONDS = 180
REPORT_MAX_AGE_SECONDS = 90
MAX_PROBE_SECONDS = 60
HANDSHAKE_MAX_AGE_SECONDS = 180
CLOCK_SKEW_SECONDS = 5
MAX_INTEGER = 2**63 - 1
FAILURE_CODES = frozenset({
    'namespace_unavailable', 'interface_mismatch', 'peer_mismatch',
    'route_mismatch', 'dns_failed', 'https_failed', 'handshake_missing',
    'handshake_stale', 'traffic_missing', 'metrics_failed', 'probe_failed',
})
REPORT_FIELDS = frozenset({
    'schema_version', 'server_id', 'server_version', 'generation', 'probe_id',
    'sequence', 'started_at', 'finished_at', 'handshake_at', 'rx_delta_bytes',
    'tx_delta_bytes', 'dns_ok', 'https_ok', 'cpu_percent', 'rx_bps', 'tx_bps',
    'bandwidth_capacity_bps', 'failure_code',
})


class HealthReportError(ValueError):
    """A safe, bounded code; never include remote response bodies or tokens."""


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise HealthReportError('InvalidProbeInteger')
    return value


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise HealthReportError('InvalidProbeIdentity')
    return value


def _one(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row is not None else None


def create_schema(conn):
    """No executescript/commit: preserve the caller's migration transaction."""
    conn.execute('''CREATE TABLE IF NOT EXISTS wg_fleet_probes (
        server_id TEXT PRIMARY KEY REFERENCES servers(id),
        server_version INTEGER NOT NULL CHECK(server_version > 0),
        probe_id TEXT NOT NULL UNIQUE,
        generation INTEGER NOT NULL CHECK(generation > 0),
        last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
        last_finished_at INTEGER NOT NULL DEFAULT 0,
        registered_at INTEGER NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS wg_fleet_health (
        server_id TEXT PRIMARY KEY REFERENCES wg_fleet_probes(server_id),
        server_version INTEGER NOT NULL, generation INTEGER NOT NULL,
        probe_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL,
        received_at INTEGER NOT NULL, handshake_at INTEGER NOT NULL,
        rx_delta_bytes INTEGER NOT NULL, tx_delta_bytes INTEGER NOT NULL,
        dns_ok INTEGER NOT NULL CHECK(dns_ok IN (0,1)),
        https_ok INTEGER NOT NULL CHECK(https_ok IN (0,1)),
        cpu_percent REAL, rx_bps INTEGER, tx_bps INTEGER,
        bandwidth_capacity_bps INTEGER,
        healthy INTEGER NOT NULL CHECK(healthy IN (0,1)), failure_code TEXT)''')


def register_probe(conn, server_id, server_version, probe_id, generation):
    """Enroll a source; a new incarnation cannot replay its predecessor's data.

    Repeating the exact registration is idempotent and preserves its sequence.
    Rotating the probe identity needs a higher generation or server version.
    The caller owns token generation, hashing, and authorization.
    """
    _identity(server_id)
    _identity(probe_id)
    _integer(server_version, 1)
    _integer(generation, 1)
    node = _one(conn, 'SELECT protocol,version FROM servers WHERE id=?', (server_id,))
    if not node or node['protocol'] != 'wg' or node['version'] != server_version:
        raise HealthReportError('ProbeServerVersionMismatch')
    old = _one(conn, 'SELECT * FROM wg_fleet_probes WHERE server_id=?', (server_id,))
    if old:
        if generation < old['generation'] or server_version < old['server_version']:
            raise HealthReportError('ProbeGenerationRollback')
        if generation == old['generation'] and server_version == old['server_version']:
            if probe_id != old['probe_id']:
                raise HealthReportError('ProbeGenerationRequired')
            return {'server_id': server_id, 'server_version': server_version,
                    'probe_id': probe_id, 'generation': generation}
    collision = _one(conn, 'SELECT server_id FROM wg_fleet_probes WHERE probe_id=?', (probe_id,))
    if collision and collision['server_id'] != server_id:
        raise HealthReportError('ProbeIdentityAlreadyRegistered')
    conn.execute('DELETE FROM wg_fleet_health WHERE server_id=?', (server_id,))
    conn.execute('''INSERT INTO wg_fleet_probes
        (server_id,server_version,probe_id,generation,last_sequence,last_finished_at,registered_at)
        VALUES (?,?,?,?,0,0,?) ON CONFLICT(server_id) DO UPDATE SET
        server_version=excluded.server_version,probe_id=excluded.probe_id,
        generation=excluded.generation,last_sequence=0,last_finished_at=0,
        registered_at=excluded.registered_at''',
        (server_id, server_version, probe_id, generation, int(time.time())))
    return {'server_id': server_id, 'server_version': server_version,
            'probe_id': probe_id, 'generation': generation}


def validate_report(report, *, now=None):
    """Validate shape and time before any database change, including failed tests."""
    now = int(time.time()) if now is None else _integer(now, 1)
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise HealthReportError('InvalidProbeReportFields')
    data = dict(report)
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise HealthReportError('UnsupportedProbeReportVersion')
    for field in ('server_id', 'probe_id'):
        _identity(data[field])
    for field in ('server_version', 'generation', 'sequence', 'started_at', 'finished_at'):
        _integer(data[field], 1)
    for field in ('handshake_at', 'rx_delta_bytes', 'tx_delta_bytes'):
        _integer(data[field])
    for field in ('dns_ok', 'https_ok'):
        if type(data[field]) is not bool:
            raise HealthReportError('InvalidProbeBoolean')
    if data['failure_code'] is not None and (
            not isinstance(data['failure_code'], str) or data['failure_code'] not in FAILURE_CODES):
        raise HealthReportError('InvalidProbeFailureCode')
    started, finished = data['started_at'], data['finished_at']
    if finished < started or finished - started > MAX_PROBE_SECONDS:
        raise HealthReportError('InvalidProbeDuration')
    if finished > now + CLOCK_SKEW_SECONDS or now - finished > REPORT_MAX_AGE_SECONDS:
        raise HealthReportError('StaleProbeReport')
    if data['handshake_at'] > finished + CLOCK_SKEW_SECONDS:
        raise HealthReportError('InvalidProbeHandshakeTime')
    cpu = data['cpu_percent']
    if cpu is not None:
        if type(cpu) not in (int, float) or not math.isfinite(cpu) or not 0 <= cpu <= 100:
            raise HealthReportError('InvalidProbeCpu')
    for field in ('rx_bps', 'tx_bps', 'bandwidth_capacity_bps'):
        if data[field] is not None:
            _integer(data[field], 1 if field == 'bandwidth_capacity_bps' else 0)
    return data


def _failure(data):
    if data['failure_code']:
        return data['failure_code']
    if not data['dns_ok']:
        return 'dns_failed'
    if not data['https_ok']:
        return 'https_failed'
    if not data['handshake_at']:
        return 'handshake_missing'
    if data['finished_at'] - data['handshake_at'] > HANDSHAKE_MAX_AGE_SECONDS:
        return 'handshake_stale'
    if not data['rx_delta_bytes'] or not data['tx_delta_bytes']:
        return 'traffic_missing'
    if any(data[field] is None for field in ('cpu_percent', 'rx_bps', 'tx_bps', 'bandwidth_capacity_bps')):
        return 'metrics_failed'
    return None


def ingest(conn, report, authenticated_probe_id, *, now=None):
    """Accept a new observation only for its enrolled, authenticated node source."""
    now = int(time.time()) if now is None else _integer(now, 1)
    data = validate_report(report, now=now)
    if not secrets.compare_digest(data['probe_id'], _identity(authenticated_probe_id)):
        raise HealthReportError('ProbeAuthenticationMismatch')
    registration = _one(conn, 'SELECT * FROM wg_fleet_probes WHERE server_id=?', (data['server_id'],))
    node = _one(conn, 'SELECT protocol,version FROM servers WHERE id=?', (data['server_id'],))
    if not registration or registration['probe_id'] != authenticated_probe_id:
        raise HealthReportError('ProbeNotRegistered')
    if (not node or node['protocol'] != 'wg' or node['version'] != data['server_version']
            or registration['server_version'] != data['server_version']
            or registration['generation'] != data['generation']):
        raise HealthReportError('ProbeServerVersionMismatch')
    if (data['sequence'] <= registration['last_sequence']
            or data['finished_at'] < registration['last_finished_at']):
        raise HealthReportError('ProbeReportReplay')
    changed = conn.execute('''UPDATE wg_fleet_probes SET last_sequence=?,last_finished_at=?
        WHERE server_id=? AND probe_id=? AND server_version=? AND generation=?
        AND last_sequence<? AND last_finished_at<=?''',
        (data['sequence'], data['finished_at'], data['server_id'], authenticated_probe_id,
         data['server_version'], data['generation'], data['sequence'], data['finished_at'])).rowcount
    if changed != 1:
        raise HealthReportError('ProbeReportReplay')
    failure = _failure(data)
    columns = ('server_id', 'server_version', 'generation', 'probe_id', 'sequence',
               'started_at', 'finished_at', 'handshake_at', 'rx_delta_bytes',
               'tx_delta_bytes', 'dns_ok', 'https_ok', 'cpu_percent', 'rx_bps',
               'tx_bps', 'bandwidth_capacity_bps')
    names = ','.join(columns) + ',received_at,healthy,failure_code'
    updates = ','.join(f'{name}=excluded.{name}' for name in (*columns[1:], 'received_at', 'healthy', 'failure_code'))
    conn.execute(f'INSERT INTO wg_fleet_health ({names}) VALUES ({",".join("?" for _ in range(19))}) '
                 f'ON CONFLICT(server_id) DO UPDATE SET {updates}',
                 tuple(data[name] for name in columns) + (now, int(failure is None), failure))
    return health_summary(conn, data['server_id'], data['server_version'], now=now)


def health_summary(conn, sid, version, *, now=None):
    """Public admin status: no endpoint, private key, bearer, or response body."""
    now = int(time.time()) if now is None else _integer(now, 1)
    registration = _one(conn, 'SELECT * FROM wg_fleet_probes WHERE server_id=?', (sid,))
    node = _one(conn, 'SELECT version FROM servers WHERE id=?', (sid,))
    base = {'server_id': sid, 'server_version': version, 'healthy': False,
            'status': 'unregistered', 'checked_at': None, 'received_at': None,
            'generation': registration['generation'] if registration else None,
            'probe_id': registration['probe_id'] if registration else None,
            'cpu_percent': None, 'rx_bps': None, 'tx_bps': None,
            'bandwidth_capacity_bps': None, 'handshake_at': None,
            'bandwidth_utilization_percent': None, 'failure_code': None}
    if not registration:
        return base
    if not node or node['version'] != version or registration['server_version'] != version:
        return {**base, 'status': 'configuration_changed'}
    data = _one(conn, 'SELECT * FROM wg_fleet_health WHERE server_id=?', (sid,))
    if not data:
        return {**base, 'status': 'never_reported'}
    if any(data[field] != registration[field] for field in ('server_version', 'generation', 'probe_id')):
        return {**base, 'status': 'configuration_changed'}
    base.update({field: data[field] for field in ('received_at', 'cpu_percent', 'rx_bps', 'tx_bps',
                                                'bandwidth_capacity_bps', 'handshake_at', 'failure_code')})
    base['checked_at'] = data['finished_at']
    if all(data[field] is not None for field in ('rx_bps', 'tx_bps', 'bandwidth_capacity_bps')):
        base['bandwidth_utilization_percent'] = 100 * max(data['rx_bps'], data['tx_bps']) / data['bandwidth_capacity_bps']
    if not -CLOCK_SKEW_SECONDS <= now - data['finished_at'] <= HEALTH_TTL_SECONDS:
        return {**base, 'status': 'stale'}
    # Fresh transfer evidence is bounded again at selection time: a report's
    # remaining TTL must not make a subsequently stale handshake look fresh.
    if data['healthy'] and now - data['handshake_at'] > HANDSHAKE_MAX_AGE_SECONDS:
        return {**base, 'status': 'stale', 'failure_code': 'handshake_stale'}
    return {**base, 'healthy': bool(data['healthy']),
            'status': 'healthy' if data['healthy'] else 'unhealthy'}


def _ranked(conn, candidates, now):
    result = []
    seen = set()
    for original in candidates:
        row = dict(original)
        sid = row.get('server_id') or row.get('id')
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if row.get('state', 'active') != 'active' or not row.get('accept_new', True):
            continue
        if row.get('health') == 'error':
            continue  # New issuance still needs the management API.
        try:
            version = _integer(row.get('version', row.get('server_version')), 1)
            capacity = _integer(row.get('capacity', 0))
            occupied = _integer(row.get('occupied', 0))
            reserved = _integer(row.get('reserved', 0))
        except HealthReportError:
            continue
        used = occupied + reserved
        if capacity and used >= capacity:
            continue
        status = health_summary(conn, sid, version, now=now)
        if not status['healthy']:
            continue
        # Fractions preserve a strict minimum: rounding may never create a tie
        # between different loads. Zero capacity means explicitly unbounded
        # subscription slots, not unbounded CPU or network throughput.
        cpu = Fraction(str(status['cpu_percent'])) / 100
        bandwidth = Fraction(max(status['rx_bps'], status['tx_bps']), status['bandwidth_capacity_bps'])
        slots = Fraction(used, capacity) if capacity else Fraction(0)
        score = max(cpu, bandwidth, slots)
        if score >= 1:
            continue
        result.append((score, row, status))
    return result


def eligible(conn, candidates, *, now=None):
    now = int(time.time()) if now is None else _integer(now, 1)
    return [{**row, 'fleet_load_score': float(score), 'fleet_health': status}
            for score, row, status in _ranked(conn, candidates, now)]


def ordered(conn, candidates, *, now=None):
    """Rank once; randomize only mathematically equal scores within each group."""
    now = int(time.time()) if now is None else _integer(now,1)
    ranked=sorted(_ranked(conn,candidates,now),key=lambda item:item[0])
    result=[]
    for _,group in groupby(ranked,key=lambda item:item[0]):
        tied=[item[1] for item in group]
        secrets.SystemRandom().shuffle(tied)
        result.extend(tied)
    return result


def select(conn, candidates, *, now=None):
    """Choose the strict minimum; randomize only exact ties.

    Caller must reserve the resulting placement before committing its write
    transaction. ``occupied`` includes pending primary issuance, ``reserved``
    includes standby and migration reservations, each counted exactly once.
    """
    now = int(time.time()) if now is None else _integer(now, 1)
    ranked = _ranked(conn, candidates, now)
    if not ranked:
        return None
    minimum = min(score for score, _, _ in ranked)
    tied = [row for score, row, _ in ranked if score == minimum]
    return tied[0] if len(tied) == 1 else secrets.choice(tied)
