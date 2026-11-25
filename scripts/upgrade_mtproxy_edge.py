#!/usr/bin/env python3
"""Conservative in-place upgrade for the Shop deployment beside MTProxy.

Unlike upgrade.py, this does not require installer.json and does not move the
data volume. Never changes MTProxy, Caddy, Pushok ID, or WGDashboard.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys

import update_source

ROOT = Path("/opt/pushok-shop")
IMAGE = "pushok-shop:mtproxy-edge"
CONTAINER = "pushok-shop"
VOLUME = "pushok_shop_data"
MAX_BACKUP = 180 * 1024 * 1024
IMAGE_SHA = re.compile(r"sha256:[a-f0-9]{64}\Z")


class Rejected(ValueError):
    pass


def run(command, *, input=None, env=None, timeout=120, capture=True):
    try:
        result = subprocess.run(
            command, input=input, env=env, timeout=timeout,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Rejected("Команда Docker недоступна или превысила время ожидания") from exc
    if result.returncode:
        # Never surface Docker stderr: it can contain config or credentials.
        raise Rejected("Операция Docker завершилась с ошибкой")
    return result.stdout.decode("utf-8").strip() if capture else ""


def docker_json(*args):
    try:
        return json.loads(run(["docker", *args]))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Rejected("Docker вернул некорректные метаданные") from exc


def required_file(path, *, private=False):
    if path.is_symlink() or not path.is_file():
        raise Rejected("Отсутствует обязательный обычный файл: " + path.name)
    if private and (path.stat().st_mode & 0o077):
        raise Rejected("Ограничьте доступ к .env: chmod 600 /opt/pushok-shop/.env")
    return path


def compose_files(root):
    base = required_file(root / "compose.mtproxy-edge.yaml")
    overlay = root / "compose.wg-ip.yaml"
    files = [base]
    if overlay.exists() or overlay.is_symlink():
        files.append(required_file(overlay))
    return files


def compose(root, files):
    result = ["docker", "compose", "--project-name", "pushok-shop",
              "--env-file", str(root / ".env")]
    for file in files:
        result += ["-f", str(file)]
    return result


def hash_files(root, files):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [root / ".env", *files]}


def internal(path):
    code = (
        "import sys,urllib.request;"
        "r=urllib.request.urlopen('http://127.0.0.1:8000'+sys.argv[1],timeout=8);"
        "print(r.read(4096).decode())"
    )
    try:
        return json.loads(run(["docker", "exec", CONTAINER, "python", "-c", code, path], timeout=15))
    except (Rejected, ValueError, json.JSONDecodeError):
        return None


def probe(root):
    if root.is_symlink() or not root.is_dir():
        raise Rejected("Каталог установки отсутствует или является ссылкой")
    required_file(root / ".env", private=True)
    files = compose_files(root)
    conf = docker_json(*(compose(root, files)[1:] + ["config", "--format", "json"]))
    services = conf.get("services", {})
    if set(services) != {"shop"}:
        raise Rejected("Compose содержит другие сервисы; обновлять автоматически нельзя")
    service = services["shop"]
    if (service.get("image") != IMAGE or service.get("container_name") != CONTAINER
            or service.get("ports") or service.get("network_mode") == "host"):
        raise Rejected("Образ, контейнер или сеть Shop отличаются от проверенной схемы")
    mounts = [item for item in service.get("volumes", [])
              if item.get("target") == "/app/data"]
    if len(mounts) != 1 or mounts[0].get("type") != "volume":
        raise Rejected("Compose не использует ожидаемый постоянный том Shop")
    source_name = mounts[0].get("source")
    volumes = conf.get("volumes") or {}
    declared_volume = volumes.get(source_name) if isinstance(volumes, dict) else None
    resolved_name = declared_volume.get("name") if isinstance(declared_volume, dict) else None
    if source_name != VOLUME and resolved_name != VOLUME:
        raise Rejected("Compose не использует ожидаемый постоянный том Shop")
    containers = docker_json("inspect", CONTAINER)
    if len(containers) != 1:
        raise Rejected("Не найден единственный контейнер Shop")
    container = containers[0]
    labels = container.get("Config", {}).get("Labels") or {}
    sources = [v.strip() for v in labels.get("com.docker.compose.project.config_files", "").split(",")]
    if (labels.get("com.docker.compose.project") != "pushok-shop"
            or labels.get("com.docker.compose.service") != "shop"
            or sources != [str(item) for item in files]):
        raise Rejected("Контейнер запущен из другого Compose-проекта/набора файлов")
    running_config_hash = labels.get("com.docker.compose.config-hash", "")
    effective_hash = run(compose(root, files) + ["config", "--hash", "shop"]).split()[-1]
    if (not re.fullmatch(r"[a-f0-9]{64}", running_config_hash)
            or running_config_hash != effective_hash):
        raise Rejected("Compose изменён с момента запуска контейнера; нужен ручной разбор")
    real_mounts = [m for m in container.get("Mounts", []) if m.get("Destination") == "/app/data"]
    if (len(real_mounts) != 1 or real_mounts[0].get("Type") != "volume"
            or real_mounts[0].get("Name") != VOLUME or real_mounts[0].get("RW") is not True):
        raise Rejected("Том запущенного Shop не совпадает с конфигурацией")
    image = container.get("Image", "")
    alias = run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE])
    if not IMAGE_SHA.fullmatch(image) or alias != image:
        raise Rejected("Текущий Docker image не совпадает с Compose; ручная проверка")
    state = container.get("State", {})
    if state.get("Status") != "running" or (state.get("Health") or {}).get("Status") != "healthy":
        raise Rejected("Shop должен быть запущен и healthy")
    health = internal("/shop/health")
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise Rejected("Внутренний /shop/health недоступен")
    public_version = internal("/shop/api/version")
    version = public_version.get("version") if isinstance(public_version, dict) else None
    return {
        "container_id": container["Id"], "image_id": image, "volume": VOLUME,
        "runtime_version": version if isinstance(version, str) else None,
        "compose_files": [p.name for p in files],
        "fingerprint": hash_files(root, files),
    }


def unchanged(before, after):
    if any(before[key] != after[key] for key in ("container_id", "image_id", "volume", "fingerprint", "compose_files")):
        raise Rejected("Установка изменилась во время подготовки; операция отменена")


@contextmanager
def exclusive(root):
    lock = root / ".edge-upgrade.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Rejected("Другое обновление Shop уже выполняется") from exc
        yield
    finally:
        os.close(descriptor)


def save(job, state):
    payload = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    temporary = job / ".state.tmp"
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, job / "state.json")
    finally:
        if temporary.exists():
            temporary.unlink()


def secret_password(password_file):
    if password_file:
        path = Path(password_file)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise Rejected("Файл пароля должен быть обычным файлом с правами 0600")
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise Rejected("Слишком длинный пароль резервной копии")
        password = raw.decode("utf-8").rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise Rejected("Запустите в терминале или укажите --password-file")
        password = getpass.getpass("Отдельный пароль резервной копии (16+ символов): ")
        if password != getpass.getpass("Повторите пароль: "):
            raise Rejected("Пароли резервной копии не совпали")
    if len(password) < 16:
        raise Rejected("Пароль резервной копии должен содержать от 16 символов")
    return password


def encrypted_backup(root, job, password):
    """Create+verify inside the old container; transfer bytes, compare SHA-256."""
    archive = "/tmp/" + job.name + ".pvb"
    credentials = {**os.environ, "PUSHOK_BACKUP_PASSWORD": password}
    output = job / "backup.pvb"
    try:
        run(["docker", "exec", "-i", "-e", "PUSHOK_BACKUP_PASSWORD", CONTAINER,
             "python", "scripts/backup.py", "create", "--database", "/app/data/shop.db",
             "--env-stdin", "--output", archive],
            input=(root / ".env").read_bytes(), env=credentials, timeout=90)
        run(["docker", "exec", "-e", "PUSHOK_BACKUP_PASSWORD", CONTAINER,
             "python", "scripts/backup.py", "verify", archive], env=credentials, timeout=90)
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            proc = subprocess.run(
                ["docker", "exec", CONTAINER, "python", "-c",
                 "import shutil,sys;shutil.copyfileobj(open(sys.argv[1],'rb'),sys.stdout.buffer)",
                 archive], stdout=file, stderr=subprocess.DEVNULL, timeout=90, check=False)
            file.flush()
            os.fsync(file.fileno())
            if proc.returncode:
                raise Rejected("Не удалось получить защищённую копию из контейнера")
        if not 24 < output.stat().st_size <= MAX_BACKUP:
            raise Rejected("Неверный размер резервной копии")
        with output.open("rb") as file:
            local_sha = hashlib.file_digest(file, "sha256").hexdigest()
        remote_sha = run(["docker", "exec", CONTAINER, "python", "-c",
                          "import hashlib,sys;print(hashlib.file_digest(open(sys.argv[1],'rb'),'sha256').hexdigest())",
                          archive])
        if local_sha != remote_sha:
            raise Rejected("Проверка копии после переноса не пройдена")
        return local_sha
    finally:
        try:
            run(["docker", "exec", CONTAINER, "python", "-c",
                 "import os,sys; p=sys.argv[1]; os.path.isfile(p) and os.unlink(p)", archive], timeout=15)
        except Rejected:
            pass


def build(source, name, logfile):
    with logfile.open("xb") as log:
        proc = subprocess.run(["docker", "build", "--tag", name, str(source)],
                              stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=False)
    if proc.returncode:
        raise Rejected("Сборка не прошла; приватный журнал: " + str(logfile))
    image = run(["docker", "image", "inspect", "--format", "{{.Id}}", name])
    if not IMAGE_SHA.fullmatch(image):
        raise Rejected("Неверный идентификатор собранного образа")
    return image


def apply(root, target, password_file):
    update_source.version(target)
    with exclusive(root):
        previous = status(root)
        if previous["state"] in {"recovery_required", "switching", "downloading", "building", "backing_up"}:
            raise Rejected("Есть незавершённое edge-обновление; сначала проверьте его журнал вручную")
        before = probe(root)
        if before["runtime_version"] == target:
            return {"state": "already_current", "version": target, "container_restarted": False}
        if before["runtime_version"] and update_source.version(before["runtime_version"]) > update_source.version(target):
            raise Rejected("Обновление запрещает откат на более старый релиз")
        upgrades = root / "edge-upgrades"
        upgrades.mkdir(mode=0o700, exist_ok=True)
        if upgrades.is_symlink() or upgrades.stat().st_mode & 0o077:
            raise Rejected("Каталог edge-upgrades должен иметь права 0700")
        operation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
        job = upgrades / operation
        job.mkdir(mode=0o700)
        candidate_name = "pushok-shop:edge-" + secrets.token_hex(8)
        old_name = "pushok-shop:rollback-" + secrets.token_hex(8)
        state = {"phase": "downloading", "target": target, "old_image": before["image_id"],
                 "backup": None, "candidate": None, "volume": VOLUME,
                 "compose_files": before["compose_files"], "operation": operation}
        save(job, state)
        switched = False
        try:
            source, commit = update_source.fetch(target, job / "release")
            state.update(phase="building", commit=commit)
            save(job, state)
            state["candidate"] = build(source, candidate_name, job / "build.log")
            state["phase"] = "backing_up"
            save(job, state)
            unchanged(before, probe(root))
            password = secret_password(password_file)
            state["backup_sha256"] = encrypted_backup(root, job, password)
            del password
            state["backup"] = "backup.pvb"
            save(job, state)
            unchanged(before, probe(root))
            files = compose_files(root)
            state["phase"] = "switching"
            state["rollback_image"] = old_name
            save(job, state)
            run(["docker", "tag", before["image_id"], old_name])
            run(["docker", "tag", state["candidate"], IMAGE])
            switched = True
            run(compose(root, files) + [
                "up", "-d", "--no-deps", "--no-build", "--force-recreate",
                "--wait", "--wait-timeout", "150", "shop"], timeout=210)
            after = probe(root)
            if after["runtime_version"] != target or after["volume"] != before["volume"]:
                raise Rejected("Версия или постоянный том после запуска не совпали")
            if after["image_id"] != state["candidate"]:
                raise Rejected("В контейнер запущен другой образ")
            state["phase"] = "complete"
            save(job, state)
            return {"state": "complete", "version": target, "backup": str(job / "backup.pvb"),
                    "operation": operation, "container_restarted": True}
        except BaseException:
            state["phase"] = "recovery_required" if switched else "failed_before_switch"
            save(job, state)
            # Never restore a database automatically after the candidate may have served writes.
            raise


def status(root):
    upgrades = root / "edge-upgrades"
    jobs = sorted(upgrades.glob("*/state.json")) if upgrades.is_dir() else []
    if not jobs:
        return {"state": "no_edge_upgrade"}
    data = json.loads(jobs[-1].read_text())
    return {"state": data["phase"], "target": data["target"],
            "operation": data["operation"], "backup": data.get("backup")}


def main(argv=None):
    p = argparse.ArgumentParser(description="Безопасное обновление Shop рядом с MTProxy")
    p.add_argument("command", choices=("plan", "apply", "status"))
    p.add_argument("--dir", type=Path, default=ROOT)
    p.add_argument("--release", default="v0.064")
    p.add_argument("--password-file")
    p.add_argument("--confirm-edge-upgrade", action="store_true")
    args = p.parse_args(argv)
    root = args.dir.absolute()
    if args.command == "status":
        result = status(root)
    elif args.command == "plan":
        update_source.version(args.release)
        result = {"state": "ready_for_edge_plan", **probe(root),
                  "target": args.release, "does_not_modify_server": True}
    else:
        if not args.confirm_edge_upgrade:
            raise Rejected("Для обновления нужен флаг --confirm-edge-upgrade")
        if os.geteuid() != 0:
            raise Rejected("Нужны права root")
        result = apply(root, args.release, args.password_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Rejected, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # No Docker error messages, credentials, environment, or traceback in terminal.
        print("Остановлено: " + str(exc) if isinstance(exc, Rejected)
              else "Остановлено: ошибка файлов, источника релиза или Docker",
              file=sys.stderr)
        raise SystemExit(1)
