"""One-use Platega payment links for confirmed Shop orders."""
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException, Request, Response

from . import config as c
from .db import execute

API = 'https://api.payplatform.cc'
MIN_RUB = 1
MAX_RUB = 10_000
LINK_LIFETIME = timedelta(hours=24)


class VerificationError(RuntimeError):
    pass


def credentials():
    merchant = os.getenv('PLATEGA_MERCHANT_ID', '').strip()
    secret = os.getenv('PLATEGA_SECRET', '').strip()
    try:
        UUID(merchant)
        valid_secret = bool(secret) and all(33 <= ord(ch) < 127 for ch in secret)
    except ValueError:
        return None
    if not valid_secret:
        return None
    return {'X-MerchantId': merchant, 'X-Secret': secret}


def configured():
    return bool(c.SECURE_COOKIE and credentials())


def request_headers():
    headers = credentials()
    if headers is None:
        raise RuntimeError('Platega credentials unavailable')
    return headers


def verified(condition):
    if not condition:
        raise VerificationError('Platega payment verification mismatch')


def provider_id(value):
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise VerificationError('Invalid Platega identifier') from None


def checkout_url(value):
    try:
        parts = urlsplit(value) if isinstance(value, str) else None
        verified(parts and parts.scheme == 'https' and parts.hostname and parts.username is None
                 and parts.password is None and not any(ch.isspace() or ch == '\\' for ch in value))
    except ValueError:
        raise VerificationError('Invalid Platega checkout URL') from None
    return value


async def request(client, method, path, headers, **kwargs):
    response = await client.request(method, API + path, headers=headers, **kwargs)
    response.raise_for_status()
    data = json.loads(response.content, parse_float=Decimal)
    verified(isinstance(data, dict))
    return data


def expires_at(order):
    return datetime.fromtimestamp(order['created'], timezone.utc) + LINK_LIFETIME


async def create(order):
    order_id = order['id']
    verified(order['currency'] == 'RUB' and MIN_RUB <= order['amount'] <= MAX_RUB)
    amount = f"{order['amount']}.00"
    expiry = expires_at(order)
    return_url = c.PUBLIC_URL.rstrip('/') + '/?payment=return'
    body = {
        'usageMode': 1,
        'paymentMode': 0,
        'currency': 'RUB',
        'minAmount': amount,
        'maxAmount': amount,
        'orderId': order_id,
        'payload': order_id,
        'expiresAt': expiry.isoformat(),
        'description': 'Подписка Pushok VPN',
        'return': return_url,
        'failedUrl': return_url,
    }
    headers = request_headers() | {'Idempotency-Key': order_id}
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        data = await request(client, 'POST', '/v2/payment-links', headers, json=body)
    return provider_id(data.get('id')), checkout_url(data.get('url'))


async def status(order):
    order_id = order['id']
    link_id = provider_id(order['payment_id'])
    headers = request_headers()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        link = await request(client, 'GET', '/v2/payment-links/' + link_id, headers)
        verified(link.get('id') == link_id and link.get('orderId') == order_id
                 and type(link.get('usageMode')) is int and link['usageMode'] == 1)
        transaction = link.get('consumedTransactionId')
        if transaction is None:
            verified(type(link.get('active')) is bool and not link.get('consumedAt')
                     and link.get('transactionsCount', 0) == 0)
            if not link['active'] and expires_at(order) <= datetime.now(timezone.utc):
                return 'canceled'
            return 'pending'
        transaction_id = provider_id(transaction)
        data = await request(client, 'GET', '/transaction/' + transaction_id, headers)
    details = data.get('paymentDetails')
    verified(data.get('id') == transaction_id and data.get('payload') == order_id
             and isinstance(details, dict) and details.get('currency') == order['currency'] == 'RUB')
    try:
        amount = Decimal(str(details.get('amount')))
        verified(amount.is_finite() and amount == Decimal(order['amount']))
    except InvalidOperation:
        raise VerificationError('Invalid Platega amount') from None
    state = data.get('status')
    if state == 'CONFIRMED':
        return 'paid'
    if state == 'CANCELED':
        return 'canceled'
    verified(state in ('PENDING', 'PARTIALLY_REFUNDED', 'DISPUTED', 'CHARGEBACKED'))
    return 'pending'


async def receive(request: Request):
    headers = credentials()
    if headers is None:
        raise HTTPException(503, 'Приём уведомлений Platega не настроен')
    if not all(hmac.compare_digest(request.headers.get(name, '').encode(), value.encode())
               for name, value in headers.items()):
        raise HTTPException(403, 'Неверные реквизиты отправителя')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            raise HTTPException(413, 'Уведомление слишком большое')
    try:
        event = json.loads(body)
        if not isinstance(event, dict):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Некорректное уведомление') from None
    order_id, link_id = event.get('payload'), event.get('paymentLinkId')
    if isinstance(order_id, str) and isinstance(link_id, str):
        # Persist a wake-up only. The polling worker fetches both canonical
        # records and does not apply financial state from a callback.
        execute("""UPDATE orders SET next_attempt=0
            WHERE id=? AND method='platega' AND payment_id=? AND status='pending'""",
            (order_id, link_id))
    return Response(status_code=200)
