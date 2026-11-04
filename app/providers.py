"""External side effects are deterministic: subscription UUID is also the provider id."""
import json
import os
import httpx
from . import config as c


def available(protocol):
    if protocol == 'xui':
        return all(os.getenv(k) for k in ('XUI_URL', 'XUI_USERNAME', 'XUI_PASSWORD', 'XUI_INBOUND_ID', 'XUI_LINK_TEMPLATE'))
    return all(os.getenv(k) for k in ('WG_AGENT_URL', 'WG_AGENT_TOKEN'))


async def xui_client(sub, enabled=True):
    base = os.environ['XUI_URL'].rstrip('/')
    inbound_id = int(os.environ['XUI_INBOUND_ID'])
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(base + '/login', data={
            'username': os.environ['XUI_USERNAME'], 'password': os.environ['XUI_PASSWORD']})
        r.raise_for_status()
        if not r.json().get('success'):
            raise RuntimeError('3x-ui login failed')
        r = await client.get(base + f'/panel/api/inbounds/get/{inbound_id}')
        r.raise_for_status()
        result = r.json()
        if not result.get('success'):
            raise RuntimeError('3x-ui inbound unavailable')
        inbound = result['obj']
        if inbound.get('protocol') != 'vless':
            raise RuntimeError('Only a VLESS inbound is supported')
        settings = inbound['settings']
        settings = json.loads(settings) if isinstance(settings, str) else settings
        clients = settings.get('clients', [])
        existing = next((x for x in clients if x.get('id') == sub['id']), None)
        if not enabled and not existing:
            return ''
        payload = dict(existing) if existing else {
            'id': sub['id'], 'email': 'pushok_' + sub['id'], 'flow': os.getenv('XUI_FLOW', ''),
            'limitIp': 0, 'totalGB': 0, 'subId': sub['id'].replace('-', '')}
        payload.update(enable=enabled, expiryTime=sub['expires'] * 1000)
        endpoint = f"updateClient/{sub['id']}" if existing else 'addClient'
        r = await client.post(base + '/panel/api/inbounds/' + endpoint,
                              data={'id': inbound_id, 'settings': json.dumps({'clients': [payload]})})
        r.raise_for_status()
        if not r.json().get('success'):
            raise RuntimeError('3x-ui client operation failed')
    return os.environ['XUI_LINK_TEMPLATE'].replace('{uuid}', sub['id'])


async def provision(sub):
    if not available(sub['protocol']):
        raise RuntimeError('Provider not configured')
    if sub['protocol'] == 'xui':
        return await xui_client(sub)
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.put(os.environ['WG_AGENT_URL'].rstrip('/') + '/peers/' + sub['id'],
                             headers={'Authorization': 'Bearer ' + os.environ['WG_AGENT_TOKEN']},
                             json={'expires': sub['expires']})
        r.raise_for_status()
        return r.json()['config']


async def disable(sub):
    if sub['protocol'] == 'xui':
        await xui_client(sub, enabled=False)
    else:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.delete(os.environ['WG_AGENT_URL'].rstrip('/') + '/peers/' + sub['id'],
                                    headers={'Authorization': 'Bearer ' + os.environ['WG_AGENT_TOKEN']})
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
