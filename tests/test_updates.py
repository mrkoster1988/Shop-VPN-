"""Релизы, устойчивое расписание, сетевые ошибки и границы доступа владельца."""
import asyncio
from datetime import datetime
import json

import httpx
import pytest

from app import updates
from app.db import db, execute, init_db, one
from app.security import digest
from test_shop import client, admin_headers


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


NOW = stamp('2026-11-12T17:00:00+00:00')  # 20:00 Москва


def release(tag='v0.056', **extra):
    return {'tag_name': tag, 'draft': False, 'prerelease': False,
            'published_at': '2026-11-12T15:00:00Z', 'name': 'Новая версия', 'body': 'Исправления', **extra}


@pytest.fixture
def clock(client, monkeypatch):
    values = [NOW]
    monkeypatch.setattr(updates.time, 'time', lambda: values[0])
    monkeypatch.setattr(updates.diagnostics, 'VERSION', 'v0.055')
    return values


def network(monkeypatch, reply):
    original = httpx.AsyncClient
    calls, options = [], []
    def handle(request):
        calls.append(request)
        return reply(request)
    def factory(**kwargs):
        options.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(updates.httpx, 'AsyncClient', factory)
    return calls, options


def check():
    return asyncio.run(updates.check())


def test_initial_check_then_daily_without_browser(clock, monkeypatch):
    calls, options = network(monkeypatch, lambda _: httpx.Response(200, json=release(), headers={'etag': '"one"'}))
    result = check()
    assert result['state'] == 'available' and result['can_open_guide']
    assert result['current_version'] == 'v0.055'
    assert one('SELECT next_check FROM update_checker')['next_check'] == NOW + 3600
    assert check() is None
    clock[0] += 3599
    assert check() is None
    clock[0] += 1
    assert check()['state'] == 'available'
    assert len(calls) == 2
    assert calls[0].url == updates.API_URL and 'authorization' not in calls[0].headers
    assert calls[1].headers['if-none-match'] == '"one"'
    assert all(x['trust_env'] is False and x['follow_redirects'] is False for x in options)


@pytest.mark.parametrize('tag,state', [('v0.054','ahead'),('v0.055','current'),('v0.056','available'),('v1.000','available')])
def test_version_comparison(clock, monkeypatch, tag, state):
    network(monkeypatch, lambda _: httpx.Response(200, json=release(tag)))
    result = check()
    assert result['state'] == state
    assert result['can_open_guide'] == (state == 'available')
    assert updates.version('v10.000') > updates.version('v9.999')


@pytest.mark.parametrize('changes', [dict(draft=True),dict(prerelease=True),dict(draft='false'),
    dict(tag_name='v0.057-beta'),dict(tag_name='v01.000'),dict(tag_name='v0.55'),dict(tag_name='../main'),
    dict(tag_name=56),dict(published_at=None),dict(published_at='2026-11-12'),dict(body=['bad']),dict(name={1:2})])
def test_invalid_release_cannot_enable_update(clock, monkeypatch, changes):
    network(monkeypatch, lambda _: httpx.Response(200, json=release(**changes)))
    result = check()
    assert result['state'] == 'check_failed' and result['error_code'] == 'invalid_release'
    assert result['release'] is None and result['can_open_guide'] is False


def test_untrusted_links_ignored_notes_bounded(clock, monkeypatch):
    network(monkeypatch, lambda _: httpx.Response(200, json=release(
        body='<script>alert(1)</script>' + 'x' * 21000, html_url='https://evil.example', tarball_url='http://localhost')))
    result = check()['release']
    assert len(result['notes']) == 20000 and result['notes_truncated']
    assert result['url'] == updates.RELEASES_URL + '/tag/v0.056'
    assert result['guide_url'].endswith('/Shop-VPN-/blob/v0.056/docs/UPGRADE.md')


@pytest.mark.parametrize('code,error', [(301,'source_unavailable'),(302,'source_unavailable'),(404,'source_unavailable'),
    (500,'source_unavailable'),(429,'rate_limited'),(403,'rate_limited')])
