#!/usr/bin/env python3
"""Unattended, fail-closed updater for the existing MTProxy-edge Shop container.

This is *not* the installer-backed host updater. It only invokes the audited
upgrade_mtproxy_edge path and never touches MTProxy, Caddy, ID or WGDashboard.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

import update_source
import upgrade_mtproxy_edge as edge

ROOT = Path("/opt/pushok-shop")
STATE = Path("/etc/pushok/shop-edge-autoupdate")
PASSWORD = STATE / "backup-password"
UNIT = Path("/etc/systemd/system/pushok-shop-edge-autoupdate.service")
TIMER = Path("/etc/systemd/system/pushok-shop-edge-autoupdate.timer")
MARK = "# Managed by Pushok edge_auto_update.py; do not edit manually."
URL = update_source.API + "/releases?per_page=30"
ALLOWED_PREVIOUS = {"no_edge_upgrade", "complete", "already_current"}


def version(tag):
    return update_source.version(tag)


def discover(releases, current):
    """Select at most ONE consecutive stable release, never skip schema steps."""
    if not isinstance(releases, list) or len(releases) > 30:
        raise edge.Rejected("Неожиданный ответ GitHub Releases")
    cur = version(current)
    candidates = []
    for release in releases:
        if not isinstance(release, dict):
            raise edge.Rejected("Некорректная запись релиза")
        tag = release.get("tag_name")
        try:
            candidate = version(tag)
        except ValueError:
            continue  # Other projects/tags are not Shop versions.
        if release.get("draft") is False and release.get("prerelease") is False and release.get("published_at"):
            if candidate > cur:
                candidates.append((candidate, tag))
    if not candidates:
        return None
    ordered = sorted(set(candidates))
    selected, tag = ordered[0]
    if selected[0] != cur[0] or selected[1] != cur[1] + 1:
        raise edge.Rejected("Пропущена версия Shop. Нужна ручная проверка миграций")
    return tag


def only_regular_private(path, expected_mode=None):
    if path.is_symlink() or not path.is_file():
        raise edge.Rejected("Недопустимый файл обновлятора: " + path.name)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077 or (expected_mode is not None and mode != expected_mode):
        raise edge.Rejected("Небезопасные права файла: " + path.name)
    return path


def guarded_state():
    if STATE.is_symlink() or not STATE.is_dir() or STATE.stat().st_mode & 0o077:
        raise edge.Rejected("Каталог системного обновлятора должен иметь права 0700")
    return only_regular_private(PASSWORD)


@contextmanager
def process_lock():
    fd = os.open(STATE / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise edge.Rejected("Другое автоматическое обновление Shop выполняется") from exc
        yield
    finally:
        os.close(fd)


def write_private(path, raw):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pushok-auto-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_result(result):
    # Last-run state contains only public version and coarse operation status.
    if STATE.is_dir():
        try:
            write_private(STATE / "last-result.json",
                          (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
        except OSError:
            pass


def check_root(root, require_owner=True):
    if root != ROOT or root.is_symlink() or not root.is_dir():
        raise edge.Rejected("Поддерживается только /opt/pushok-shop")
    if require_owner and os.geteuid() != 0:
        raise edge.Rejected("Нужны права root")
    for script in ("upgrade_mtproxy_edge.py", "update_source.py"):
        edge.required_file(root / "scripts" / script)
    return root


def plan(root, fetch=update_source.download):
    check_root(root, require_owner=False)
    before = edge.probe(root)
    if not before["runtime_version"]:
        raise edge.Rejected("Невозможно определить установленную версию Shop")
    prior = edge.status(root)
    if prior["state"] not in ALLOWED_PREVIOUS:
        raise edge.Rejected("Незавершённое обновление Shop требует ручной диагностики")
    latest = discover(fetch(URL, 2 * 1024 * 1024), before["runtime_version"])
    return {"state": "upgrade_available" if latest else "current",
            "installed": before["runtime_version"], "next_release": latest,
            "volume": before["volume"], "scope": "shop_only"}


def execute(root, *, fetch=update_source.download, apply=edge.apply):
    guarded_state()
    with process_lock():
        before = plan(root, fetch=fetch)
        tag = before["next_release"]
        if tag is None:
            save_result({"at": datetime.now(timezone.utc).isoformat(), "state": "current",
                         "version": before["installed"]})
            return {"state": "current", "installed": before["installed"]}
        # Verify this release's *actual official git tag* before any backup,
        # Docker command or source code execution. apply verifies it again.
        update_source.resolve(tag)
        result = apply(root, tag, str(PASSWORD))
        if result.get("state") not in {"complete", "already_current"}:
            raise edge.Rejected("Обновление завершилось без подтверждения")
        save_result({"at": datetime.now(timezone.utc).isoformat(), "state": "complete",
                     "version": tag, "operation": result.get("operation")})
        return {"state": "complete", "version": tag, "restarted": result.get("container_restarted")}


def unit_contents():
    return MARK + "\n" + """[Unit]
