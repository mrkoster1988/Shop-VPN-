"""Однократное включение службы владельцем VPS; приложение не устанавливает её само."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

spec = importlib.util.spec_from_file_location('installer', Path(__file__).with_name('install.py'))
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)


def setup(root, port, *, plan=False):
    root = i.install_path(str(root))
    if root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
        raise ValueError('Каталог установки должен принадлежать root с правами 0700')
    settings = json.loads((root / 'installer.json').read_text())
    if not re.fullmatch(r'pushok-[a-f0-9]{10}', settings.get('project','')):
        raise ValueError('Нужна установка мастером Pushok')
    if (root / 'upgrade.json').exists() and json.loads((root / 'upgrade.json').read_text()).get('phase') not in ('committed','rolled_back'):
        raise ValueError('Сначала завершите предыдущую операцию обновления')
    state = root / 'updater'
    work = Path('/var/lib/pushok-updater') / settings['project']
    unit_name = 'pushok-updater-' + settings['project'] + '.service'
    if plan:
        return {'service':unit_name, 'bind':'127.0.0.1', 'port':port, 'restart_shop':True, 'plan_only':True}
    if os.geteuid() != 0:
        raise ValueError('Нужны права root')
    fd = os.open(root / '.install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state.exists():
            raise ValueError('Служба уже настроена. Используйте status; ключи повторно не создаются')
        if not i.port_free(port, '127.0.0.1'):
            raise ValueError('Порт уже занят')
        env_path = root / '.env'
        if env_path.is_symlink() or not env_path.is_file():
            raise ValueError('Не найден обычный файл .env')
        original = env_path.read_text()
        if re.search(r'^\s*(?:export\s+)?PUSHOK_HOST_UPDATER_(?:PORT|TOKEN)\s*=', original, re.M):
            raise ValueError('Сначала разберите существующие настройки службы в .env')
        if work.exists() or work.is_symlink():
            raise ValueError('Каталог службы уже существует; требуется проверка прежней установки')
        state.mkdir(mode=0o700)
        work.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if work.parent.is_symlink() or work.parent.stat().st_uid != 0 or work.parent.stat().st_mode & 0o022:
            raise ValueError('Небезопасный каталог службы')
        work.mkdir(mode=0o700)
        token = secrets.token_hex(32)
        i.atomic_text(state / 'config.json', json.dumps({'port':port,'token':token,'work':str(work)}))
        i.atomic_text(state / 'backup-password', secrets.token_urlsafe(48) + '\n')
        i.atomic_text(state / 'env.before', original)
        for name in ('host_updater.py','update_source.py'):
            i.atomic_text(state / name, Path(__file__).with_name(name).read_text())
        unit = '\n'.join(['[Unit]','Description=Pushok host updater','After=docker.service network-online.target',
            'Requires=docker.service','[Service]','Type=simple','User=root','UMask=0077',
            'ExecStart=/usr/bin/python3 ' + str(state / 'host_updater.py') + ' serve --dir ' + str(root),
            'Restart=on-failure','RestartSec=5','KillMode=control-group','TimeoutStopSec=30',
            'NoNewPrivileges=true','PrivateTmp=true','ProtectSystem=strict',
            'BindPaths=' + str(root),
            'ReadWritePaths=' + str(root) + ' ' + str(work),'[Install]','WantedBy=multi-user.target',''])
        i.atomic_text(Path('/etc/systemd/system') / unit_name, unit, mode=0o644)
        subprocess.run(['systemctl','daemon-reload'], check=True)
        subprocess.run(['systemctl','enable','--now',unit_name], check=True)
        i.atomic_text(env_path, original.rstrip() + '\nPUSHOK_HOST_UPDATER_PORT=' + str(port)
                      + '\nPUSHOK_HOST_UPDATER_TOKEN=' + token + '\n')
        subprocess.run(['docker','compose','--project-name',settings['project'],'--env-file',str(env_path),
                        '-f',str(root / 'compose.install.json'),'up','-d','--no-deps','--no-build',
                        '--force-recreate','--wait','--wait-timeout','120','shop'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'installed':True,'service':unit_name,'backup_password_file':str(state / 'backup-password')}
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Включить обновление Pushok из панели владельца')
    parser.add_argument('--dir', default='/opt/pushok-vpn', type=Path)
    parser.add_argument('--port', type=int, default=9079)
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError('Нужен порт от 1024 до 65535')
        print(json.dumps(setup(args.dir,args.port,plan=args.plan),ensure_ascii=False))
    except Exception:
        print('Настройка не завершена. Проверьте локальную службу, каталог updater и .env; исходная конфигурация при начале настройки сохраняется в updater/env.before. Секреты не выводятся.',file=sys.stderr)
        raise SystemExit(1)
