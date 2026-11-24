#!/usr/bin/env python3
"""Read-only VPS gate for a single MTProxy -> Shop -> WGDashboard -> Flow pilot.

Never prints secrets, never changes Docker/DB/DNS, never creates WireGuard peers.
A PASS proves connectivity/readiness only; it does NOT prove peer creation,
on-device traffic, revocation or expiry enforcement.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

HOST = "wg.pushok.shop"
EXPECTED_IP = "45.90.56.23"
HEX_32_BYTES = re.compile(r"[0-9a-f]{64}\Z")
HOST_LINE = re.compile(r"(?m)^\s*-\s*['\"]?wg\.pushok\.shop=45\.90\.56\.23['\"]?\s*$")

PROBE = """
import os, socket, ssl, sys, urllib.request
try:
    address = socket.gethostbyname('wg.pushok.shop')
    if address != '45.90.56.23':
        sys.exit(10)
    with urllib.request.urlopen('https://wg.pushok.shop/', timeout=10,
                                context=ssl.create_default_context()) as res:
        if res.status not in (200, 401, 403):
            sys.exit(11)
    if not os.getenv('FLOW_SHOP_BRIDGE_SECRET'):
        sys.exit(12)
except Exception:
    sys.exit(13)
"""

MTPROXY_PROBE = """
import os, sys
key = os.environ.get('FLOW_SHOP_BRIDGE_SECRET', '')
sys.exit(0 if len(key) == 64 else 1)
"""


class NotReady(ValueError):
    """An invalid local prerequisite, without secrets in the message."""


def env_value(path: Path, name: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise NotReady("missing or symlinked env")
    source = path.read_text(encoding="utf-8")
    if len(source) > 256 * 1024:
        raise NotReady("oversized env")
    matches = re.findall(r"(?m)^[ \t]*(?:export[ \t]+)?" + re.escape(name) +
                         r"[ \t]*=[ \t]*([^\r\n]*)", source)
    if len(matches) != 1:
        raise NotReady("missing or duplicated env setting")
    return matches[0].strip()


def check_shared_secret(shop: Path, mtproxy: Path) -> bool:
    left = env_value(shop / ".env", "FLOW_SHOP_BRIDGE_SECRET")
    right = env_value(mtproxy / ".env", "FLOW_SHOP_BRIDGE_SECRET")
    return bool(HEX_32_BYTES.fullmatch(left) and HEX_32_BYTES.fullmatch(right)
                and left == right)


def overlay_has_ip(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    return bool(HOST_LINE.search(path.read_text(encoding="utf-8")))


def run(*args, timeout=25):
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False)


def configured_shop(shop: Path) -> bool:
    cmd = ("docker", "compose", "-f", "compose.mtproxy-edge.yaml",
           "-f", "compose.wg-ip.yaml", "-p", "pushok-shop",
           "config", "--format", "json")
    response = run(*cmd, timeout=35) if shop == Path.cwd() else subprocess.run(
        cmd, cwd=shop, capture_output=True, timeout=35, check=False)
    if response.returncode:
        return False
    # Compose JSON may contain resolved secrets; never display it.
    try:
        config = json.loads(response.stdout)
        service = config["services"]["shop"]
        if service.get("ports"):
            return False  # Must stay behind existing MTProxy Caddy.
        hosts = service.get("extra_hosts", {})
        if isinstance(hosts, dict):
            correct = hosts.get(HOST) == EXPECTED_IP
        elif isinstance(hosts, list):
            correct = any(item in (f"{HOST}={EXPECTED_IP}", f"{HOST}:{EXPECTED_IP}")
                          for item in hosts)
        else:
            correct = False
        mounts = service.get("volumes", [])
        persistent = any(mount.get("target") == "/app/data" and
                         mount.get("type") == "volume" and
                         mount.get("source") == "pushok_shop_data"
                         for mount in mounts if isinstance(mount, dict))
        return correct and persistent
    except (ValueError, TypeError, KeyError):
        return False


def result(label: str, ok: bool) -> None:
    print(("PASS " if ok else "BLOCK ") + label)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-dir", type=Path, default=Path("/opt/pushok-shop"))
    parser.add_argument("--mtproxy-dir", type=Path, default=Path("/opt/mtproxy-pushok"))
    args = parser.parse_args(argv)
    shop, mtproxy = args.shop_dir, args.mtproxy_dir
    checks = []
    try:
        shared = check_shared_secret(shop, mtproxy)
    except (NotReady, OSError, UnicodeError):
        shared = False
    checks.append(("same private bridge secret in .env files", shared))

    try:
        pilot = env_value(shop / ".env", "FLOW_TRIAL_MTPROXY_ONLY") == "true"
    except (NotReady, OSError, UnicodeError):
        pilot = False
    checks.append(("MTProxy-only trial guard enabled", pilot))

    try:
        overlay = overlay_has_ip(shop / "compose.wg-ip.yaml")
        composed = overlay and configured_shop(shop)
    except (OSError, UnicodeError, subprocess.SubprocessError, ValueError):
        composed = False
    checks.append(("Shop Compose overlay keeps WG IP, volume and private port", composed))

    migrated_file = (mtproxy / "database/076_flow_shop_rate_window.sql").is_file()
    checks.append(("MTProxy 076 migration present in checked-out release", migrated_file))

    try:
        health = run("docker", "inspect", "--format", "{{.State.Health.Status}}",
                     "pushok-shop").stdout.strip() == b"healthy"
        shop_probe = (run("docker", "exec", "pushok-shop", "python", "-c", PROBE,
                          timeout=25).returncode == 0)
    except (OSError, subprocess.SubprocessError):
        health = shop_probe = False
    checks.append(("Shop container healthy", health))
    checks.append(("Shop container resolves pinned WG IP, TLS and bridge", shop_probe))

    try:
        mtproxy_probe = (run("docker", "exec", "mtproxy-pushok-api-1", "python",
                             "-c", MTPROXY_PROBE).returncode == 0)
    except (OSError, subprocess.SubprocessError):
        mtproxy_probe = False
    checks.append(("live MTProxy API received bridge secret", mtproxy_probe))

    for label, ok in checks:
        result(label, ok)
    print("NOTE: No peer created; WGDashboard expiry, Android handshake and subscription"
          " still require a single-user acceptance test.")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
