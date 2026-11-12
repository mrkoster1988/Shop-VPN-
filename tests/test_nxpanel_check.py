"""Контракт NX диагностики: только чтение, ограниченная пагинация, без утечки секретов."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.nxpanel import NXPanel
from app import servers

spec = importlib.util.spec_from_file_location('nx_panel_check', Path(__file__).parents[1] / 'scripts/panel_check.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def profile(**changes):
    return {'api': 'nxpanel', 'url': 'https://nx.example/private-path',
            'token': 'private-token', 'template_id': 7} | changes


def identity(**changes):
    return {'id': 1, 'token': True, 'base': 'admin',
            'scopes': ['users:read', 'templates:read']} | changes


def page(items, limit=100, offset=0, total=None):
    return {'items': items, 'limit': limit, 'offset': offset,
            'total': len(items) if total is None else total}


def handler(request):
    route = request.url.path.rsplit('/', 1)[-1]
    if route == 'me':
        data = identity()
    elif route == 'users':
        data = page([{'id': 4, 'name': 'private-client', 'subToken': 'private-sub-token'}], limit=1)
    elif route == 'templates':
        data = page([{'id': 7, 'name': 'private-template', 'core': {'secret': 'private-key'}}])
    else:
        pytest.fail('Неожиданный маршрут')
    return httpx.Response(200, json=data)


def probe(callback=handler, data=None):
    return asyncio.run(s.probe(data or profile(), nxpanel=True,
        client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(callback), **kw)))


def test_only_versioned_reads_preserve_base_path_and_redact_data():
    calls = []
    def capture(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.headers['Authorization'] == 'Bearer private-token'
        return handler(request)
    result = probe(capture)
    assert result['status'] == 'api_checks_ok'
    assert all(result['checks'].values()) and not any(result['scope'].values())
    assert [r.url.path for r in calls] == ['/private-path/api/v1/' + route for route in ('me', 'users', 'templates')]
    assert dict(calls[1].url.params) == {'limit': '1', 'offset': '0'}
    assert 'private' not in json.dumps(result) and 'nx.example' not in json.dumps(result)


@pytest.mark.parametrize('scopes', [[], ['users:write', 'templates:write']])
def test_unrestricted_or_write_scope_can_read_without_claiming_writes(scopes):
    result = probe(lambda r: httpx.Response(200, json=identity(scopes=scopes)) if r.url.path.endswith('/me') else handler(r))
    assert result['status'] == 'api_checks_ok'
    assert result['warnings'] == ([] if scopes else ['NXUnrestrictedToken'])
    assert result['scope']['provisioning_checked'] is False


@pytest.mark.parametrize('change,code', [
    ({'token': False}, 'UnsupportedNXIdentity'), ({'id': True}, 'UnsupportedNXIdentity'),
    ({'base': 'unknown'}, 'UnsupportedNXIdentity'), ({'scopes': None}, 'UnsupportedNXIdentity'),
    ({'scopes': 'users:read'}, 'UnsupportedNXIdentity'), ({'scopes': [1]}, 'UnsupportedNXIdentity'),
    ({'scopes': ['users:read']}, 'NXReadScopesRequired'),
    ({'scopes': ['templates:read']}, 'NXReadScopesRequired'),
    ({'base': 'resale', 'blocked': True}, 'NXResellerUnavailable'),
    ({'base': 'resale'}, 'NXResellerUnavailable'),
])
def test_invalid_identity_stops_before_reading_customers(change, code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=identity(**change))
    result = probe(respond)
    assert result['error_code'] == code and len(calls) == 1


def test_template_scan_reaches_second_page():
    offsets = []
    def respond(request):
        if not request.url.path.endswith('/templates'):
            return handler(request)
        offset = int(request.url.params['offset'])
        offsets.append(offset)
        ids = range(1, 101) if offset == 0 else [107]
        return httpx.Response(200, json=page([{'id': n} for n in ids], offset=offset, total=101))
    assert probe(respond, profile(template_id=107))['status'] == 'api_checks_ok'
    assert offsets == [0, 100]


@pytest.mark.parametrize('mode,code', [('missing', 'NXTemplateNotFound'),
    ('repeated', 'NXTemplateListChanged'), ('changed', 'NXTemplateListChanged'),
    ('limit', 'NXTemplateScanLimit')])
def test_missing_or_unstable_template_lists_never_report_ready(mode, code):
    calls = []
    def respond(request):
        if not request.url.path.endswith('/templates'):
            return handler(request)
        offset = int(request.url.params['offset'])
        calls.append(offset)
        total = 101 if mode in ('repeated', 'changed') else 1001
        items = [{'id': n} for n in range(offset + 1, offset + 101)]
        if mode == 'missing':
            total, items = 0, []
        elif offset and mode == 'repeated':
            items = [{'id': 1}]
        elif offset and mode == 'changed':
            total = 200
        return httpx.Response(200, json=page(items, offset=offset, total=total))
    assert probe(respond, profile(template_id=2000))['error_code'] == code
    assert len(calls) <= NXPanel.MAX_PAGES


@pytest.mark.parametrize('body', [[], {}, {'items': []}, page([], total=1),
    page([{'id': True}]), page([{'id': 0}]), page([{'id': 7}, {'id': 7}]),
    page([{'id': 7}], offset=1), page([{'id': 7}], limit=True),
    page([{'id': 7}], total=True)])
def test_malformed_template_envelopes_fail_closed(body):
    assert probe(lambda r: httpx.Response(200, json=body) if r.url.path.endswith('/templates') else handler(r))['error_code'] == 'UnsupportedNXSchema'


@pytest.mark.parametrize('body', [[], {'items': [], 'total': 0}, page([{'id': '4'}], limit=1)])
def test_user_list_contract_is_checked(body):
    assert probe(lambda r: httpx.Response(200, json=body) if r.url.path.endswith('/users') else handler(r))['error_code'] == 'UnsupportedNXSchema'


@pytest.mark.parametrize('status,code', [(401, 'AuthenticationRejected'), (403, 'AuthenticationRejected'),
    (302, 'RedirectRefused'), (402, 'NXLicenseLimit'), (429, 'NXRateLimited'), (500, 'PanelHTTPError')])
def test_http_errors_are_redacted_and_not_retried(status, code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, text='private-token', headers={'Location': 'https://other.example', 'Retry-After': '99999'})
    result = probe(respond)
    assert result['error_code'] == code and len(calls) == 1
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('error,code', [(httpx.ReadTimeout, 'PanelTimeout'), (httpx.ConnectError, 'PanelConnectionFailed')])
def test_transport_errors_do_not_expose_credentials(error, code):
    def respond(request):
        raise error('private-token at private-path', request=request)
    assert probe(respond)['error_code'] == code


def test_json_and_size_failures_are_bounded(monkeypatch):
    assert probe(lambda r: httpx.Response(200, text='private-token'))['error_code'] == 'UnsupportedNXSchema'
    monkeypatch.setattr(NXPanel, 'MAX_BODY', 16)
    assert probe(lambda r: httpx.Response(200, content=b' ' * 17))['error_code'] == 'NXResponseTooLarge'


@pytest.mark.parametrize('change', [{'url': 'http://127.0.0.1'}, {'url': 'https://u:p@nx.example'},
    {'url': 'https://nx.example/base?token=private'}, {'url': 'https://nx.example/a/../b'},
    {'url': 'https://nx.example/a%2fb'}, {'url': 'https://nx.example/api/v1'},
    {'url': 'https://nx.example/a//b'}, {'token': 'private\ntoken'},
    {'template_id': True}, {'template_id': '7'}, {'template_id': 0}, {'unexpected': 1}])
def test_invalid_profile_is_rejected_before_network(change):
    assert probe(lambda r: pytest.fail('Сеть не должна вызываться'), profile(**change))['status'] == 'invalid_input'


def test_cli_requires_private_profile_and_rejects_registered_server(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'nx.json'
    path.write_text(json.dumps(profile()))
    path.chmod(0o600)
    observed = []
    async def fake(data, ca_file=None, **kw):
        observed.append(kw)
        return {'status': 'api_checks_ok'}
    monkeypatch.setattr(s, 'probe', fake)
    assert s.main(['--nxpanel', '--profile-file', str(path)]) == 0
    assert observed == [{'wgdashboard': False, 'nxpanel': True}]
    monkeypatch.setattr(s, 'registered_profile', lambda *a: pytest.fail('Реестр не должен читаться'))
    assert s.main(['--nxpanel', '--server', 'fixture']) == 2
    with pytest.raises(SystemExit):
        s.main(['--nxpanel', '--wgdashboard', '--profile-file', str(path)])
    assert 'private-token' not in capsys.readouterr().out


def test_diagnostic_profile_cannot_enable_a_sales_server():
    with pytest.raises(Exception):
        servers.validated('xui', profile())


def test_root_url_and_unblocked_reseller():
    def respond(request):
        assert request.url.path.startswith('/api/v1/')
        if request.url.path.endswith('/me'):
            return httpx.Response(200, json=identity(base='resale', blocked=False))
        return handler(request)
    assert probe(respond, profile(url='https://nx.example/'))['status'] == 'api_checks_ok'


def test_diagnostic_client_requires_tls_and_ignores_environment_proxies():
    import ssl
    def factory(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert kwargs['verify'].verify_mode == ssl.CERT_REQUIRED
        assert kwargs['verify'].check_hostname is True
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    result = asyncio.run(s.probe(profile(), nxpanel=True, client_factory=factory))
    assert result['status'] == 'api_checks_ok'
