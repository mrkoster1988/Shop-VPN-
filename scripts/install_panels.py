"""Необязательные панели: отдельный Compose-проект, без изменения существующих панелей."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess

IMAGES = {'xui': 'ghcr.io/mhsanaei/3x-ui', 'wgdashboard': 'ghcr.io/wgdashboard/wgdashboard'}


def compose(selected, images, public_ip=''):
    services, volumes = {}, {}
    for name in selected:
        image = images[name]
        if not re.fullmatch(re.escape(IMAGES[name]) + r'@sha256:[a-f0-9]{64}', image):
            raise ValueError('Нужен официальный образ с неизменяемым @sha256: digest для '+name)
        service = {'image': image, 'restart': 'unless-stopped'}
        if name == 'xui':
            service.update(ports=['127.0.0.1:2053:2053/tcp', '8443:8443/tcp'],
                           volumes=['xui_db:/etc/x-ui', 'xui_cert:/root/cert'],
                           environment={'XUI_ENABLE_FAIL2BAN': 'false'})
            volumes.update(xui_db={}, xui_cert={})
        else:
            ipaddress.IPv4Address(public_ip)
            service.update(ports=['127.0.0.1:10086:10086/tcp', '51820:51820/udp'],
                           volumes=['wg_conf:/etc/wireguard', 'wg_data:/data', 'awg_conf:/etc/amnezia/amneziawg'],
                           cap_add=['NET_ADMIN'], environment={'public_ip': public_ip, 'tz': 'UTC'})
            volumes.update(wg_conf={}, wg_data={}, awg_conf={})
        services[name] = service
    return {'services': services, 'volumes': volumes}


def main():
    p = argparse.ArgumentParser(description='Установка дополнительных VPN-панелей Shop')
    p.add_argument('--panels', choices=['xui', 'wgdashboard', 'both'], required=True)
    p.add_argument('--dir', type=Path, default=Path('/opt/pushok-panels'))
    p.add_argument('--xui-image'); p.add_argument('--wgdashboard-image'); p.add_argument('--public-ip', default='')
    p.add_argument('--plan', action='store_true')
    args = p.parse_args()
    selected = ['xui', 'wgdashboard'] if args.panels == 'both' else [args.panels]
    images = {}
    print('Новые панели: существующие установки не импортируются и не обновляются.')
    print('Выберите проверенный релиз в официальном реестре и скопируйте образ с @sha256:.')
    for name in selected:
        value = getattr(args, name+'_image')
        if not value:
            with open('/dev/tty', 'r+') as tty:
                tty.write(IMAGES[name]+'@sha256:… (полная строка): '); tty.flush(); value = tty.readline().strip()
        images[name] = value
    if 'wgdashboard' in selected and not args.public_ip:
        with open('/dev/tty', 'r+') as tty:
            tty.write('Публичный IPv4 VPS для WireGuard: '); tty.flush(); args.public_ip = tty.readline().strip()
    config = compose(selected, images, args.public_ip)
    if args.plan:
        print(json.dumps(config, indent=2)); return
    if os.geteuid() != 0: raise ValueError('Нужны права root')
    root = args.dir
    if not root.is_absolute() or root.exists(): raise ValueError('Нужен новый абсолютный каталог; существующая установка не изменена')
    subprocess.run(['docker', 'compose', 'version'], check=True)
    subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL)
    for name in selected:
        for port, udp in ([(2053, False), (8443, False)] if name == 'xui' else [(10086, False), (51820, True)]):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
                sock.bind(('0.0.0.0', port))
    root.mkdir(mode=0o700, parents=True)
    target = root/'compose.json'
    target.write_text(json.dumps(config, indent=2)+'\n'); target.chmod(0o600)
    command = ['docker', 'compose', '-p', 'pushok-panels-'+hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:10], '-f', str(target)]
    subprocess.run(command+['config', '--quiet'], check=True)
    subprocess.run(command+['up', '-d'], check=True)
    print('Контейнеры запущены. Это ещё не подтверждение готовности VPN. См. docs/PANELS_SETUP.md.')
    print('Панели доступны только через localhost: 3x-ui 2053, WGDashboard 10086. Войдите через SSH-туннель и задайте свои пароли.')


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit('Установка панелей остановлена: '+str(exc))
