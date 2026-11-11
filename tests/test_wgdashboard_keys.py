import uuid
import pytest
from test_shop import client
from app.wgdashboard import stored_private_key, WGDashboard
from app.panel import PanelError
from app.db import one, init_db


def test_key_survives_repeated_calls_and_migration_without_plaintext(client):
    identity = str(uuid.uuid4())
    key = stored_private_key('test-wg', identity)
    assert WGDashboard.identity(key)
    encrypted = one('SELECT private_key FROM wgdashboard_keys')['private_key']
    assert encrypted != key and key not in encrypted
    init_db()
    assert stored_private_key('test-wg', identity) == key
    assert stored_private_key('test-wg', str(uuid.uuid4())) != key


@pytest.mark.parametrize('server,identity',[('test-xui',str(uuid.uuid4())),('missing',str(uuid.uuid4())),('test-wg','bad')])
def test_invalid_binding_does_not_create_key(client,server,identity):
    with pytest.raises(PanelError):stored_private_key(server,identity)
    assert one('SELECT count(*) AS n FROM wgdashboard_keys')['n'] == 0
