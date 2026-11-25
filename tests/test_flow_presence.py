import hashlib
import hmac

import pytest
from fastapi import HTTPException

from app import flow_presence as presence


def test_status_requires_scoped_fresh_proof(monkeypatch):
    key = 'ab' * 32
    monkeypatch.setenv('FLOW_SHOP_BRIDGE_SECRET', key)
    monkeypatch.setattr(presence.time, 'time', lambda: 1800000000)
    def proof(ts, scope):
        sig = hmac.new(bytes.fromhex(key), f'{scope}:{ts}'.encode(), hashlib.sha256).hexdigest()
        return f'{ts}:{sig}'
    presence.authorize(proof(1800000000, presence.DOMAIN))
    for invalid in (None, proof(1799999900, presence.DOMAIN), proof(1800000000, 'different-scope')):
        with pytest.raises(HTTPException) as error:
            presence.authorize(invalid)
        assert error.value.status_code == 401


def test_handshake_and_traffic_are_observed_from_client_perspective():
    peer = {'latest_handshake':'0:02:59', 'total_receive':1, 'cumu_receive':0.5,
            'total_sent':0.25, 'cumu_sent':0, 'private_key':'unexportable'}
    assert presence.metrics(peer, 1000) == {'last_seen':821, 'online':True,
        'received':2**28,'sent':3*2**29}
    assert presence.metrics({'latest_handshake':'0:03:00'},1000)['online'] is False
    assert presence.metrics({'latest_handshake':'1 day, 0:00:00'},100000)['last_seen'] == 13600
    assert presence.metrics({},1000) == dict(online=None,last_seen=None,sent=None,received=None)
