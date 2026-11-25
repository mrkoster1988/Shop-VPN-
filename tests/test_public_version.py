"""Verify deployed Shop identity without cookies, API tokens or cache staleness."""
from test_shop import client


def test_deployed_version_only_exposes_public_constant(client):
    response = client.get('/api/version')
    assert response.status_code == 200
    assert response.json() == {'version': 'v0.063', 'flow_binding': 'account'}
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert 'secret' not in response.text.lower()
    assert 'token' not in response.text.lower()
    assert 'client_id' not in response.text.lower()
    assert 'telegram_id' not in response.text.lower()
