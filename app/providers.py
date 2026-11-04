"""External side effects are deterministic: subscription UUID is also the provider id."""
import json
import os
import httpx
from . import config as c, servers


def available(protocol, user_id=None):
    return servers.available(protocol, user_id)

async def xui_inbound(client, profile):
    base, inbound_id = profile['url'], profile['inbound_id']
    r = await client.post(base + '/login', data={'username':profile['username'], 'password':profile['password']})
    r.raise_for_status()
    if not r.json().get('success'):
        raise RuntimeError('3x-ui login failed')
    r = await client.get(base + f'/panel/api/inbounds/get/{inbound_id}')
    r.raise_for_status()
    result = r.json()
    if not result.get('success') or result.get('obj',{}).get('protocol') != 'vless':
        raise RuntimeError('3x-ui VLESS inbound unavailable')
    return result['obj']

async def probe_server(row):
    profile = servers.decode(row)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        if row['protocol']=='xui':
            await xui_inbound(client, profile)
        else:
            r = await client.get(profile['url']+'/health', headers={'Authorization':'Bearer '+profile['token']})
            r.raise_for_status()
            if r.json().get('status') != 'ok':
                raise RuntimeError('WireGuard agent unavailable')


async def xui_client(sub, enabled=True):
    _, profile = servers.for_subscription(sub, revoking=not enabled)
    base, inbound_id = profile['url'], profile['inbound_id']
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        inbound = await xui_inbound(client, profile)
        settings = inbound['settings']
        settings = json.loads(settings) if isinstance(settings, str) else settings
        clients = settings.get('clients', [])
        existing = next((x for x in clients if x.get('id') == sub['id']), None)
        if not enabled and not existing:
            return ''
        payload = dict(existing) if existing else {
            'id': sub['id'], 'email': 'pushok_' + sub['id'], 'flow': profile['flow'],
            'limitIp': 0, 'totalGB': 0, 'subId': sub['id'].replace('-', '')}
        payload.update(enable=enabled, expiryTime=sub['expires'] * 1000)
        endpoint = f"updateClient/{sub['id']}" if existing else 'addClient'
        r = await client.post(base + '/panel/api/inbounds/' + endpoint,
                              data={'id': inbound_id, 'settings': json.dumps({'clients': [payload]})})
        r.raise_for_status()
        if not r.json().get('success'):
            raise RuntimeError('3x-ui client operation failed')
    return profile['link_template'].replace('{uuid}', sub['id'])


async def provision(sub):
    if sub['protocol'] == 'xui':
        return await xui_client(sub)
    _, profile = servers.for_subscription(sub)
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        r = await client.put(profile['url'] + '/peers/' + sub['id'],
                             headers={'Authorization': 'Bearer ' + profile['token']},
                             json={'expires': sub['expires']})
        r.raise_for_status()
        return r.json()['config']


async def disable(sub):
    if sub['protocol'] == 'xui':
        await xui_client(sub, enabled=False)
    else:
        _, profile = servers.for_subscription(sub, revoking=True)
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            r = await client.delete(profile['url'] + '/peers/' + sub['id'],
                                    headers={'Authorization': 'Bearer ' + profile['token']})
            r.raise_for_status()


async def payment(order, email=None):
    async with httpx.AsyncClient(timeout=25,
                                 auth=(os.environ['YOOKASSA_SHOP_ID'], os.environ['YOOKASSA_SECRET'])) as client:
        data = {'amount': {'value': f"{order['amount']}.00", 'currency': 'RUB'},
                'capture': True, 'confirmation': {'type': 'redirect', 'return_url': c.PUBLIC_URL + '/?payment=return'},
                'description': 'Pushok VPN / ' + order['id'], 'metadata': {'order_id': order['id']}}
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