def test_http_errors_do_not_mean_no_updates(clock, monkeypatch, code, error):
    calls, _ = network(monkeypatch, lambda _: httpx.Response(code, text='private upstream error', headers={'location':'http://localhost/secret'}))
    result = check()
    assert result['error_code'] == error and result['state'] == 'check_failed'
    assert 'private upstream' not in json.dumps(result)
    assert len(calls) == 1 and check() is None


def test_network_failure_keeps_old_release_and_retry_survives_restart(clock, monkeypatch):
    replies = [httpx.Response(200,json=release(),headers={'etag':'"one"'}), httpx.ConnectError('secret-in-exception')]
    def reply(_):
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    network(monkeypatch, reply)
    check(); clock[0] += 3600
    result = check()
    assert result['state'] == 'check_failed' and result['stale'] and not result['can_open_guide']
    assert result['release']['tag'] == 'v0.056' and result['last_success'] == NOW
    init_db()
    assert check() is None
    assert one('SELECT retry_at FROM update_checker')['retry_at'] == clock[0] + 900
    clock[0] += 900
    replies.append(httpx.Response(304))
    result = check()
    assert result['state'] == 'available' and result['last_success'] == clock[0]
    assert not result['error_code'] and not result['stale']
    assert one('SELECT failures,retry_at FROM update_checker') == {'failures':0,'retry_at':0}


def test_rate_limit_respected_by_manual_request(clock, client, monkeypatch):
    network(monkeypatch, lambda _: httpx.Response(429, headers={'retry-after':'7200'}))
    result = client.post('/api/admin/updates/check', headers=admin_headers())
    assert result.status_code == 200 and result.json()['error_code'] == 'rate_limited'
    clock[0] += 61
    assert client.post('/api/admin/updates/check', headers=admin_headers()).status_code == 429
    assert one('SELECT retry_at FROM update_checker')['retry_at'] == NOW + 7200


@pytest.mark.parametrize('reply', [httpx.Response(304),httpx.Response(200,text='not json'),
    httpx.Response(200,content=b'x'*(updates.MAX_RESPONSE+1))])
def test_missing_cache_and_invalid_large_responses(clock, monkeypatch, reply):
    network(monkeypatch, lambda _: reply)
    assert check()['error_code'] == 'invalid_release'


def test_stale_success_requires_new_check(clock, monkeypatch):
    network(monkeypatch, lambda _: httpx.Response(200,json=release()))
    check(); clock[0] += 48 * 3600 + 1
    result = updates.status()
    assert result['stale'] and result['state'] == 'stale' and not result['can_open_guide']


def test_expired_lease_retried_late_response_ignored(clock, monkeypatch):
    old_token, _ = updates._claim(False, NOW)
    assert updates.status()['checking'] and check() is None
    clock[0] += updates.LEASE_SECONDS
    assert updates.status()['error_code'] == 'interrupted'
    new_token, _ = updates._claim(False, clock[0])
    updates._finish(old_token, updates.parse_release(release('v9.999')))
    assert updates.status()['release'] is None and updates.status()['checking']
    updates._finish(new_token, updates.parse_release(release()))
    assert updates.status()['release']['tag'] == 'v0.056'


