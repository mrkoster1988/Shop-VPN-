"""MTProxy -> Shop bootstrap must not reveal or silently rotate secrets."""
import importlib.util
import os
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "prepare_flow_bridge.py"
spec = importlib.util.spec_from_file_location("prepare_flow_bridge", SOURCE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def _paths(tmp_path, mtproxy="DB_KEY=mtproxy\n", shop="PUBLIC_URL=https://pushok.shop/shop\nFLOW_SHOP_BRIDGE_SECRET=\n"):
    left = tmp_path / "mtproxy.env"
    right = tmp_path / "shop.env"
    left.write_text(mtproxy)
    right.write_text(shop)
    return left, right, tmp_path / "private-backups"


def _key(path):
    return bridge._read(path)[1]


def test_dry_run_never_changes_files_or_generates_secret(tmp_path, monkeypatch):
    left, right, backup = _paths(tmp_path)
    monkeypatch.setattr(bridge.secrets, "token_hex", lambda count: (_ for _ in ()).throw(AssertionError("generated")))
    before = left.read_bytes(), right.read_bytes()
    result = bridge.prepare(left, right, backup)
    assert result == {"status": "needs_setup", "changed": False, "backup": None}
    assert not backup.exists()
    assert before == (left.read_bytes(), right.read_bytes())


def test_apply_adds_shared_key_with_backups_and_is_idempotent(tmp_path):
    left, right, backup = _paths(tmp_path)
    first = (left.read_bytes(), right.read_bytes())
    result = bridge.prepare(left, right, backup, True)
    assert result["changed"] and result["status"] == "ready"
    secret = _key(left)
    assert len(secret) == 64 and secret == _key(right)
    assert b'FLOW_TRIAL_MTPROXY_ONLY=true\n' in right.read_bytes()
    assert b'FLOW_TRIAL_MTPROXY_ONLY=' not in left.read_bytes()
    assert left.stat().st_mode & 0o777 == 0o600
    assert right.stat().st_mode & 0o777 == 0o600
    copies = Path(result["backup"])
    assert copies.stat().st_mode & 0o777 == 0o700
    assert (copies / "mtproxy.env").read_bytes() == first[0]
    assert (copies / "shop.env").read_bytes() == first[1]
    assert bridge.prepare(left, right, backup, True) == {
        "status": "ready", "changed": False, "backup": None
    }
    assert len(list(backup.iterdir())) == 1


def test_existing_key_reused_without_rotating(tmp_path):
    secret = "a1" * 32
    left, right, backup = _paths(tmp_path, mtproxy="FLOW_SHOP_BRIDGE_SECRET=" + secret + "\n")
    bridge.prepare(left, right, backup, True)
    assert _key(left) == secret == _key(right)


def test_conflicts_and_bad_env_never_write(tmp_path):
    secret = "a1" * 32
    left, right, backup = _paths(tmp_path, "FLOW_SHOP_BRIDGE_SECRET=" + secret + "\n",
                                     "FLOW_SHOP_BRIDGE_SECRET=" + "b2" * 32 + "\n")
    original = left.read_bytes(), right.read_bytes()
    with pytest.raises(bridge.BridgeSetupError, match="differ"):
        bridge.prepare(left, right, backup, True)
    assert original == (left.read_bytes(), right.read_bytes())
    assert not backup.exists()

    right.write_text("FLOW_SHOP_BRIDGE_SECRET=invalid\n")
    with pytest.raises(bridge.BridgeSetupError, match="invalid"):
        bridge.prepare(left, right, backup, True)

    right.write_text("FLOW_SHOP_BRIDGE_SECRET=\nFLOW_SHOP_BRIDGE_SECRET=\n")
    with pytest.raises(bridge.BridgeSetupError, match="duplicate"):
        bridge.prepare(left, right, backup, True)


def test_rejects_symlinks_and_nonexistent_files(tmp_path):
    left, right, backup = _paths(tmp_path)
    shortcut = tmp_path / "symlink.env"
    shortcut.symlink_to(left)
    with pytest.raises(bridge.BridgeSetupError, match="regular"):
        bridge.prepare(shortcut, right, backup, True)
    with pytest.raises(bridge.BridgeSetupError, match="regular"):
        bridge.prepare(tmp_path / "missing.env", right, backup, True)


def test_different_files_are_required(tmp_path):
    left, right, backup = _paths(tmp_path)
    with pytest.raises(bridge.BridgeSetupError, match="different"):
        bridge.prepare(left, left, backup, True)
