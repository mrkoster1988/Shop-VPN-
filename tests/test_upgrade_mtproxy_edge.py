"""Regression tests for non-installer Shop deployment beside MTProxy."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("upgrade_mtproxy_edge", SCRIPTS / "upgrade_mtproxy_edge.py")
edge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge)

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".env").write_text("ENCRYPTION_KEY=synthetic\n")
    (tmp_path / ".env").chmod(0o600)
    (tmp_path / "compose.mtproxy-edge.yaml").write_text("services: {}\n")
    (tmp_path / "compose.wg-ip.yaml").write_text("services: {}\n")
    return tmp_path


def simulated_compose():
    return {
        "services": {
            "shop": {
                "image": edge.IMAGE,
                "container_name": edge.CONTAINER,
                "volumes": [{"type": "volume", "source": edge.VOLUME, "target": "/app/data"}],
            }
        }
    }


def simulated_container(root, *, mount="pushok_shop_data"):
    return [{
        "Id": "f" * 64,
        "Image": OLD,
        "Config": {"Labels": {
            "com.docker.compose.project": "pushok-shop",
            "com.docker.compose.service": "shop",
            "com.docker.compose.project.config_files":
                str(root / "compose.mtproxy-edge.yaml") + "," + str(root / "compose.wg-ip.yaml"),
        }},
        "Mounts": [{"Type": "volume", "Destination": "/app/data", "Name": mount, "RW": True}],
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
    }]


def patch_probe(monkeypatch, root, *, compose=None, mount="pushok_shop_data", label_config=None):
    conf = compose if compose is not None else simulated_compose()
    container = simulated_container(root, mount=mount)
    if label_config is not None:
        container[0]["Config"]["Labels"]["com.docker.compose.project.config_files"] = label_config

    def fake_json(*args):
        if args[-3:] == ("config", "--format", "json"):
            return conf
        if args == ("inspect", edge.CONTAINER):
            return container
        raise AssertionError(args)

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return OLD
        raise AssertionError(command)

    monkeypatch.setattr(edge, "docker_json", fake_json)
    monkeypatch.setattr(edge, "run", fake_run)
    monkeypatch.setattr(edge, "internal", lambda path: (
        {"status": "ok"} if path.endswith("/health") else {"version": "v0.062"}
    ))


def test_probe_accepts_local_wg_overlay_without_installer_metadata(root, monkeypatch):
    patch_probe(monkeypatch, root)
    assert not (root / "installer.json").exists()
    assert not (root / "compose.install.json").exists()
    result = edge.probe(root)
    assert result["runtime_version"] == "v0.062"
    assert result["volume"] == "pushok_shop_data"
    assert result["compose_files"] == ["compose.mtproxy-edge.yaml", "compose.wg-ip.yaml"]
    assert ".env" in result["fingerprint"]
    assert "ENCRYPTION_KEY" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["extra_service", "port", "host_network", "wrong_image"])
def test_probe_rejects_unsafe_compose(root, monkeypatch, mutation):
    conf = simulated_compose()
    shop = conf["services"]["shop"]
    if mutation == "extra_service":
        conf["services"]["db"] = {}
    elif mutation == "port":
        shop["ports"] = [{"target": 8000, "published": "8000"}]
    elif mutation == "host_network":
        shop["network_mode"] = "host"
    else:
        shop["image"] = "someone-else:latest"
    patch_probe(monkeypatch, root, compose=conf)
    with pytest.raises(edge.Rejected):
        edge.probe(root)


def test_probe_rejects_wrong_data_volume_and_different_deployment(root, monkeypatch):
    patch_probe(monkeypatch, root, mount="temporary_wrong_volume")
    with pytest.raises(edge.Rejected, match="Том"):
        edge.probe(root)
    patch_probe(monkeypatch, root, label_config=str(root / "compose.yaml"))
    with pytest.raises(edge.Rejected, match="Compose"):
        edge.probe(root)


def test_probe_requires_private_env_and_existing_files(root, monkeypatch):
    patch_probe(monkeypatch, root)
    (root / ".env").chmod(0o644)
    with pytest.raises(edge.Rejected, match="chmod 600"):
        edge.probe(root)
    (root / ".env").chmod(0o600)
    (root / "compose.wg-ip.yaml").unlink()
    patch_probe(monkeypatch, root, label_config=str(root / "compose.mtproxy-edge.yaml"))
    assert edge.probe(root)["compose_files"] == ["compose.mtproxy-edge.yaml"]


def test_source_revision_cannot_change_under_upgrade(root):
    before = {"container_id": "first", "image_id": OLD, "volume": edge.VOLUME,
              "fingerprint": {"local": "old"}, "compose_files": ["compose.mtproxy-edge.yaml"]}
    after = {**before, "fingerprint": {"local": "changed"}}
    with pytest.raises(edge.Rejected, match="изменилась"):
        edge.unchanged(before, after)
    edge.unchanged(before, before)


def test_password_file_permissions(root):
    password = root / "pass"
    password.write_text("1234567890abcdef\n")
    password.chmod(0o644)
    with pytest.raises(edge.Rejected, match="0600"):
        edge.secret_password(str(password))
    password.chmod(0o600)
    assert edge.secret_password(str(password)) == "1234567890abcdef"


def test_unfinished_switch_blocks_any_new_attempt(root, monkeypatch):
    monkeypatch.setattr(edge, "status", lambda _: {"state": "recovery_required"})
    monkeypatch.setattr(edge, "probe", lambda _: (_ for _ in ()).throw(AssertionError("must not probe")))
    with pytest.raises(edge.Rejected, match="незавершённое"):
        edge.apply(root, "v0.063", None)


def test_apply_builds_before_backup_and_only_recreates_shop(root, monkeypatch):
    before = {"container_id": "old", "image_id": OLD, "volume": edge.VOLUME,
              "fingerprint": {"env": "sha"}, "compose_files": ["compose.mtproxy-edge.yaml", "compose.wg-ip.yaml"],
              "runtime_version": "v0.062"}
    after = {**before, "container_id": "new", "image_id": NEW, "runtime_version": "v0.063"}
    probes = iter([before, before, before, after])
    events = []

    monkeypatch.setattr(edge, "status", lambda _: {"state": "no_edge_upgrade"})
    monkeypatch.setattr(edge, "probe", lambda _: next(probes))
    monkeypatch.setattr(edge.update_source, "fetch", lambda version, work: (
        events.append("download") or root, "e" * 40
    ))
    monkeypatch.setattr(edge, "build", lambda source, tag, log: events.append("build") or NEW)
    monkeypatch.setattr(edge, "secret_password", lambda _: "strong-synthetic-password")
    monkeypatch.setattr(edge, "encrypted_backup", lambda *args: events.append("backup") or "0" * 64)

    def record(command, **kwargs):
        events.append(command)
        return ""
    monkeypatch.setattr(edge, "run", record)
    result = edge.apply(root, "v0.063", None)
    assert result["state"] == "complete"
    assert events[:3] == ["download", "build", "backup"]
    docker_calls = [x for x in events if isinstance(x, list)]
    assert len(docker_calls) == 3
    assert docker_calls[0][:2] == ["docker", "tag"]
    assert docker_calls[1][:2] == ["docker", "tag"]
    assert docker_calls[2][:3] == ["docker", "compose", "--project-name"]
    assert "--no-deps" in docker_calls[2] and "--no-build" in docker_calls[2]
    assert "--force-recreate" in docker_calls[2] and docker_calls[2][-1] == "shop"
    assert all("down" not in x for x in docker_calls)
    jobs = list((root / "edge-upgrades").glob("*/state.json"))
    assert len(jobs) == 1
    assert json.loads(jobs[0].read_text())["phase"] == "complete"


def test_no_confirm_does_not_invoke_docker(root, monkeypatch):
    monkeypatch.setattr(edge, "probe", lambda *_: (_ for _ in ()).throw(AssertionError("probe")))
    with pytest.raises(edge.Rejected, match="--confirm-edge-upgrade"):
        edge.main(["apply", "--dir", str(root)])
