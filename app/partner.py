"""Подписанный приём проверенных событий для персональной партнёрской скидки."""
import hashlib
import hmac
import json
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import config as c
from .db import db


MAX_SKEW_SECONDS = 300
GRANT_LIFETIME_SECONDS = 30 * 86400


class PartnerEvent(BaseModel):
    model_config = {'extra': 'forbid'}
    event_id: UUID
    telegram_id: int = Field(gt=0, le=9223372036854775807, strict=True)
    event_type: Literal['service_purchase_paid']


def _canonical_hash(event):
    stable=json.dumps(event.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(stable).hexdigest()


def accept(headers, body):
    """Проверяет HMAC и атомарно фиксирует одно право на промокод."""
    secret=c.PARTNER_WEBHOOK_SECRET
    code=c.PARTNER_PROMO_CODE
    if len(secret.encode())<32 or not code:
        raise HTTPException(503,'Партнёрская скидка не настроена')
    if not body or len(body)>4096:
        raise HTTPException(413,'Событие партнёра слишком большое')

    timestamp=headers.get('x-partner-timestamp','')
    signature=headers.get('x-partner-signature','')
    if not re.fullmatch(r'[0-9]{1,12}',timestamp) or not re.fullmatch(r'sha256=[0-9a-f]{64}',signature):
        raise HTTPException(401,'Подпись партнёра недействительна')
    signed=timestamp.encode()+b'.'+body
    expected='sha256='+hmac.new(secret.encode(),signed,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):
        raise HTTPException(401,'Подпись партнёра недействительна')

    try:
        event=PartnerEvent.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422,'Проверьте данные события партнёра') from None
    now=int(time.time())
    issued_at=int(timestamp)
    if abs(now-issued_at)>MAX_SKEW_SECONDS:
        raise HTTPException(401,'Подпись партнёра устарела')
    event_id=str(event.event_id)
    payload_hash=_canonical_hash(event)

    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous=conn.execute('SELECT payload_hash,expires_at FROM partner_events WHERE event_id=?',(event_id,)).fetchone()
        if previous:
            if not hmac.compare_digest(previous['payload_hash'],payload_hash):
                raise HTTPException(409,'Идентификатор события уже использован')
            return {'accepted':True,'expires_at':previous['expires_at'],'replayed':True}
        coupon=conn.execute('SELECT id,enabled,expires_at,max_uses FROM coupons WHERE code=?',(code,)).fetchone()
        if (not coupon or not coupon['enabled']
            or (coupon['expires_at'] and coupon['expires_at']<=now)
            or conn.execute('SELECT count(*) FROM coupon_uses WHERE coupon_id=?',(coupon['id'],)).fetchone()[0]>=coupon['max_uses']):
            raise HTTPException(503,'Партнёрский промокод сейчас недоступен')
        expires_at=now+GRANT_LIFETIME_SECONDS
        if coupon['expires_at']:
            expires_at=min(expires_at,coupon['expires_at'])
        conn.execute('INSERT INTO partner_events(event_id,telegram_id,promo_code,payload_hash,received_at,expires_at) VALUES (?,?,?,?,?,?)',
            (event_id,event.telegram_id,code,payload_hash,now,expires_at))
    return {'accepted':True,'expires_at':expires_at,'replayed':False}
