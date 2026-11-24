#!/usr/bin/env python3
"""Safely pair MTProxy and Shop for Flow without touching Pushok ID.

Default mode only inspects two .env files. --apply backs them up and stores
one 32-byte shared signing key. It does NOT restart a container, call an API,
enable 30-day trial access, print a secret, or modify Caddy/Pushok ID.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

NAME = "FLOW_SHOP_BRIDGE_SECRET"
KEY = re.compile(r"[0-9a-f]{64}\Z")
MENTION = re.compile(r"(?m)^\s*(?:export\s+)?" + NAME + r"\s*=")
ASSIGNMENT = re.compile(
    r"(?m)^(?P<before>[ \t]*" + NAME + r"[ \t]*=[ \t]*)"
    r"(?P<value>[^\r\n]*)(?P<ending>\r?\n|$)"
)


class BridgeSetupError(ValueError):
    pass


def _read(path: Path):
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise BridgeSetupError("Expected an existing regular .env file (not a symlink)")
    data = path.read_bytes()
    if len(data) > 256 * 1024 or b"\x00" in data:
        raise BridgeSetupError("Unexpected .env contents")
    try:
        source = data.decode("utf-8")
    except UnicodeError as exc:
        raise BridgeSetupError("Invalid .env encoding") from exc
    matches = list(ASSIGNMENT.finditer(source))
    if len(matches) > 1 or len(list(MENTION.finditer(source))) != len(matches):
        raise BridgeSetupError("Ambiguous or duplicate FLOW_SHOP_BRIDGE_SECRET setting")
    value = matches[0].group("value").strip() if matches else ""
    if value and not KEY.fullmatch(value):
        raise BridgeSetupError("Existing bridge key has an invalid format")
    return source, value, matches[0] if matches else None


def _replace(source: str, match, value: str) -> str:
    if match is not None:
        return source[:match.start("value")] + value + source[match.end("value"):]
    if source and not source.endswith("\n"):
        source += "\n"
    return source + NAME + "=" + value + "\n"


def _write_atomic(path: Path, content: bytes):
    fd, temporary = tempfile.mkstemp(prefix=".flow-secret-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save_backup(backup_root: Path, files):
    if backup_root.is_symlink():
        raise BridgeSetupError("Backup directory must not be a symlink")
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backup_root.stat().st_mode & 0o077:
        raise BridgeSetupError("Backup directory permissions are too broad")
    backup_dir = Path(tempfile.mkdtemp(prefix="flow-bridge-", dir=backup_root))
    for label, _, source, _, _ in files:
        target = backup_dir / (label + ".env")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(source.encode("utf-8"))
    return backup_dir


def prepare(mtproxy: Path, shop: Path, backup_root: Path, apply: bool = False):
    if mtproxy.resolve() == shop.resolve():
        raise BridgeSetupError("MTProxy and Shop must use different .env files")
    files = []
    for label, path in (("mtproxy", mtproxy), ("shop", shop)):
        source, existing, match = _read(path)
        files.append((label, path, source, existing, match))
    distinct = {existing for _, _, _, existing, _ in files if existing}
    if len(distinct) > 1:
        raise BridgeSetupError("Bridge secrets differ; refusing automatic rotation")
    # A dry run never generates or reveals a secret.
    if not apply:
        return {"status": "ready" if len(distinct) == 1 and all(f[3] for f in files) else "needs_setup",
                "changed": False, "backup": None}
    value = next(iter(distinct), None) or secrets.token_hex(32)
    planned = [(label, path, source, _replace(source, match, value))
               for label, path, source, existing, match in files
               if existing != value or match is None]
    if not planned:
        return {"status": "ready", "changed": False, "backup": None}
    backup = _save_backup(backup_root, files)
    changed = []
    try:
        for label, path, source, updated in planned:
            _write_atomic(path, updated.encode("utf-8"))
            changed.append((path, source))
        # Validate both files after writing, without logging their values.
        if {_read(mtproxy)[1], _read(shop)[1]} != {value}:
            raise BridgeSetupError("Bridge key verification failed")
    except Exception:
        for path, source in reversed(changed):
            _write_atomic(path, source.encode("utf-8"))
        raise
    return {"status": "ready", "changed": True, "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mtproxy-env", type=Path, default=Path("/opt/mtproxy-pushok/.env"))
    parser.add_argument("--shop-env", type=Path, default=Path("/opt/pushok-shop/.env"))
    parser.add_argument("--backup-root", type=Path, default=Path("/root/pushok-flow-bridge-backups"))
    parser.add_argument("--apply", action="store_true",
                        help="Back up both files and write a shared secret; never restart services")
    args = parser.parse_args()
    try:
        result = prepare(args.mtproxy_env, args.shop_env, args.backup_root, args.apply)
    except (BridgeSetupError, OSError) as exc:
        print("BLOCKED:", type(exc).__name__, str(exc))
        return 1
    if not args.apply:
        print("DRY RUN:", result["status"], "| no files changed, no secrets printed")
    else:
        print("Bridge env:", result["status"],
              "| changed" if result["changed"] else "| already configured")
        if result["backup"]:
            print("Backups:", result["backup"])
        print("Containers NOT restarted. Free 30-day issuance still disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
