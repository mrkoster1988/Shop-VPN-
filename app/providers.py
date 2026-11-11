"""External side effects are deterministic: subscription UUID is also the provider id."""
import json
import os
import httpx
from . import config as c, servers, panel, wgdashboard


def _require_vpn_protocol(protocol):
    """Reject channels and unknown drivers before any network calls."""
    if protocol not in ('xui', 'wg'):
        raise ValueError('UnsupportedVpnProtocol')
    return protocol


def available(protocol, user_id=None):
    return servers.available(protocol, user_id)

async def probe_server(row):
    _require_vpn_protocol(row['protocol'])
    profile = servers.decode(row)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
        if row['protocol']=='xui':
            await panel.adapter(profile).check(client)
        elif profile.get('api') == 'wgdashboard':
            await wgdashboard.WGDashboard(profile).check(client)
        else:
            r = await client.get(profile['url']+'/health', headers={'Authorization':'Bearer '+profile['token']})
            r.raise_for_status()
            if r.json().get('status') != 'ok':
                raise RuntimeError('WireGuard agent unavailable')


async def xui_client(sub, enabled=True):
    _, profile = servers.for_subscription(sub, revoking=not enabled)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
        return await panel.adapter(profile).apply(client, sub, enabled)


async def provision(sub):
    _require_vpn_protocol(sub['protocol'])
    if sub['protocol'] == 'xui':
        return await xui_client(sub)
    row, profile = servers.for_subscription(sub)
    if profile.get('api') == 'wgdashboard':
        private_key = wgdashboard.stored_private_key(row['id'], sub['id'])
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
            adapter = wgdashboard.WGDashboard(profile)
            await adapter.ensure_peer(client, private_key, 'pushok_' + sub['id'])
            return await adapter.download_config(client, private_key)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
        r = await client.put(profile['url'] + '/peers/' + sub['id'],
                             headers={'Authorization': 'Bearer ' + profile['token']},
                             json={'expires': sub['expires']})
        r.raise_for_status()
        return r.json()['config']


async def disable(sub):
    _require_vpn_protocol(sub['protocol'])
    if sub['protocol'] == 'xui':
        await xui_client(sub, enabled=False)
    else:
        row, profile = servers.for_subscription(sub, revoking=True)
        if profile.get('api') == 'wgdashboard':
            private_key = wgdashboard.stored_private_key(row['id'], sub['id'], create=False)
            async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
                adapter = wgdashboard.WGDashboard(profile)
                await adapter.revoke_peer(client, adapter.identity(private_key))
            return
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
            r = await client.delete(profile['url'] + '/peers/' + sub['id'],
                                    headers={'Authorization': 'Bearer ' + profile['token']})
            r.raise_for_status()


async def payment(order, email=None):
    # Legacy direct callers without method use ordinary YooKassa checkout.
    # An explicit unsupported method still fails closed.
    method = order.get('method', 'yookassa')
    if method not in ('yookassa', 'sbp'):
        raise ValueError('UnsupportedPaymentMethod')
    async with httpx.AsyncClient(timeout=25,
                                 auth=(os.environ['YOOKASSA_SHOP_ID'], os.environ['YOOKASSA_SECRET'])) as client:
        data = {'amount': {'value': f"{order['amount']}.00", 'currency': 'RUB'},
                'capture': True, 'confirmation': {'type': 'redirect', 'return_url': c.PUBLIC_URL + '/?payment=return'},
                'description': 'Pushok VPN / ' + order['id'], 'metadata': {'order_id': order['id']}}
        if method == 'sbp':
            data['payment_method_data'] = {'type': 'sbp'}
        # Receipt parameters depend on merchant taxation; configured explicitly.
        if os.getenv('YOOKASSA_RECEIPTS') == 'true':
            if not email:
                raise RuntimeError('Receipt email required')
            data['receipt'] = {'customer': {'email': email}, 'items': [{
                'description': 'Доступ Pushok VPN', 'quantity': '1.00', 'amount': data['amount'],
                'vat_code': int(os.getenv('YOOKASSA_VAT_CODE', '1')),
                'payment_mode': 'full_payment', 'payment_subject': 'service'}]}
        r = await client.post('https://api.yookassa.ru/v3/payments', json=data,
                              headers={'Idempotence-Key': order['id']})
        r.raise_for_status()
        return r.json()


async def get_payment(payment_id):
    async with httpx.AsyncClient(timeout=20,
                                 auth=(os.environ['YOOKASSA_SHOP_ID'], os.environ['YOOKASSA_SECRET'])) as client:
        r = await client.get('https://api.yookassa.ru/v3/payments/' + payment_id)
        r.raise_for_status()
        return r.json()


async def stars_invoice(order):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post('https://api.telegram.org/bot' + c.BOT_TOKEN + '/createInvoiceLink', json={
            'title': 'Pushok VPN', 'description': f"Доступ на {order['days']} дней / {order['protocol']}",
            'payload': order['id'], 'provider_token': '', 'currency': 'XTR',
            'prices': [{'label': 'Подписка VPN', 'amount': order['amount']}]})
        r.raise_for_status()
        result = r.json()
        if not result.get('ok'):
            raise RuntimeError('Telegram invoice creation failed')
        return result['result']
