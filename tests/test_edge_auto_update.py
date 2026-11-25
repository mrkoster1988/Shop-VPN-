"""Unattended MTProxy-edge updates must never weaken release/backup gates."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("edge_auto_update", HERE / "edge_auto_update.py")
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)


def rel(tag, **kwargs):
    value = dict(tag_name=tag, draft=False, prerelease=False,
                 published_at="2026-11-25T09:30:00Z")
    value.update(kwargs)
    return value


def test_discovers_only_next_stable_release_in_numerical_order():
    releases = [rel("v0.067"), rel("v0.064"), rel("v0.066"), rel("v0.065"),
                dict(tag_name="v0.064", draft=True, prerelease=False, published_at=None)]
    assert auto.discover(releases, "v0.063") == "v0.064"
    assert auto.discover(releases, "v0.064") == "v0.065"
    assert auto.discover(releases, "v0.067") is None


def test_prevents_skipped_migrations_and_invalid_releases():
    with pytest.raises(auto.edge.Rejected, match="Пропущена"):
        auto.discover([rel("v0.067")], "v0.063")
    with pytest.raises(auto.edge.Rejected, match="Пропущена"):
        auto.discover([rel("v1.000")], "v0.999")
    with pytest.raises(auto.edge.Rejected):
        auto.discover({"tag_name": "v0.064"}, "v0.063")
    with pytest.raises(auto.edge.Rejected):
        auto.discover([rel("v0.064")] * 31, "v0.063")
    assert auto.discover([rel("v0.063")], "v0.063") is None
    assert auto.discover([dict(tag_name="v0.064", draft=False, prerelease=True,
                               published_at="today")], "v0.063") is None


def test_plan_fails_on_unhealthy_or_unfinished_installation(tmp_path, monkeypatch):
    root = auto.ROOT
    monkeypatch.setattr(auto, "check_root", lambda *a, **k: root)
    monkeypatch.setattr(auto.edge, "probe", lambda _: {"runtime_version": "v0.063", "volume": auto.edge.VOLUME})
    monkeypatch.setattr(auto.edge, "status", lambda _: {"state": "recovery_required"})
    with pytest.raises(auto.edge.Rejected, match="Незавершённое"):
        auto.plan(root, fetch=lambda *args: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(auto.edge, "status", lambda _: {"state": "complete"})
    assert auto.plan(root, fetch=lambda *args: [rel("v0.064")])["next_release"] == "v0.064"
    assert auto.plan(root, fetch=lambda *args: [])["state"] == "current"


def test_execute_verifies_official_tag_and_applies_once(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "guarded_state", lambda: tmp_path / "password")
    monkeypatch.setattr(auto, "process_lock", lambda: _NoopContext())
    monkeypatch.setattr(auto, "plan", lambda *a, **kw: {
        "state": "upgrade_available", "installed": "v0.063", "next_release": "v0.064"})
    actions = []
    monkeypatch.setattr(auto.update_source, "resolve", lambda tag: actions.append(("resolve", tag)))
    monkeypatch.setattr(auto, "save_result", lambda x: actions.append(("save", x["state"])))
    result = auto.execute(auto.ROOT, apply=lambda root, tag, password: (
        actions.append(("apply", tag, password)) or
        {"state": "complete", "container_restarted": True, "operation": "op"}))
    assert result == {"state": "complete", "version": "v0.064", "restarted": True}
    assert actions == [
        ("resolve", "v0.064"), ("apply", "v0.064", str(auto.PASSWORD)), ("save", "complete"),
    ]


class _NoopContext:
    def __enter__(self): return None
    def __exit__(self, *args): return False


def test_execute_stays_idle_without_invoking_release_resolver(monkeypatch):
    monkeypatch.setattr(auto, "guarded_state", lambda: None)
    monkeypatch.setattr(auto, "process_lock", lambda: _NoopContext())
    monkeypatch.setattr(auto, "plan", lambda *a, **kw: {
        "state": "current", "installed": "v0.064", "next_release": None})
    monkeypatch.setattr(auto.update_source, "resolve", lambda tag: (_ for _ in ()).throw(AssertionError(tag)))
    monkeypatch.setattr(auto, "save_result", lambda x: None)
    assert auto.execute(auto.ROOT)["state"] == "current"


def test_installer_never_overwrites_foreign_systemd_units(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    root.mkdir()
    sysroot = tmp_path / "systemd"
    sysroot.mkdir()
    existing = sysroot / "pushok-shop-edge-autoupdate.service"
    existing.write_text("[Service]\nExecStart=/unrelated\n")
    monkeypatch.setattr(auto, "ROOT", root)
    monkeypatch.setattr(auto, "UNIT", existing)
    monkeypatch.setattr(auto, "TIMER", sysroot / "pushok-shop-edge-autoupdate.timer")
    monkeypatch.setattr(auto, "STATE", tmp_path / "config" / "shop")
    monkeypatch.setattr(auto, "PASSWORD", auto.STATE / "backup-password")
    monkeypatch.setattr(auto, "check_root", lambda *a, **k: root)
    monkeypatch.setattr(auto, "plan", lambda *a, **k: {"state": "current"})
    monkeypatch.setattr(auto.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("systemctl must not run")))
    with pytest.raises(auto.edge.Rejected, match="Чужой"):
        auto.install(root)
    assert existing.read_text() == "[Service]\nExecStart=/unrelated\n"


def test_generated_service_never_calls_down_or_prints_password():
    service = auto.unit_contents()
    timer = auto.timer_contents()
    assert "ExecStart=/usr/bin/python3 /opt/pushok-shop/scripts/edge_auto_update.py run" in service
    assert "down" not in service
    assert "pushok_shop_data" not in service
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "backup-password" not in service


def test_private_password_file_must_not_be_world_readable(tmp_path):
    p = tmp_path / "password"
    p.write_text("test")
    p.chmod(0o644)
    with pytest.raises(auto.edge.Rejected):
        auto.only_regular_private(p)
    p.chmod(0o600)
    assert auto.only_regular_private(p) == p