Description=Pushok Shop safe unattended edge release updater
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service
ConditionPathExists=/opt/pushok-shop/scripts/edge_auto_update.py

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/pushok-shop
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=3600
ExecStart=/usr/bin/python3 /opt/pushok-shop/scripts/edge_auto_update.py run
"""


def timer_contents():
    return MARK + "\n" + """[Unit]
Description=Check published Pushok Shop release hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true
Unit=pushok-shop-edge-autoupdate.service

[Install]
WantedBy=timers.target
"""


def install(root, *, run_now=False):
    check_root(root)
    plan(root)  # Fail before writing any unit or secret if the Shop deployment is unsafe.
    for path in (UNIT, TIMER):
        if path.is_symlink() or (path.exists() and
              (not path.is_file() or not path.read_text().startswith(MARK))):
            raise edge.Rejected("Чужой systemd unit не будет перезаписан: " + path.name)
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.parent.is_symlink():
        raise edge.Rejected("Каталог /etc/pushok не должен быть ссылкой")
    STATE.mkdir(mode=0o700, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_mode & 0o077:
        raise edge.Rejected("Системный каталог обновлений должен иметь права 0700")
    if PASSWORD.exists() or PASSWORD.is_symlink():
        only_regular_private(PASSWORD, 0o600)
    else:
        fd = os.open(PASSWORD, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_urlsafe(48) + "\n")
            output.flush()
            os.fsync(output.fileno())
    for path, content in ((UNIT, unit_contents()), (TIMER, timer_contents())):
        write_private(path, content.encode())
        path.chmod(0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
    subprocess.run(["systemctl", "enable", "--now", TIMER.name], check=True, timeout=30)
    if run_now:
        subprocess.run(["systemctl", "start", UNIT.name], check=True, timeout=3600)
    return {"state": "installed", "timer": TIMER.name,
            "password_file": str(PASSWORD), "run_now": run_now,
            "warning": "Сохраните отдельную защищённую копию файла пароля вне VPS"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Автоматические безопасные обновления Shop по GitHub Releases")
    parser.add_argument("command", choices=("plan", "run", "install", "status"))
    parser.add_argument("--run-now", action="store_true")
    args = parser.parse_args(argv)
    if args.run_now and args.command != "install":
        raise edge.Rejected("--run-now используется только с install")
    if args.command == "plan":
        result = plan(ROOT)
    elif args.command == "status":
        result = json.loads(only_regular_private(STATE / "last-result.json").read_text())
    elif args.command == "run":
        check_root(ROOT)
        try:
            result = execute(ROOT)
        except BaseException:
            save_result({"at": datetime.now(timezone.utc).isoformat(), "state": "manual_review_required"})
            raise
    else:
        result = install(ROOT, run_now=args.run_now)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (edge.Rejected, ValueError, OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print("Автообновление остановлено: " + str(exc) if isinstance(exc, edge.Rejected)
              else "Автообновление остановлено: ошибка GitHub, Docker, systemd или файлов",
              file=sys.stderr)
        raise SystemExit(1)