def test_cancel_releases_lease_and_preserves_retry(clock, monkeypatch):
    async def canceled(*_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(updates, 'fetch_release', canceled)
    with pytest.raises(asyncio.CancelledError):
        check()
    result = updates.status()
    assert not result['checking'] and result['error_code'] == 'interrupted'


def test_schedule_off_manual_check_and_conflict(clock, client, monkeypatch):
    calls, _ = network(monkeypatch, lambda _: httpx.Response(200,json=release()))
    data = {'enabled':False,'check_time':'22:15','timezone':'Asia/Yekaterinburg','version':1}
    result = client.put('/api/admin/updates/schedule', json=data, headers=admin_headers())
    assert result.status_code == 200 and result.json()['settings']['version'] == 2
    assert result.json()['next_check'] is None and check() is None and calls == []
    assert client.put('/api/admin/updates/schedule', json=data, headers=admin_headers()).status_code == 409
    assert client.post('/api/admin/updates/check', headers=admin_headers()).json()['state'] == 'available'
    assert client.post('/api/admin/updates/check', headers=admin_headers()).status_code == 429


def test_schedule_survives_migration_and_long_downtime(clock, monkeypatch):
    monkeypatch.setenv('BACKGROUND_TASKS','true')
    network(monkeypatch, lambda _: httpx.Response(200,json=release()))
    updates.save_schedule(updates.Schedule(enabled=True,check_time='21:00',timezone='Europe/Moscow',version=1))
    assert updates.status()['next_check'] == NOW + 3600
    assert check() is None
    init_db()
    clock[0] += 3 * 86400
    assert check()['state'] == 'available' and check() is None
    assert updates.status()['next_check'] == clock[0] + 3600


def test_schedule_change_during_check_keeps_new_time_and_recovery(clock):
    updates._claim(False, NOW)
    updates.save_schedule(updates.Schedule(enabled=True,check_time='23:00',timezone='UTC',version=1))
    row = one('SELECT next_check,retry_at FROM update_checker')
    assert row['next_check'] == stamp('2026-11-12T23:00:00+00:00')
    assert row['retry_at'] == NOW + updates.LEASE_SECONDS


@pytest.mark.parametrize('now,local_time,zone,expected', [
    ('2026-11-12T17:59:00+00:00','21:00','Europe/Moscow','2026-11-12T18:00:00+00:00'),
    ('2026-11-12T18:00:00+00:00','21:00','Europe/Moscow','2026-11-13T18:00:00+00:00'),
    ('2026-11-12T23:59:00+00:00','00:00','UTC','2026-11-13T00:00:00+00:00'),
    ('2026-03-29T00:00:00+00:00','02:30','Europe/Berlin','2026-03-29T01:30:00+00:00'),
    ('2026-10-25T00:30:00+00:00','02:30','Europe/Berlin','2026-10-26T01:30:00+00:00'),
])
def test_schedule_timezones_and_dst(now, local_time, zone, expected):
    assert updates.next_daily(stamp(now),local_time,zone) == stamp(expected)


@pytest.mark.parametrize('changes', [dict(enabled='yes'),dict(check_time='24:00'),dict(check_time='21:60'),
    dict(timezone='not/a/timezone'),dict(timezone='/etc/passwd'),dict(timezone='../UTC'),dict(version=True),dict(url='http://localhost')])
def test_invalid_schedule_rejected(clock, client, changes):
    payload = {'enabled':True,'check_time':'21:00','timezone':'Europe/Moscow','version':1} | changes
    before = one('SELECT * FROM update_checker')
    assert client.put('/api/admin/updates/schedule', json=payload, headers=admin_headers()).status_code == 422
    assert one('SELECT * FROM update_checker') == before


@pytest.mark.parametrize('role', ['panel_operator','support','finance','mtproxy_manager'])
def test_updates_owner_only(clock, client, role):
    with db() as conn:
        cur = conn.execute('INSERT INTO staff_users(email,password,role,created,updated) VALUES (?,?,?,?,?)',
                           (role+'@example.com','unused',role,NOW,NOW))
        conn.execute('INSERT INTO staff_sessions(token_hash,staff_id,created,expires) VALUES (?,?,?,?)',
                     (digest('session'),cur.lastrowid,NOW,NOW+3600))
    client.cookies.set('staff_session','session')
    for method,path in [('get','updates'),('post','updates/check'),('put','updates/schedule')]:
        result = getattr(client,method)('/api/admin/'+path, json={} if method == 'put' else None) if method != 'get' else client.get('/api/admin/'+path)
        assert result.status_code == 403


def test_anonymous_csrf_and_read_only_status(clock, client, monkeypatch):
    calls, _ = network(monkeypatch, lambda _: pytest.fail('GET must not contact GitHub'))
    assert client.get('/api/admin/updates').status_code == 401
    result = client.get('/api/admin/updates', headers=admin_headers())
    assert result.status_code == 200 and result.json()['state'] == 'not_checked'
    assert result.json()['scheduler_running'] is False and calls == []
    assert client.post('/api/admin/updates/check', headers=admin_headers() | {'Origin':'https://evil.example'}).status_code == 403
    assert 'no-store' in result.headers['cache-control']


def test_concurrent_manual_check_returns_busy(clock, client):
    updates._claim(False,NOW)
    result = client.post('/api/admin/updates/check', headers=admin_headers())
    assert result.status_code == 409
