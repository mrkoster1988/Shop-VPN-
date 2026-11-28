"""Cloudflare contract responses, durable ownership and guarded deletion."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3

import httpx
import pytest

from app import wg_fleet_dns as dns


ZONE = 'a' * 32
RECORD = 'b' * 32


@pytest.fixture
def storage(tmp_path, monkeypatch):
    filename = tmp_path / 'dns.sqlite'

    @contextmanager
    def connect():
        conn = sqlite3.connect(filename)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    with connect() as conn:
        conn.execute('''CREATE TABLE wg_fleet_nodes(serial INTEGER PRIMARY KEY,server_id TEXT UNIQUE,
            domain TEXT,endpoint_ip TEXT,dns_state TEXT,archived INTEGER,created INTEGER)''')
        conn.execute('CREATE TABLE wg_fleet_placements(server_id TEXT,state TEXT)')
        conn.execute('CREATE TABLE subscriptions(server_id TEXT,state TEXT)')
        conn.execute("INSERT INTO wg_fleet_nodes VALUES (1,'node-a','wg1.pushok.shop','1.1.1.1','pending',0,1)")
        dns.create_schema(conn)
    monkeypatch.setattr(dns, 'db', connect)
    monkeypatch.setenv('WG_FLEET_CLOUDFLARE_ZONE_ID', ZONE)
    monkeypatch.setenv('WG_FLEET_CLOUDFLARE_API_TOKEN', 'fixture-token-' + 'x' * 40)
    monkeypatch.delenv('WG_FLEET_DOMAIN', raising=False)
    return connect


class Cloudflare:
    def __init__(self, monkeypatch):
        self.records = []
        self.calls = []
        self.lose_create = False
        self.force_proxied = False
        self.after_write = None
        monkeypatch.setattr(dns, '_client', lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), follow_redirects=False, trust_env=False))

    def handle(self, request):
        self.calls.append(request.method)
        assert request.url.host == 'api.cloudflare.com'
        assert request.headers['authorization'].startswith('Bearer fixture-token-')
        assert request.url.path.startswith('/client/v4/zones/' + ZONE + '/dns_records')
        if request.method == 'GET':
            assert request.url.params['name'] == 'wg1.pushok.shop'
            return httpx.Response(200, json={'success': True, 'result': self.records,
                'result_info': {'total_count': len(self.records), 'total_pages': 1}})
        if request.method == 'POST':
            value = json.loads(request.content)
            assert value['proxied'] is False and value['ttl'] == 60
            self.records = [{**value, 'id': RECORD, 'proxied': self.force_proxied}]
            if self.lose_create:
                self.lose_create = False
                raise httpx.ReadTimeout('response lost, private response must not be logged', request=request)
            result = self.records[0]
        elif request.method == 'PUT':
            assert request.url.path.endswith('/' + RECORD)
            self.records = [{**json.loads(request.content), 'id': RECORD, 'proxied': self.force_proxied}]
            result = self.records[0]
        elif request.method == 'DELETE':
            assert request.url.path.endswith('/' + RECORD)
            self.records = []
            result = {'id': RECORD}
        else:
            raise AssertionError(request.method)
        if self.after_write:
            self.after_write()
        return httpx.Response(200, json={'success': True, 'result': result})


def state(storage):
    with storage() as conn:
        return dict(conn.execute('SELECT * FROM wg_fleet_nodes').fetchone())


def retry(storage):
    with storage() as conn:
        conn.execute('UPDATE wg_fleet_dns_records SET next_attempt=0,lease_until=0')


def test_create_owned_dns_only_record_and_exact_update(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    assert asyncio.run(dns.sync()) == 1
    assert state(storage)['dns_state'] == 'ready'
    assert remote.calls == ['GET', 'POST', 'GET']
    assert remote.records[0]['comment'].startswith('pushok-fleet:')
    with storage() as conn:
        saved = dict(conn.execute('SELECT * FROM wg_fleet_dns_records').fetchone())
        assert saved['record_id'] == RECORD and saved['zone_id'] == ZONE
        conn.execute("UPDATE wg_fleet_nodes SET endpoint_ip='2606:4700:4700::1111',dns_state='pending'")
    assert asyncio.run(dns.sync()) == 1
    assert remote.records[0]['type'] == 'AAAA'
    assert remote.records[0]['content'] == '2606:4700:4700::1111'
    assert remote.calls[-3:] == ['GET', 'PUT', 'GET']


def test_preexisting_matching_record_is_not_adopted_by_ip(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    remote.records = [{'id': RECORD, 'name': 'wg1.pushok.shop', 'content': '1.1.1.1',
                       'type': 'A', 'ttl': 60, 'proxied': False, 'comment': 'someone else'}]
    assert asyncio.run(dns.sync()) == 0
    assert state(storage)['dns_state'] == 'conflict'
    assert remote.calls == ['GET']
    assert remote.records[0]['comment'] == 'someone else'


def test_ambiguous_create_recovers_by_persisted_marker_without_duplicate(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    remote.lose_create = True
    assert asyncio.run(dns.sync()) == 0
    assert state(storage)['dns_state'] == 'error'
    with storage() as conn:
        saved = dict(conn.execute('SELECT * FROM wg_fleet_dns_records').fetchone())
        assert saved['record_id'] is None
        assert saved['last_error'] == 'CloudflareRequestFailed'
    retry(storage)
    assert asyncio.run(dns.sync()) == 1
    assert remote.calls.count('POST') == 1
    assert state(storage)['dns_state'] == 'ready'


def test_delete_requires_archive_and_confirmed_remote_peer_revocations(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    assert asyncio.run(dns.sync()) == 1
    with storage() as conn:
        conn.execute('UPDATE wg_fleet_nodes SET archived=1')
        conn.execute("INSERT INTO wg_fleet_placements VALUES ('node-a','revoking')")
    assert asyncio.run(dns.sync()) == 0
    assert 'DELETE' not in remote.calls
    with storage() as conn:
        conn.execute("UPDATE wg_fleet_placements SET state='revoked'")
    retry(storage)
    assert asyncio.run(dns.sync()) == 1
    assert remote.calls[-3:] == ['GET', 'DELETE', 'GET']
    assert state(storage)['dns_state'] == 'deleted'
    assert remote.records == []


def test_active_account_binding_also_blocks_dns_deletion(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    asyncio.run(dns.sync())
    with storage() as conn:
        conn.execute('UPDATE wg_fleet_nodes SET archived=1')
        conn.execute("INSERT INTO subscriptions VALUES ('node-a','active')")
    assert asyncio.run(dns.sync()) == 0
    assert 'DELETE' not in remote.calls


def test_changed_record_identity_or_owner_is_never_deleted(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    asyncio.run(dns.sync())
    remote.records[0]['id'] = 'c' * 32
    with storage() as conn:
        conn.execute('UPDATE wg_fleet_nodes SET archived=1')
    assert asyncio.run(dns.sync()) == 0
    assert state(storage)['dns_state'] == 'conflict'
    assert 'DELETE' not in remote.calls


def test_write_result_must_confirm_dns_only(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    remote.force_proxied = True
    assert asyncio.run(dns.sync()) == 0
    assert state(storage)['dns_state'] != 'ready'


def test_external_conflict_is_not_overwritten_or_deleted(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    asyncio.run(dns.sync())
    remote.records.append({**remote.records[0], 'id': 'c' * 32, 'comment': 'external-owner'})
    with storage() as conn:
        conn.execute("UPDATE wg_fleet_nodes SET dns_state='pending'")
    before = list(remote.calls)
    assert asyncio.run(dns.sync()) == 0
    assert remote.calls == before + ['GET']
    assert state(storage)['dns_state'] == 'conflict'


def test_pending_address_change_during_http_never_marks_old_address_ready(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)

    def change():
        with storage() as conn:
            conn.execute("UPDATE wg_fleet_nodes SET endpoint_ip='8.8.8.8',dns_state='pending'")

    remote.after_write = change
    assert asyncio.run(dns.sync()) == 0
    assert state(storage)['endpoint_ip'] == '8.8.8.8'
    assert state(storage)['dns_state'] == 'pending'
    remote.after_write = None
    retry(storage)
    assert asyncio.run(dns.sync()) == 1
    assert remote.records[0]['content'] == '8.8.8.8'


def test_out_of_scope_name_and_zone_change_do_not_make_provider_writes(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    with storage() as conn:
        conn.execute("UPDATE wg_fleet_nodes SET domain='auth.pushok.shop'")
    assert asyncio.run(dns.sync()) == 0
    assert remote.calls == []
    assert state(storage)['dns_state'] == 'conflict'


def test_changed_zone_preserves_existing_owned_record(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    assert asyncio.run(dns.sync()) == 1
    monkeypatch.setenv('WG_FLEET_CLOUDFLARE_ZONE_ID', 'c' * 32)
    with storage() as conn:
        conn.execute("UPDATE wg_fleet_nodes SET dns_state='pending'")
    before = list(remote.calls)
    assert asyncio.run(dns.sync()) == 0
    assert remote.calls == before
    assert state(storage)['dns_state'] == 'conflict'
    assert len(remote.records) == 1


def test_ambiguous_recreation_resets_record_id_intent_before_post(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    assert asyncio.run(dns.sync()) == 1
    remote.records = []
    remote.lose_create = True
    with storage() as conn:
        conn.execute("UPDATE wg_fleet_nodes SET dns_state='pending'")
    assert asyncio.run(dns.sync()) == 0
    # Simulate the recreated provider record receiving a different ID.
    remote.records[0]['id'] = 'c' * 32
    retry(storage)
    assert asyncio.run(dns.sync()) == 1
    assert remote.calls.count('POST') == 2  # One original, one recreation, no third copy.
    with storage() as conn:
        assert conn.execute('SELECT record_id FROM wg_fleet_dns_records').fetchone()[0] == 'c' * 32


def test_no_credentials_does_not_attempt_external_requests(storage, monkeypatch):
    remote = Cloudflare(monkeypatch)
    monkeypatch.delenv('WG_FLEET_CLOUDFLARE_API_TOKEN')
    monkeypatch.delenv('WG_FLEET_CLOUDFLARE_ZONE_ID')
    assert asyncio.run(dns.sync()) == 0
    assert remote.calls == []
    assert dns.configuration_status()['configured'] is False


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
    '100.64.0.1', '224.0.0.1', '192.0.2.1', '::', '::1', 'fe80::1',
    'fec0::1', 'fc00::1', '::ffff:1.1.1.1', '1.1.1.1:51820', 'https://1.1.1.1', ' 1.1.1.1'])
def test_only_public_unambiguous_endpoint_ips(address):
    with pytest.raises(dns.DNSFailure):
        dns.validate_endpoint_ip(address)


def test_canonical_public_address():
    assert dns.validate_endpoint_ip('193.238.155.39') == '193.238.155.39'
    assert dns.validate_endpoint_ip('2606:4700:4700:0:0:0:0:1111') == '2606:4700:4700::1111'
