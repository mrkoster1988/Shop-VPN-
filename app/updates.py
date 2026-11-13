"""Проверка релизов магазина. Не скачивает и не выполняет обновления."""
import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import re
import secrets
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from . import diagnostics
from .db import db

REPOSITORY = 'mrkoster1988/Shop-VPN-'
RELEASES_URL = 'https://github.com/' + REPOSITORY + '/releases'
API_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
MAX_RESPONSE = 256 * 1024
LEASE_SECONDS = 120
MANUAL_INTERVAL = 60
log = logging.getLogger('pushok.updates')


def version(value):
    match = re.fullmatch(r'v(0|[1-9][0-9]{0,5})\.([0-9]{3})', value or '')
    return tuple(map(int, match.groups())) if match else None


class Schedule(BaseModel):
    model_config = {'extra': 'forbid'}
    enabled: bool = Field(strict=True)
    check_time: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$', strict=True)
    timezone: str = Field(min_length=1, max_length=64, strict=True)
    version: int = Field(ge=1, strict=True)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Укажите часовой пояс IANA, например Europe/Moscow') from None
        return value


def next_daily(now, check_time, zone):
    """Один запуск в местную дату; пропущенное при DST время сдвигается вперёд."""
    tz = ZoneInfo(zone)
    local = datetime.fromtimestamp(now, tz)
    hour, minute = map(int, check_time.split(':'))
    for offset in range(3):
        day = local.date() + timedelta(days=offset)
        target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz, fold=0)
        stamp = int(target.timestamp())
        if stamp > now:
            return stamp
    raise ValueError('Не удалось рассчитать расписание')


def scheduler_enabled():
    return os.getenv('BACKGROUND_TASKS', 'true') == 'true' and os.getenv('MAINTENANCE_MODE') != 'true'


def _row(conn):
    return dict(conn.execute('SELECT * FROM update_checker WHERE id=1').fetchone())


def status(now=None):
    from . import update_install
    now = int(time.time()) if now is None else now
    with db() as conn:
        row = _row(conn)
    release = json.loads(row['release_json']) if row['release_json'] else None
    checking = bool(row['lease_token'] and row['lease_until'] > now)
    error = row['error_code']
    if row['lease_token'] and not checking:
        error = 'interrupted'
    stale = bool(release and (error or not row['last_success'] or now - row['last_success'] > 48 * 3600))
    current = version(diagnostics.VERSION)
    available = bool(release and current and version(release['tag']) > current)
    if error:
        state = 'check_failed'
    elif not row['last_success']:
        state = 'not_checked'
    elif stale:
        state = 'stale'
    elif not current:
        state = 'unknown_version'
    elif available:
        state = 'available'
    elif release and version(release['tag']) < current:
        state = 'ahead'
    else:
        state = 'current'
    due = row['retry_at'] or row['next_check']
    cooldown = max((row['last_attempt'] or 0) + MANUAL_INTERVAL,
                   row['retry_at'] if row['error_code'] == 'rate_limited' else 0)
    return {
        'current_version': diagnostics.VERSION, 'state': state, 'checking': checking,
        'settings': {key: (bool(row[key]) if key == 'enabled' else row[key])
                     for key in ('enabled', 'check_time', 'timezone', 'version')},
        'scheduler_running': scheduler_enabled(),
        'last_attempt': row['last_attempt'], 'last_success': row['last_success'],
        'next_check': max(now, row['lease_until'] if checking else due) if row['enabled'] and scheduler_enabled() else None,
        'manual_allowed_at': max(cooldown, row['lease_until'] if checking else 0),
        'error_code': error, 'release': release, 'stale': stale,
        'update_available': available, 'can_open_guide': available and not stale and not checking,
        'installation_mode': 'host' if update_install.configuration() else 'manual', 'releases_url': RELEASES_URL,
    }


def save_schedule(data):
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _row(conn)
        if row['version'] != data.version:
            raise HTTPException(409, 'Расписание уже изменено. Обновите раздел и повторите.')
        conn.execute('''UPDATE update_checker SET enabled=?,check_time=?,timezone=?,version=version+1,
            next_check=?,retry_at=CASE WHEN error_code='rate_limited' OR lease_token IS NOT NULL THEN retry_at ELSE 0 END WHERE id=1''',
            (int(data.enabled), data.check_time, data.timezone, next_daily(now, data.check_time, data.timezone)))
    return status()


class CheckError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after


