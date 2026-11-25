"""Verify deployed Shop identity without cookies, API tokens or cache staleness."""
from test_shop import client


def test_deployed_version_only_exposes_public_constant(client):
    response = client.get('/api/version')
    assert response.status_code == 200
    assert response.json() == {'version': 'v0.065', 'flow_binding': 'account'}
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert 'secret' not in response.text.lower()
    assert 'token' not in response.text.lower()
    assert 'client_id' not in response.text.lower()
    assert 'telegram_id' not in response.text.lower()


def test_v065_upgrade_scripts_accept_previous_release():
    """No version split between installed API, installer and two upgrade modes."""
    from scripts import install, upgrade
    from app import diagnostics
    from pathlib import Path
    assert diagnostics.VERSION == install.VERSION == upgrade.VERSION == 'v0.065'
    assert 'v0.064' in upgrade.SUPPORTED
    edge = Path(__file__).resolve().parents[1] / 'scripts' / 'upgrade_mtproxy_edge.py'
    assert 'default="v0.065"' in edge.read_text(encoding='utf-8')
