"""Read-only Flow route preflight checks, with no external network."""
import importlib.util
import io
from pathlib import Path

script = Path(__file__).resolve().parents[1] / "scripts" / "flow_public_preflight.py"
spec = importlib.util.spec_from_file_location("flow_public_preflight", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response:
    status = 200
    headers = {"Content-Type": "application/json; charset=utf-8"}

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return None

    def read(self, _limit):
        return self.data


class Opener:
    def __init__(self, data):
        self.data = data
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 8
        assert request.get_method() == "GET"
        assert not request.has_header("Authorization")
        return Response(self.data)


def test_flow_capabilities_and_shop_health_shapes():
    mtproxy = Opener(b'{"protocol_version":1,"session_ttl_seconds":43200}')
    assert module.check("/api/v1/flow/capabilities", "mtproxy", mtproxy)[0]
    assert mtproxy.requests[0].full_url == "https://pushok.shop/api/v1/flow/capabilities"
    shop = Opener(b'{"status":"ok"}')
    assert module.check("/shop/health", "shop", shop)[0]
    assert shop.requests[0].full_url == "https://pushok.shop/shop/health"


def test_preflight_does_not_accept_html_or_unexpected_api():
    assert not module.check("/api/v1/flow/capabilities", "mtproxy",
                            Opener(b'{"protocol_version":2,"session_ttl_seconds":43200}'))[0]
    assert not module.check("/shop/health", "shop", Opener(b'{"status":"error"}'))[0]
    assert not module.check("/shop/health", "shop", Opener(b'not-json'))[0]
    assert not module.check("/shop/health", "shop", Opener(b'X' * 4097))[0]
