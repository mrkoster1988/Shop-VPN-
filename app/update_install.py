"""Клиент узкого loopback API службы. У магазина нет доступа к Docker."""
import os
from uuid import UUID
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field


class Plan(BaseModel):
    model_config = {'extra':'forbid'}
    request_id: UUID
    tag: str = Field(pattern=r'^v(?:0|[1-9][0-9]{0,5})\.[0-9]{3}$', max_length=12)


class Apply(Plan):
    plan_id: UUID


def configuration():
    port = os.getenv('PUSHOK_HOST_UPDATER_PORT','')
    token = os.getenv('PUSHOK_HOST_UPDATER_TOKEN','')
    if not port.isascii() or not port.isdigit() or not 1024 <= int(port) <= 65535 or len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        return None
    return int(port), token


async def request(action, data=None):
    config = configuration()
    if not config:
        raise HTTPException(503, 'Служба обновления ещё не подключена на VPS.')
    port, token = config
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            async with client.stream('POST' if data else 'GET', 'http://127.0.0.1:'+str(port)+'/v1/'+action,
                    headers={'Authorization':'Bearer '+token}, json=data) as response:
                if response.status_code == 409:
                    raise HTTPException(409, 'Служба отклонила запрос: проверьте статус, действительность плана и завершённость предыдущего обновления.')
                if response.status_code not in (200,202):
                    raise ValueError()
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        raise ValueError()
                import json
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get('enabled') is not True or result.get('reachable') is not True:
                    raise ValueError()
                # Не пропускаем произвольные данные службы в браузер.
                job = result.get('job')
                if job is not None:
                    if not isinstance(job, dict):
                        raise ValueError()
                    keys = {'id','kind','plan_id','tag','state','error','created','updated','previous','commit','expires','can_apply','phase','activation_started'}
                    job = {k:v for k,v in job.items() if k in keys}
                return {'enabled':True,'reachable':True,'job':job}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Нет подтверждения от службы обновления. Запрос мог быть принят: обновите статус, не создавайте новую операцию.') from None


async def status():
    if not configuration():
        return {'enabled':False,'reachable':False,'job':None}
    try:
        return await request('status')
    except HTTPException:
        return {'enabled':True,'reachable':False,'job':None}


async def plan(data):
    from . import updates
    current = updates.status()
    if not current['can_open_guide'] or current['release']['tag'] != data.tag:
        raise HTTPException(409, 'Сначала подтвердите доступность этой версии свежей проверкой релизов.')
    return await request('plan', data.model_dump(mode='json'))
