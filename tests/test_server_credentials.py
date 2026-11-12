"""Замена доступа к панели сохраняет профиль и требует свежей проверки API."""
import json
import time

import pytest

from app import config as c, providers, servers
from app.db import one, execute
from app.main import ServerCredentials
from app.security import digest
from test_shop import (client, registry_client, server_body, server_create, server_enable,
                       server_patch, admin_headers, add_customer)


def rotate(client, row, **changes):
    return client.patch('/api/admin/servers/' + row['id'] + '/credentials',
        json={'version': row['version'], **changes}, headers=admin_headers())


TOKEN = 'new-private-api-token-' + 'z' * 32
BASE = {'url': 'https://panel.example/private', 'token': 'old-private-token-' + 'a' * 32}
PROFILES = [
    ('xui', server_body()['settings']),
    ('xui', {**BASE, 'api': 'clients', 'inbound_id': 7, 'link_template': 'vless://{uuid}@vpn.example'}),
    ('xui', {**BASE, 'api': 'remnawave', 'squad_uuids': ['11111111-1111-1111-1111-111111111111']}),
    ('wg', BASE),
    ('wg', {**BASE, 'api': 'wgdashboard', 'configuration': 'wg0', 'schedule_expiry': True, 'panel_timezone': 'Europe/Moscow'}),
    ('vmess', {**BASE, 'api': 'clients', 'inbound_id': 7, 'address': 'vpn.example', 'port': 443, 'sni': 'vpn.example', 'network': 'ws', 'path': '/vpn', 'ws_host': 'vpn.example'}),
    ('hysteria2', {**BASE, 'api': 'sui', 'inbound_id': 7, 'address': 'vpn.example', 'port': 443, 'sni': 'vpn.example', 'obfs_password': 'keep-salamander-password'}),
    ('tuic', {**BASE, 'api': 'sui', 'inbound_id': 7, 'address': 'vpn.example', 'port': 443, 'sni': 'vpn.example', 'congestion_control': 'bbr', 'udp_relay_mode': 'quic'}),
]


@pytest.mark.parametrize('protocol,profile', PROFILES)
def test_rotation_keeps_endpoint_and_binding_and_requires_recheck(registry_client, monkeypatch, protocol, profile):
    client = registry_client
    row = server_enable(client, server_create(client, protocol, settings=profile).json(), monkeypatch)
    uid = add_customer()
    execute('INSERT INTO subscriptions(id,user_id,protocol,state,expires,server_id) VALUES (?,?,?,?,?,?)',
        ('bound', uid, protocol, 'active', 2000000000, row['id']))
    before = servers.decode(one('SELECT * FROM servers WHERE id=?', (row['id'],)))
    changes = {'password': 'new-private-password'} if row['profile'] == 'legacy_vless' else {'token': TOKEN}
    result = rotate(client, row, **changes)
    assert result.status_code == 200, result.text
    updated = result.json()
    assert updated['version'] == row['version'] + 1
    assert (updated['state'], updated['health'], updated['checked_at'], updated['error_code']) == ('disabled', 'unknown', None, None)
    raw = one('SELECT * FROM servers WHERE id=?', (row['id'],))
    assert servers.decode(raw) == before | changes
    assert all(secret not in raw['settings'] + result.text for secret in changes.values())
    assert one("SELECT server_id,state FROM subscriptions WHERE id='bound'") == {'server_id': row['id'], 'state': 'active'}
    assert one('SELECT action FROM server_changes ORDER BY id DESC')['action'] == 'credentials_updated'
    assert not providers.available(protocol, uid)
    assert server_patch(client, updated, state='active').status_code == 409
    assert server_enable(client, updated, monkeypatch)['state'] == 'active'


def test_legacy_can_change_login_only_and_stale_write_is_rejected(registry_client):
    row = server_create(registry_client).json()
    result = rotate(registry_client, row, username='new-panel-owner')
    assert result.status_code == 200
    assert servers.decode(one('SELECT * FROM servers'))['password'] == server_body()['settings']['password']
    stale = rotate(registry_client, row, password='stale-private-password')
    assert stale.status_code == 409 and 'stale-private' not in stale.text
    assert servers.decode(one('SELECT * FROM servers'))['username'] == 'new-panel-owner'


@pytest.mark.parametrize('changes', [{}, {'token': TOKEN}, {'password': None}, {'password': ''},
    {'username': None}, {'password': 'new', 'url': 'https://other.example'},
    {'state': 'active'}, {'version': True, 'password': 'new'},
    {'password': 'new', 'settings': {'inbound_id': 99}}])
def test_invalid_legacy_rotation_is_atomic_and_redacted(registry_client, changes):
    row = server_create(registry_client).json()
    before = one('SELECT * FROM servers')
    result = rotate(registry_client, row, **changes)
    assert result.status_code == 422
    assert TOKEN not in result.text and 'other.example' not in result.text
    assert one('SELECT * FROM servers') == before


@pytest.mark.parametrize('changes', [{'password': 'new'}, {'username': 'owner', 'token': TOKEN},
    {'token': None}, {'token': 'too-short'}, {'token': TOKEN + '\n'}])
def test_token_rotation_accepts_only_valid_token(registry_client, changes):
    row = server_create(registry_client, 'wg').json()
    before = one('SELECT * FROM servers')
    assert rotate(registry_client, row, **changes).status_code == 422
    assert one('SELECT * FROM servers') == before


def test_inflight_old_probe_cannot_mark_new_credentials_healthy(registry_client, monkeypatch):
    row = server_create(registry_client).json()
    async def old_probe(_):
        servers.update_credentials(row['id'], ServerCredentials(version=row['version'], password='new-private-password'))
    monkeypatch.setattr(providers, 'probe_server', old_probe)
    result = registry_client.post('/api/admin/servers/' + row['id'] + '/check', headers=admin_headers())
    assert result.status_code == 409
    assert one('SELECT health,state FROM servers') == {'health': 'unknown', 'state': 'disabled'}


def test_rotation_requires_login_and_same_origin(registry_client):
    row = server_create(registry_client).json()
    url = '/api/admin/servers/' + row['id'] + '/credentials'
    body = {'version': row['version'], 'password': 'private-new-password'}
    assert registry_client.patch(url, json=body).status_code == 401
    assert registry_client.patch(url, json=body, headers=admin_headers() | {'Origin':'https://other.example'}).status_code == 403
    assert rotate(registry_client, {'id':'missing', 'version':1}, password='private').status_code == 404


@pytest.mark.parametrize('role,status', [('owner',200), ('panel_operator',200), ('finance',403), ('support',403), ('mtproxy_manager',403)])
def test_rotation_obeys_staff_permissions_and_audit_redacts(registry_client, role, status):
    row = server_create(registry_client).json()
    execute('INSERT INTO staff_users(email,password,role,enabled,created,updated) VALUES (?,?,?,?,?,?)',
        ('staff@example.com', 'unused', role, 1, 0, 0))
    uid = one('SELECT id FROM staff_users')['id']
    execute('INSERT INTO staff_sessions(token_hash,staff_id,expires,created) VALUES (?,?,?,?)',
        (digest('fixture-session'), uid, int(time.time()) + 3600, 0))
    registry_client.cookies.set('staff_session', 'fixture-session')
    result = rotate(registry_client, row, password='private-new-password')
    assert result.status_code == status, result.text
    if status == 200:
        assert result.headers['cache-control'] == 'no-store'
        # Аудит пишет маршрут и результат, но не тело запроса с паролем.
        with c.DB.open('rb') as file:
            assert b'private-new-password' not in file.read()
