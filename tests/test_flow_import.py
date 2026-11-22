import runpy
from pathlib import Path
import dotenv
import pytest
from fastapi.testclient import TestClient
from test_shop import client
from app import config as c
from app.main import create_app


def assert_private_landing(response):
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-frame-options'] == 'DENY'
    policy = response.headers['content-security-policy']
    assert "connect-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "script-src 'self'" in policy
    assert 'https://t.me/pushok_app' in response.text
    assert 'https://github.com/mrkoster1988/Pushok-Flow/releases/latest' in response.text
    assert 'id="flow-import-open"' in response.text
    assert 'flow-import.js' in response.text and 'flow-links.js' in response.text


def test_flow_landing_has_private_headers_and_requires_no_account(client):
    assert_private_landing(client.get('/static/flow-import.html'))
    # Browsers do not send fragments to the server; they cannot change the static response.
    response = client.get('/static/flow-import.html#url=https%3A%2F%2Fvpn.example%2Fsub%2Fprivate-token')
    assert_private_landing(response)
    assert 'private-token' not in response.text
    for asset in ('flow-links.js', 'flow-import.js', 'flow-import.css'):
        assert client.get('/static/' + asset).status_code == 200


def test_flow_landing_keeps_headers_and_assets_under_shop_prefix(client, monkeypatch):
    monkeypatch.setattr(c, 'BASE_PATH', '/shop')
    monkeypatch.setattr(c, 'PUBLIC_URL', 'http://testserver/shop')
    with TestClient(create_app('/shop')) as shop:
        assert_private_landing(shop.get('/shop/static/flow-import.html'))
        index = shop.get('/shop/').text
        assert index.index('src="/shop/static/flow-links.js"') < index.index('src="/shop/static/app.js"')
        assert 'id="flow-open" type="button" disabled' in index
        for asset in ('flow-links.js', 'flow-import.js', 'flow-import.css'):
            assert shop.get('/shop/static/' + asset).status_code == 200


@pytest.mark.parametrize('value, expected', [(None, 'pushok_app'), ('', 'pushok_app'), ('other_bot', 'other_bot')])
def test_default_bot_identity_without_overwriting_explicit_config(value, expected, tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda: None)
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'data'))
    if value is None:
        monkeypatch.delenv('BOT_USERNAME', raising=False)
    else:
        monkeypatch.setenv('BOT_USERNAME', value)
    values = runpy.run_path(str(Path(__file__).parents[1] / 'app/config.py'))
    assert values['BOT_USERNAME'] == expected
