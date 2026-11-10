"""Verified YooKassa notification entrypoint: a webhook is only a wake-up signal.

Never trust the payload as proof of payment: fetch payment details with merchant
credentials, compare order identity and amount, and reuse the idempotent state
transition used by the background polling worker.
"""
import json
import re
from fastapi import HTTPException, Request
from .db import one
from . import service

VALID_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,127}$')
SUPPORTED_EVENTS = frozenset(('payment.succeeded', 'payment.canceled'))


async def receive(request: Request):
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'Ожидается JSON-уведомление')
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 4096:
            raise HTTPException(413, 'Уведомление слишком большое')
    try:
        event = json.loads(payload)
        if not isinstance(event, dict) or event.get('type') != 'notification':
            raise ValueError()
        notification_type = event['event']
        details = event['object']
        if not isinstance(details, dict):
            raise ValueError()
        payment_id = details.get('id')
        if not isinstance(payment_id, str) or not VALID_ID.fullmatch(payment_id):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, 'Некорректное уведомление') from None

    if notification_type not in SUPPORTED_EVENTS:
        return {'ok': True, 'handled': False}

    # An unrelated/early event must not trigger an outbound request with arbitrary ID.
    # If the event arrived before the payment_id was persisted, normal polling remains
    # the recovery path once creation is finished.
    order = one("SELECT * FROM orders WHERE payment_id=? AND method IN ('yookassa','sbp')",
                (payment_id,))
    if not order or order['status'] != 'pending':
        return {'ok': True, 'handled': False}
    try:
        await service.reconcile_payment(order)
    except Exception:
        # Provider errors/mismatches cannot turn an unverified notification into money.
        # A non-200 asks YooKassa to retry without exposing provider internals.
        raise HTTPException(503, 'Подтверждение временно недоступно') from None
    current = one('SELECT status FROM orders WHERE id=?', (order['id'],))
    if notification_type == 'payment.succeeded' and current['status'] == 'pending':
        raise HTTPException(503, 'Подтверждение пока не получено')
    return {'ok': True, 'handled': True}
