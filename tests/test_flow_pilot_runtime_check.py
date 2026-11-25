"""Flow live preflight never mutates VPN state or exposes bridge secrets."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/flow_pilot_runtime_check.py"
spec = importlib.util.spec_from_file_location("flow_pilot_runtime_check", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def prepare(tmp_path):
    shop, mtproxy = tmp_path / "shop", tmp_path / "mtproxy"
    shop.mkdir()
    mtproxy.mkdir()
    (shop / ".env").write_text("FLOW_SHOP_BRIDGE_SECRET=" + "ab" * 32 +
                              "\nFLOW_TRIAL_MTPROXY_ONLY=true\n")
    (mtproxy / ".env").write_text("FLOW_SHOP_BRIDGE_SECRET=" + "ab" * 32 + "\n")
    (shop / "compose.wg-ip.yaml").write_text(
        'services:\n  shop:\n    extra_hosts:\n      - "wg.pushok.shop=193.238.155.39"\n'
    )
    return shop, mtproxy


def test_env_pair_guard_is_secret_free_and_detects_rotation(tmp_path):
    shop, mtproxy = prepare(tmp_path)
    assert mod.check_shared_secret(shop, mtproxy)
    assert mod.env_value(shop / ".env", "FLOW_TRIAL_MTPROXY_ONLY") == "true"
    (mtproxy / ".env").write_text("FLOW_SHOP_BRIDGE_SECRET=" + "ac" * 32 + "\n")
    assert mod.check_shared_secret(shop, mtproxy) is False
    (mtproxy / ".env").write_text("FLOW_SHOP_BRIDGE_SECRET=" + "ab" * 32 +
                                  "\nFLOW_SHOP_BRIDGE_SECRET=" + "ab" * 32 + "\n")
    with pytest.raises(mod.NotReady, match="duplicated"):
        mod.check_shared_secret(shop, mtproxy)


def test_overlay_symlink_rejected(tmp_path):
    shop, _ = prepare(tmp_path)
    overlay = shop / "compose.wg-ip.yaml"
    overlay.unlink()
    overlay.symlink_to(shop / ".env")
    assert not mod.configured_shop(shop)


def test_compose_gate_requires_private_port_and_persistent_volume(tmp_path, monkeypatch):
    shop, _ = prepare(tmp_path)
    payload = {
        "services": {
            "shop": {
                "ports": [],
                "extra_hosts": {"wg.pushok.shop": "193.238.155.39"},
                "volumes": [{"type": "volume", "source": "shop_data", "target": "/app/data"}],
            }
        },
        "volumes": {"shop_data": {"name": "pushok_shop_data"}}
    }
    results = []

    def fake_run(cmd, cwd=None, **kwargs):
        assert cwd == shop
        results.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.configured_shop(shop)
    assert results[0][:6] == ("docker", "compose", "-f", "compose.mtproxy-edge.yaml",
                              "-f", "compose.wg-ip.yaml")
    (shop / "compose.wg-ip.yaml").unlink()
    payload["services"]["shop"]["extra_hosts"] = {}
    assert mod.configured_shop(shop)
    assert "compose.wg-ip.yaml" not in results[-1]
    payload["services"]["shop"]["extra_hosts"] = ["wg.pushok.shop:192.0.2.1"]
    assert not mod.configured_shop(shop)
    assert mod.configured_shop(shop, "192.0.2.1")
    payload["services"]["shop"]["extra_hosts"] = []
    payload["services"]["shop"]["ports"] = [{"published": "8000"}]
    assert not mod.configured_shop(shop)
    payload["services"]["shop"]["ports"] = []
    payload["volumes"]["shop_data"]["name"] = "other_data"
    assert not mod.configured_shop(shop)


def test_failed_preflight_does_not_print_secrets_or_change_files(tmp_path, monkeypatch, capsys):
    shop, mtproxy = prepare(tmp_path)
    before = [(shop / ".env").read_bytes(), (mtproxy / ".env").read_bytes()]
    monkeypatch.setattr(mod, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=b""))
    code = mod.main(["--shop-dir", str(shop), "--mtproxy-dir", str(mtproxy)])
    assert code == 1
    output = capsys.readouterr().out
    assert "BLOCK" in output
    assert "ab" * 32 not in output
    assert "NOTE: No peer created" in output
    assert before == [(shop / ".env").read_bytes(), (mtproxy / ".env").read_bytes()]
