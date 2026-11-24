"""First-run MTProxy edge deployment is isolated and never resets credentials."""
import importlib.util
import os
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_mtproxy_edge.py"
spec = importlib.util.spec_from_file_location("prepare_mtproxy_edge", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_edge_config_is_private_once_and_does_not_replace_it(tmp_path):
    assert setup.ensure_env(tmp_path) is True
    path = tmp_path / ".env"
    first = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"PUBLIC_URL=https://pushok.shop/shop\n" in first
    assert b"BACKGROUND_TASKS=true\n" in first
    assert b"BOT_TOKEN=\n" in first
    assert b"FLOW_SHOP_BRIDGE_SECRET=\n" in first
    assert b"ADMIN_TOKEN=\n" not in first
    assert b"ENCRYPTION_KEY=\n" not in first
    assert setup.ensure_env(tmp_path) is False
    assert path.read_bytes() == first


def test_isolated_compose_has_no_ports_or_host_network():
    manifest = (SCRIPT.parents[1] / "compose.mtproxy-edge.yaml").read_text()
    assert "network_mode: host" not in manifest
    assert "\n    ports:" not in manifest
    assert "mtproxy-pushok_edge" in manifest
    assert "pushok-shop:8000" in manifest
    assert "      - 0.0.0.0" in manifest
    assert "no-access-log" in manifest
    assert "shop_data:/app/data" in manifest
