"""Границы справочника, продаж и выдачи доступа: запланированное не продаётся."""
import asyncio
import json
import time

import pytest

from app import config as c, protocols, providers, servers
from app.db import db, one
from app.security import digest
from test_shop import client, register


PLANNED = ('trojan', 'shadowsocks', 'tuic', 'anytls',
           'amneziawg', 'openvpn', 'ikev2-ipsec')
OWNER = {'Authorization': 'Bearer ' + 'a' * 48}


def test_catalog_is_read_only_and_never_reads_server_secrets(client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Справочник не должен читать профиль или вызывать провайдера')
    monkeypatch.setattr(servers, 'decode', forbidden)
    monkeypatch.setattr(providers.httpx, 'AsyncClient', forbidden)
    response = client.get('/api/admin/protocols', headers=OWNER)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()
    assert data['external_checks_performed'] is False
    rows = {p['id']: p for p in data['protocols']}
    assert set(rows) == {'vless', 'wireguard', 'vmess', 'hysteria2', *PLANNED}
    assert rows['vless']['driver_id'] == 'xui'
    assert rows['wireguard']['driver_id'] == 'wg'
    for name in PLANNED:
        assert rows[name]['driver_id'] is None
        assert rows[name]['implementation'] == 'planned'
        assert not rows[name]['adapters']
    assert 'panel.example' not in response.text and 'private' not in response.text
    assert client.post('/api/admin/protocols', headers=OWNER, json={}).status_code == 405


@pytest.mark.parametrize('role,expected', [('owner', 200), ('panel_operator', 200),
                                         ('support', 403), ('finance', 403), ('mtproxy_manager', 403)])
def test_catalog_staff_roles(client, role, expected):
    assert client.get('/api/admin/protocols').status_code == 401
    now = int(time.time())
    with db() as conn:
        sid = conn.execute('INSERT INTO staff_users(email,password,role,created,updated) VALUES (?,?,?,?,?)',
                           ('staff@example.com', 'unused', role, now, now)).lastrowid
        conn.execute('INSERT INTO staff_sessions(token_hash,staff_id,created,expires) VALUES (?,?,?,?)',
                     (digest('protocol-test-session'), sid, now, now + 600))
    client.cookies.set('staff_session', 'protocol-test-session')
    assert client.get('/api/admin/protocols').status_code == expected


@pytest.mark.parametrize('protocol', PLANNED)
def test_planned_protocol_cannot_create_order_trial_server_or_call_provider(client, monkeypatch, protocol):
    register(client)
    before = one('SELECT count(*) AS n FROM servers')['n']
    for path, payload, headers in (
        ('/api/orders', {'plan':'month','protocol':protocol}, {}),
        ('/api/trial', {'protocol':protocol}, {}),
        ('/api/admin/servers', {'name':'Planned','protocol':protocol,'settings':{}}, OWNER),
    ):
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 422, response.text
    assert one('SELECT count(*) AS n FROM orders')['n'] == 0
    assert one('SELECT count(*) AS n FROM servers')['n'] == before

    def forbidden(*args, **kwargs):
        raise AssertionError('Неподдерживаемый протокол попал в выдачу')
    monkeypatch.setattr(providers.httpx, 'AsyncClient', forbidden)
    monkeypatch.setattr(servers, 'for_subscription', forbidden)
    monkeypatch.setattr(servers, 'decode', forbidden)
    for action in (providers.provision, providers.disable, providers.probe_server):
        with pytest.raises(ValueError, match='UnsupportedVpnProtocol'):
            asyncio.run(action({'protocol':protocol,'id':'test','expires':2000000000}))


def test_sales_keep_driver_ids_and_check_availability_per_customer(client, monkeypatch):
    register(client)
    calls = []
    def available(protocol, user_id=None):
        calls.append((protocol,user_id))
        return protocol == 'wg'
    monkeypatch.setattr(providers, 'available', available)
    data = client.get('/api/catalog').json()['protocols']
    assert data == [{'id':'xui','name':'VLESS','available':False},
                    {'id':'wg','name':'WireGuard','available':True},
                    {'id':'vmess','name':'VMess','available':False},
                    {'id':'hysteria2','name':'Hysteria2','available':False}]
    assert calls == [('xui',1),('wg',1),('vmess',1),('hysteria2',1)]
    # Экземпляры ответа не должны менять реестр и будущий каталог продаж.
    catalog = protocols.catalog()
    catalog['protocols'][0]['driver_id'] = 'vmess'
    assert protocols.supported_drivers() == ('xui','wg','vmess','hysteria2')
