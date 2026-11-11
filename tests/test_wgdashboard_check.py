"""Контракт чтения WGDashboard; реальные peers и VPN-трафик не проверяются."""
import asyncio
import json
import httpx
import pytest
from test_panel_check import s

PROFILE = {'url':'https://wg.example/private', 'token':'t'*40, 'configuration':'wg0'}


def probe(body, status=200):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    report = asyncio.run(s.probe(PROFILE, client_factory=factory, wgdashboard=True))
    return report, calls


def test_check_reads_only_and_never_returns_private_panel_data():
    report, calls = probe({'status':True, 'data':[{
        'Name':'wg0', 'Protocol':'wg', 'Status':True, 'PrivateKey':'secret-key'}]})
    assert report['status'] == 'api_checks_ok'
    assert report['checks'] == {'wireguard_interface':True, 'supported_schema':True}
    assert all(value is False for value in report['scope'].values())
    assert len(calls) == 1 and calls[0].method == 'GET'
    assert calls[0].url.path == '/private/api/getWireguardConfigurations'
    assert calls[0].headers['wg-dashboard-apikey'] == PROFILE['token']
    assert not any(value in json.dumps(report) for value in ('secret-key', PROFILE['token'], 'wg.example'))


@pytest.mark.parametrize('rows', [[], [{'Name':'wg0','Protocol':'awg','Status':True}],
    [{'Name':'wg0','Protocol':'wg','Status':False}],
    [{'Name':'wg0','Protocol':'wg','Status':True}]*2, [None],
    [{'Name':'wg0','Protocol':'wg','Status':1}]])
def test_missing_disabled_ambiguous_or_unknown_interfaces_fail(rows):
    report, _ = probe({'status':True,'data':rows})
    assert report['status'] == 'needs_attention'


@pytest.mark.parametrize('status,code', [(401,'AuthenticationRejected'),(302,'RedirectRefused'),(500,'PanelHTTPError')])
def test_http_errors_do_not_expose_provider_details(status, code):
    report, calls = probe({'message':'secret-key'},status)
    assert report['error_code'] == code and len(calls) == 1
    assert 'secret-key' not in json.dumps(report)


@pytest.mark.parametrize('changes', [{'configuration':'../wg0'}, {'token':'bad\nkey'}, {'url':'http://public.example'}, {'extra':'no'}])
def test_invalid_profile_refuses_before_network(changes):
    def fail(**kwargs):pytest.fail('Network must not run')
    report = asyncio.run(s.probe(PROFILE | changes,client_factory=fail,wgdashboard=True))
    assert report['status'] == 'invalid_input'
