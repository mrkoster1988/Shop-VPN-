"""Check existing Shop exports and optionally fix their WGDashboard interface endpoint."""
import argparse
import asyncio
import configparser
import ipaddress
import socket
import time
from urllib.parse import quote, urlsplit

import httpx
from app import servers, service, wgdashboard
from app.db import all_rows, db, one


class CheckFailed(Exception):
    pass


def export_endpoint(config):
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read_string(config)
    return parsed['Peer']['Endpoint']


async def run(args):
    targets = []
    node_id = None
    if set(socket.gethostbyname_ex(args.host)[2]) != {str(args.ip)}:
        raise CheckFailed('DNS контейнера отличается от указанного IPv4')
    for uid in dict.fromkeys(args.user):
        sub = one("SELECT * FROM subscriptions WHERE user_id=? AND protocol='wg'", (uid,))
        if not sub:
            raise CheckFailed(f'Нет подписки WireGuard у пользователя {uid}')
        node, profile = servers.for_subscription(sub)
        url = urlsplit(profile['url'])
        if (profile.get('api') != 'wgdashboard' or url.scheme != 'https'
                or url.hostname != args.host or profile['configuration'] != args.interface):
            raise CheckFailed(f'Пользователь {uid} закреплён за другой панелью или интерфейсом')
        if node_id and node_id != node['id']:
            raise CheckFailed('Пользователи закреплены за разными узлами')
        node_id = node['id']
        key = wgdashboard.stored_private_key(node_id, service.primary_identity(sub), create=False)
        targets.append((uid, sub['id'], key))

    endpoint = f'{args.host}:{args.port}'
    adapter = wgdashboard.WGDashboard(profile)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        await adapter.check(client)
        info = await adapter.request(client, 'GET', 'getWireguardConfigurationInfo',
                                     params={'configurationName': args.interface})
        interface = info['configurationInfo']
        if (interface['Name'] != args.interface or interface['Protocol'] != 'wg'
                or int(interface['ListenPort']) != args.port):
            raise CheckFailed('Настройки интерфейса или порт не совпали')
        settings = interface['Info']['OverridePeerSettings']
        repair_needed = False
        for uid, _, key in targets:
            public = adapter.identity(key)
            peer = await adapter.peer(client, public)
            if not peer or peer[0] != 'configurationPeers':
                raise CheckFailed(f'Для пользователя {uid} нет активного пира на выбранном интерфейсе')
            data = await adapter.request(client, 'GET', 'downloadPeer/' + quote(args.interface, safe=''),
                                         params={'id': public})
            matches = export_endpoint(data['file']) == endpoint
            if matches:
                await adapter.download_config(client, key)
            print(f'Пользователь {uid}:', 'Endpoint OK' if matches else 'требуется проверка экспорта', flush=True)
            repair_needed |= not matches
        if not args.apply:
            return
        if repair_needed:
            # Only these two fields change. Preserve all other interface overrides.
            updated = {**settings, 'PeerRemoteEndpoint': args.host, 'ListenPort': str(args.port)}
            if settings != updated:
                await adapter.request(client, 'POST', 'updateWireguardConfigurationInfo',
                                      json={'Name': args.interface, 'Key':'OverridePeerSettings', 'Value': updated})
        for uid, _, key in targets:
            if export_endpoint(await adapter.download_config(client, key)) != endpoint:
                raise CheckFailed(f'WGDashboard ещё не подтвердил Endpoint пользователя {uid}')
        print(f'Экспорт для всех выбранных пользователей проверен: {endpoint}', flush=True)

    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for uid, sid, _ in targets:
            pinned = conn.execute('SELECT 1 FROM subscriptions WHERE user_id=? AND id=? AND server_id=?',
                                  (uid, sid, node_id)).fetchone()
            if not pinned:
                raise CheckFailed('За время проверки изменилась привязка подписки')
            conn.execute("UPDATE flow_auto_grants SET next_attempt=0 WHERE subscription_id=? AND state='pending'", (sid,))
            conn.execute("UPDATE orders SET next_attempt=0 WHERE user_id=? AND protocol='wg' AND status='paid'", (uid,))
    # Let the running worker serialize normal provisioning and paid renewals.
    deadline = time.monotonic() + 90
    while True:
        waiting = False
        result = []
        for uid, _, _ in targets:
            sub = one("SELECT state FROM subscriptions WHERE user_id=? AND protocol='wg'", (uid,))
            orders = all_rows("SELECT status,attempts FROM orders WHERE user_id=? AND protocol='wg' ORDER BY created,id", (uid,))
            waiting |= sub['state'] == 'pending' or any(o['status'] == 'paid' for o in orders)
            result.append((uid, sub['state'], [(o['status'], o['attempts']) for o in orders]))
        if not waiting or time.monotonic() >= deadline:
            for uid, state, statuses in result:
                print(f'Пользователь {uid}: подписка {state}, заказы (статус, попытки) {statuses}')
            return
        await asyncio.sleep(3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', type=int, action='append', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--ip', type=ipaddress.IPv4Address, required=True)
    parser.add_argument('--interface', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except CheckFailed as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        reason = str(exc) if isinstance(exc, wgdashboard.PanelError) and str(exc) in wgdashboard.ORDER_ERROR_CODES else type(exc).__name__
        raise SystemExit('Проверка завершилась ошибкой: '+reason) from None
