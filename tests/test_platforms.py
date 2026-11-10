"""Public platform inventory and fail-closed driver tests."""
import asyncio
import pytest
from app import config as c, platforms, providers
from test_shop import client


def test_platform_catalog_is_explicit_and_does_not_leak_secret(monkeypatch):
    secret = 'private_token_must_not_appear'
    monkeypatch.setattr(c, 'BOT_TOKEN', secret)
    monkeypatch.setattr(c, 'BOT_USERNAME', 'pushok_test_bot')
    result = platforms.public_catalog()
    assert result['account_linking'] == 'disabled'
    assert result['external_checks_performed'] is False
    channels = {p['id']: p for p in result['platforms']}
    assert set(channels) == {'web', 'telegram', 'vk', 'max', 'matrix'}
    assert channels['telegram']['configured'] is True
    assert channels['telegram']['connection_verified'] is False
    assert secret not in str(result)
    for name in ('vk', 'max', 'matrix'):
        assert channels[name]['implementation'] == 'planned'
        assert channels[name]['configured'] is False
        assert channels[name]['enabled_for_sales'] is False


def test_public_platform_api_reports_without_side_effects(client):
    response = client.get('/api/platforms')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['platforms'][1]['configured'] is False
    assert client.post('/api/platforms', json={}).status_code == 405


@pytest.mark.parametrize('protocol', ['matrix', 'vk', 'max', '', None])
def test_unknown_protocol_never_contacts_panel_or_wireguard(monkeypatch, protocol):
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected external provider access')
    monkeypatch.setattr(providers.servers, 'decode', forbidden)
    monkeypatch.setattr(providers.servers, 'for_subscription', forbidden)
    monkeypatch.setattr(providers.httpx, 'AsyncClient', forbidden)
    payload = {'protocol': protocol, 'id': 'some-id', 'expires': 2000000000}
    with pytest.raises(ValueError, match='UnsupportedVpnProtocol'):
        asyncio.run(providers.provision(payload))
    with pytest.raises(ValueError, match='UnsupportedVpnProtocol'):
        asyncio.run(providers.disable(payload))
    with pytest.raises(ValueError, match='UnsupportedVpnProtocol'):
        asyncio.run(providers.probe_server(payload))
