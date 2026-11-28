"""Verify deployed Shop identity without cookies, API tokens or cache staleness."""
from test_shop import client


def test_deployed_version_only_exposes_public_constant(client):
    response = client.get('/api/version')
    assert response.status_code == 200
    assert response.json() == {'version': 'v2.027', 'flow_binding': 'account'}
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    assert 'secret' not in response.text.lower()
    assert 'token' not in response.text.lower()
    assert 'client_id' not in response.text.lower()
    assert 'telegram_id' not in response.text.lower()


def test_v067_upgrade_scripts_accept_previous_release():
    """No version split between installed API, installer and two upgrade modes."""
    from scripts import install, upgrade
    from app import diagnostics
    from pathlib import Path
    assert diagnostics.VERSION == install.VERSION == upgrade.VERSION == 'v2.027'
    assert {'v2.026','v2.025','v2.024','v2.023','v2.022','v2.021','v0.067','v0.068','v2.002','v2.003','v2.004','v2.005','v2.006','v2.007'} <= upgrade.SUPPORTED
    edge = Path(__file__).resolve().parents[1] / 'scripts' / 'upgrade_mtproxy_edge.py'
    assert 'default="v2.027"' in edge.read_text(encoding='utf-8')