def parse_release(data):
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise CheckError('invalid_release')
    tag = data.get('tag_name')
    if not isinstance(tag, str) or not version(tag):
        raise CheckError('invalid_release')
    try:
        published = datetime.fromisoformat(data['published_at'].replace('Z', '+00:00'))
        if published.tzinfo is None or published.timestamp() <= 0:
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        raise CheckError('invalid_release') from None
    notes, name = data.get('body') or '', data.get('name') or tag
    if not isinstance(notes, str) or not isinstance(name, str):
        raise CheckError('invalid_release')
    # Ссылки строим сами: ответы GitHub не могут изменить репозиторий/источник.
    return {'tag': tag, 'name': name[:160], 'notes': notes[:20000], 'notes_truncated': len(notes) > 20000,
            'published_at': int(published.timestamp()), 'url': RELEASES_URL + '/tag/' + tag,
            'guide_url': 'https://github.com/' + REPOSITORY + '/blob/' + tag + '/docs/UPGRADE.md'}


async def fetch_release(etag, cached):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Pushok-Shop-Update-Checker',
               'X-GitHub-Api-Version': '2026-03-10'}
    if etag and cached:
        headers['If-None-Match'] = etag
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async with client.stream('GET', API_URL, headers=headers) as response:
            if response.status_code == 304:
                if not cached or not etag:
                    raise CheckError('invalid_release')
                return cached, etag
            if response.status_code in (403, 429):
                delay = response.headers.get('retry-after', '')
                reset = response.headers.get('x-ratelimit-reset', '')
                delay = int(delay) if delay.isascii() and delay.isdigit() and len(delay) < 12 else 3600
                if reset.isascii() and reset.isdigit() and len(reset) < 12:
                    delay = max(delay, int(reset) - int(time.time()))
                raise CheckError('rate_limited', min(86400, max(60, delay)))
            if response.status_code != 200:
                raise CheckError('source_unavailable')
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise CheckError('invalid_release')
            try:
                release = parse_release(json.loads(raw))
            except (ValueError, UnicodeError):
                raise CheckError('invalid_release') from None
            new_etag = response.headers.get('etag', '')
            if len(new_etag) > 256 or any(ord(ch) < 32 or ord(ch) > 126 for ch in new_etag):
                new_etag = ''
            return release, new_etag


def _claim(manual, now):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _row(conn)
        if row['lease_token'] and row['lease_until'] > now:
            if manual:
                raise HTTPException(409, 'Проверка уже выполняется. Обновите статус через минуту.')
            return None
        if manual:
            if (row['last_attempt'] is not None and now < row['last_attempt'] + MANUAL_INTERVAL
                    or row['error_code'] == 'rate_limited' and now < row['retry_at']):
                raise HTTPException(429, 'Проверка временно недоступна. Дождитесь времени следующей попытки.')
        elif not row['enabled'] or now < (row['retry_at'] or row['next_check']):
            return None
        token = secrets.token_hex(16)
        # Аренда переживает рестарт: после её истечения незавершённую проверку повторим.
        conn.execute('''UPDATE update_checker SET lease_token=?,lease_until=?,last_attempt=?,retry_at=?,
            next_check=? WHERE id=1''', (token, now + LEASE_SECONDS, now, now + LEASE_SECONDS,
                                        next_daily(now, row['check_time'], row['timezone'])))
        return token, row


def _finish(token, release=None, etag='', error=None):
    now = int(time.time())
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _row(conn)
        if row['lease_token'] != token:
            return  # Поздний ответ не перезапишет более новую проверку.
        if error:
            failures = min(100, row['failures'] + 1)
            delay = max(error.retry_after, (900, 3600, 21600)[min(2, failures - 1)])
            conn.execute('''UPDATE update_checker SET error_code=?,failures=?,retry_at=?,
                lease_token=NULL,lease_until=0 WHERE id=1''', (error.code, failures, now + delay))
        else:
            conn.execute('''UPDATE update_checker SET last_success=?,error_code=NULL,failures=0,
                retry_at=0,lease_token=NULL,lease_until=0,release_json=?,etag=? WHERE id=1''',
                (now, json.dumps(release, ensure_ascii=False), etag))


async def check(*, manual=False):
    claim = _claim(manual, int(time.time()))
    if not claim:
        return None
    token, row = claim
    try:
        async with asyncio.timeout(20):
            cached = json.loads(row['release_json']) if row['release_json'] else None
            release, etag = await fetch_release(row['etag'], cached)
    except asyncio.CancelledError:
        _finish(token, error=CheckError('interrupted'))
        raise
    except CheckError as error:
        _finish(token, error=error)
    except (httpx.HTTPError, TimeoutError):
        _finish(token, error=CheckError('network_error'))
    except Exception:
        # Не отдаём HTTP-ответы/исключения внешнего сервиса в API или журнал.
        _finish(token, error=CheckError('check_error'))
    else:
        _finish(token, release, etag)
    return status()


async def worker():
    while True:
        try:
            await check()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning('Не удалось сохранить результат проверки обновлений')
        await asyncio.sleep(30)
