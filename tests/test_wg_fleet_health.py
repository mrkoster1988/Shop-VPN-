"""Traffic evidence, anti-replay, strict selection and isolated probe regressions."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from app import wg_fleet_health as health


NOW = 1_800_000_000


@pytest.fixture
def conn():
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('CREATE TABLE servers (id TEXT PRIMARY KEY,protocol TEXT,version INTEGER)')
    health.create_schema(connection)
    yield connection
    connection.close()


def node(conn, sid='node-a', *, capacity=20, occupied=0, reserved=0):
    conn.execute('INSERT INTO servers VALUES (?,?,?)', (sid, 'wg', 1))
    health.register_probe(conn, sid, 1, 'probe-' + sid, 1)
    return {'id': sid, 'version': 1, 'state': 'active', 'health': 'ok',
            'capacity': capacity, 'occupied': occupied, 'reserved': reserved}


def report(sid='node-a', **changes):
    return {'schema_version': 1, 'server_id': sid, 'server_version': 1, 'generation': 1,
            'probe_id': 'probe-' + sid, 'sequence': 1, 'started_at': NOW - 3,
            'finished_at': NOW, 'handshake_at': NOW - 10,
            'rx_delta_bytes': 2048, 'tx_delta_bytes': 1024, 'dns_ok': True,
            'https_ok': True, 'cpu_percent': 10.0, 'rx_bps': 10_000_000,
            'tx_bps': 20_000_000, 'bandwidth_capacity_bps': 100_000_000,
            'failure_code': None, **changes}


def ingest(conn, data):
    return health.ingest(conn, data, data['probe_id'], now=NOW)


def test_real_evidence_and_freshness_bound_at_selection(conn):
    row = node(conn)
    assert health.select(conn, [row], now=NOW) is None
    summary = ingest(conn, report())
    assert summary['status'] == 'healthy'
    assert summary['bandwidth_utilization_percent'] == 20
    assert health.select(conn, [row], now=NOW)['id'] == 'node-a'
    assert health.select(conn, [row], now=NOW + 171) is None
    assert health.health_summary(conn, 'node-a', 1, now=NOW + 171)['failure_code'] == 'handshake_stale'
    assert health.select(conn, [row], now=NOW + health.HEALTH_TTL_SECONDS + 1) is None
    assert 'token' not in json.dumps(summary)


def test_ordered_keeps_strict_scores_and_randomizes_only_equal_groups(conn,monkeypatch):
    a=node(conn,'a');b=node(conn,'b');c=node(conn,'c')
    for sid,cpu in [('a',30),('b',10),('c',10)]:
        ingest(conn,report(sid,probe_id='probe-'+sid,cpu_percent=cpu))
    groups=[]
    class Random:
        def shuffle(self,items):
            groups.append({r['id'] for r in items})
            items.reverse()
    monkeypatch.setattr(health.secrets,'SystemRandom',Random)
    assert [row['id'] for row in health.ordered(conn,[a,b,c],now=NOW)]==['c','b','a']
    assert groups==[{'b','c'},{'a'}]


@pytest.mark.parametrize('changes,reason', [
    ({'dns_ok': False}, 'dns_failed'),
    ({'https_ok': False}, 'https_failed'),
    ({'handshake_at': 0}, 'handshake_missing'),
    ({'handshake_at': NOW - 181}, 'handshake_stale'),
    ({'rx_delta_bytes': 0}, 'traffic_missing'),
    ({'tx_delta_bytes': 0}, 'traffic_missing'),
    ({'cpu_percent': None}, 'metrics_failed'),
    ({'rx_bps': None}, 'metrics_failed'),
    ({'failure_code': 'route_mismatch'}, 'route_mismatch'),
])
def test_old_handshake_or_transfer_totals_do_not_prove_traffic(conn, changes, reason):
    row = node(conn)
    ingest(conn, report())
    summary = ingest(conn, report(sequence=2, **changes))
    assert not summary['healthy'] and summary['failure_code'] == reason
    assert health.select(conn, [row], now=NOW) is None


@pytest.mark.parametrize('changes', [
    {'cpu_percent': float('nan')}, {'cpu_percent': float('inf')}, {'cpu_percent': True},
    {'cpu_percent': -1}, {'cpu_percent': 101}, {'rx_bps': -1},
    {'bandwidth_capacity_bps': 0}, {'tx_delta_bytes': True}, {'sequence': 0},
    {'sequence': 2**63}, {'started_at': NOW + 1}, {'started_at': NOW - 61},
    {'finished_at': NOW - 91, 'started_at': NOW - 92},
    {'started_at': NOW + 6, 'finished_at': NOW + 6},
    {'handshake_at': NOW + 6}, {'dns_ok': 'true'},
    {'failure_code': 'secret-data'}, {'failure_code': []}, {'schema_version': True},
])
def test_bogus_input_never_replaces_valid_observation(conn, changes):
    row = node(conn)
    ingest(conn, report())
    with pytest.raises(health.HealthReportError):
        ingest(conn, report(**{'sequence': 2, **changes}))
    assert health.select(conn, [row], now=NOW) == row
    assert conn.execute('SELECT last_sequence FROM wg_fleet_probes').fetchone()[0] == 1


def test_replay_auth_generation_and_actual_server_version(conn):
    row = node(conn)
    ingest(conn, report(sequence=3))
    with pytest.raises(health.HealthReportError, match='ProbeReportReplay'):
        ingest(conn, report(sequence=2))
    with pytest.raises(health.HealthReportError, match='ProbeAuthenticationMismatch'):
        health.ingest(conn, report(sequence=4), 'other-probe', now=NOW)
    health.register_probe(conn, 'node-a', 1, 'probe-node-a', 1)
    with pytest.raises(health.HealthReportError, match='ProbeReportReplay'):
        ingest(conn, report(sequence=3))  # Repeated enrollment cannot reset counters.
    health.register_probe(conn, 'node-a', 1, 'probe-node-a', 2)
    with pytest.raises(health.HealthReportError, match='ProbeServerVersionMismatch'):
        ingest(conn, report(sequence=4))
    assert health.select(conn, [row], now=NOW) is None
    ingest(conn, report(generation=2))
    conn.execute('UPDATE servers SET version=2 WHERE id=?', ('node-a',))
    assert health.health_summary(conn, 'node-a', 1, now=NOW)['status'] == 'configuration_changed'
    assert health.select(conn, [row], now=NOW) is None
    with pytest.raises(health.HealthReportError, match='ProbeServerVersionMismatch'):
        ingest(conn, report(sequence=2, generation=2))


def test_cross_node_probe_cannot_overwrite_another_node(conn):
    node(conn, 'a')
    node(conn, 'b')
    with pytest.raises(health.HealthReportError, match='ProbeNotRegistered'):
        health.ingest(conn, report('a', probe_id='probe-b'), 'probe-b', now=NOW)
    with pytest.raises(health.HealthReportError, match='ProbeIdentityAlreadyRegistered'):
        health.register_probe(conn, 'a', 1, 'probe-b', 2)


def test_strict_minimum_only_exact_ties_are_random(conn, monkeypatch):
    a, b, c = [node(conn, sid) for sid in ('a', 'b', 'c')]
    ingest(conn, report('a', tx_bps=20_000_001))
    ingest(conn, report('b', tx_bps=20_000_000))
    ingest(conn, report('c', tx_bps=20_000_000))
    groups = []
    monkeypatch.setattr(health.secrets, 'choice', lambda rows: groups.append(rows) or rows[-1])
    assert health.select(conn, [a, b, c], now=NOW) == c
    assert groups == [[b, c]]  # No tolerance: even a one-bit/s difference excludes a.
    assert health.select(conn, [a, b], now=NOW) == b
    assert len(groups) == 1  # A unique minimum does not make a random choice.


def test_normalized_capacity_cpu_and_pending_standby_slots(conn):
    a = node(conn, 'a', capacity=10, occupied=1, reserved=7)
    b = node(conn, 'b', capacity=100, occupied=20, reserved=5)
    ingest(conn, report('a', tx_bps=5_000_000, cpu_percent=5))
    ingest(conn, report('b', tx_bps=50_000_000, bandwidth_capacity_bps=1_000_000_000))
    assert health.select(conn, [a, b], now=NOW) == b
    ingest(conn, report('b', sequence=2, cpu_percent=90))
    assert health.select(conn, [a, b], now=NOW) == a
    a['reserved'] = 9
    assert health.select(conn, [a], now=NOW) is None
    b['accept_new'] = False
    assert health.select(conn, [b], now=NOW) is None


def test_unbounded_slots_still_require_available_network_and_cpu(conn):
    row = node(conn, capacity=0, occupied=1000)
    ingest(conn, report(cpu_percent=100))
    assert health.select(conn, [row], now=NOW) is None
    ingest(conn, report(sequence=2, tx_bps=100_000_000))
    assert health.select(conn, [row], now=NOW) is None
    ingest(conn, report(sequence=3))
    assert health.select(conn, [row], now=NOW) == row


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'wg_fleet_probe.py'
    spec = importlib.util.spec_from_file_location('wg_fleet_probe_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_refuses_host_route_even_if_https_could_succeed(probe, monkeypatch):
    monkeypatch.setattr(probe, '_binary', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(probe, '_run', lambda *args, **kwargs: '[{"dev":"eth0"}]')
    with pytest.raises(probe.ProbeFailure, match='route_mismatch'):
        probe.verify_route({'namespace': 'pushok-probe-a', 'interface': 'wgprobe'}, '9.9.9.9')


def test_probe_checks_only_its_pinned_synthetic_peer(probe, monkeypatch):
    monkeypatch.setattr(probe, '_binary', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(probe, '_run', lambda *args, **kwargs:
        '[{"flags":["UP"],"linkinfo":{"info_kind":"wireguard"}}]')
    monkeypatch.setattr(probe, '_in_namespace', lambda *args, **kwargs: 'wrong-peer')
    with pytest.raises(probe.ProbeFailure, match='peer_mismatch'):
        probe.verify_probe_interface({'namespace': 'pushok-probe-a', 'interface': 'wgprobe',
                                      'server_public_key': 'expected-peer'})


def test_sequence_persists_across_version_rebase_and_resets_only_identity(probe, tmp_path):
    config = {'state_file': str(tmp_path / 'state.json'), 'server_id': 'a',
              'server_version': 1, 'generation': 1, 'probe_id': 'probe-a'}
    assert probe.next_sequence(config) == 1
    config['server_version'] = 2
    assert probe.next_sequence(config) == 2
    config['generation'] = 2
    assert probe.next_sequence(config) == 1
    assert (tmp_path / 'state.json').stat().st_mode & 0o077 == 0


def test_assignment_cannot_silently_change_node_or_generation(probe, monkeypatch):
    config = {'server_id': 'a', 'server_version': 1, 'generation': 1, 'probe_id': 'probe-a'}
    monkeypatch.setattr(probe, '_authorized_request', lambda config: json.dumps({**config, 'server_version': 2}))
    probe.refresh_assignment(config)
    assert config['server_version'] == 2
    monkeypatch.setattr(probe, '_authorized_request', lambda config: json.dumps({**config, 'generation': 2}))
    with pytest.raises(probe.ProbeFailure, match='ProbeEnrollmentChanged'):
        probe.refresh_assignment(config)


def test_protected_config_refuses_group_readable_secret(probe, tmp_path):
    secret = tmp_path / 'token'
    secret.write_text('do-not-log-this-token')
    secret.chmod(0o644)
    with pytest.raises(probe.ProbeFailure, match='UnsafeLocalFile') as error:
        probe._protected_read(secret)
    assert 'do-not-log' not in str(error.value)
