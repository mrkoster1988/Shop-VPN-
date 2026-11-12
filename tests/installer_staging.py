"""Настоящий CLI установщика на одноразовой Ubuntu 24.04 с готовым Docker."""
import argparse
import ast
import hashlib
import http.client
from http.cookies import SimpleCookie
import importlib.util
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import ssl
import sys
import tarfile
import tempfile

spec = importlib.util.spec_from_file_location('installer_docker_helpers', Path(__file__).with_name('docker_staging.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
check, StageError = helpers.check, helpers.StageError


def guard(disposable, uid, info):
    check(disposable and uid == 0, 'Нужен явно одноразовый root-стенд (--disposable)')
    check(info.get('ID') == 'ubuntu' and info.get('VERSION_ID') == '24.04', 'Нужна настоящая Ubuntu 24.04')


class InstallerStage(helpers.Staging):
    def load_secrets(self):
        path = self.root / '.env'
        if path.exists():
            values = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
            for key in ('ADMIN_TOKEN', 'ENCRYPTION_KEY', 'WG_AGENT_TOKEN'):
                value = ast.literal_eval(values[key])
                if value not in self.secrets: self.secrets.append(value)
            self.admin = ast.literal_eval(values['ADMIN_TOKEN'])

    def install(self, *extra, expected=2, root=None, port=None):
        args = [sys.executable, str(self.source / 'scripts/install.py'), '--source', str(self.source),
                '--dir', str(root or self.root), '--yes', '--url', self.public_url,
                '--mode', 'external', '--port', str(port or self.port), '--panels', 'none', *extra]
        # Подпроцесс запускает неизменённый установщик; Docker и health-check настоящие.
        try:
            result = helpers.subprocess.run(args, capture_output=True, text=True, timeout=900)
        finally:
            self.load_secrets()
        check(result.returncode == expected, 'installer: exit ' + str(result.returncode) + ' ' + self.scrub(result.stderr))
        if expected == 2:
            check('HTTPS пока не подключён' in result.stdout and 'Готово:' not in result.stdout,
                  'Режим external ошибочно объявлен публично готовым')
        return result.stdout

    def request(self, path, *, body=None, cookie=None, admin=False, expected=200, origin=None):
        headers = {'Host': 'shop.install.test'}
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': origin or 'https://shop.install.test'})
        if cookie: headers['Cookie'] = 'session=' + cookie
        if admin: headers['Authorization'] = 'Bearer ' + self.admin
        connection = http.client.HTTPSConnection('127.0.0.1', 443, timeout=15, context=self.context)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse(); raw = response.read(524288)
            check(response.status == expected, 'HTTP ' + str(response.status) + ' вместо ' + str(expected) + ': ' + path)
            return raw, response.headers
        finally: connection.close()

    def credentials_snapshot(self):
        return {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in ('.env', 'installer.json', 'compose.install.json')}

    def verify_runtime(self):
        info = self.json('inspect installer container', ['docker', 'inspect', self.container()])[0]
        check(info['State']['Health']['Status'] == 'healthy', 'Контейнер не healthy')
        check(info['Config']['User'] == 'shop' and info['HostConfig']['ReadonlyRootfs'], 'Нарушена изоляция контейнера')
        check(info['HostConfig']['NetworkMode'] == 'host', 'Изменён режим сети')
        check('ALL' in info['HostConfig']['CapDrop'], 'Не сброшены capabilities')
        ports = [line.split()[1] for line in Path('/proc/net/tcp').read_text().splitlines()[1:]
                 if line.split()[3] == '0A' and int(line.split()[1].split(':')[1], 16) == self.port]
        check(ports == ['0100007F:' + format(self.port, '04X')], 'Магазин должен слушать только IPv4 loopback')
        mounts = [m['Name'] for m in info['Mounts'] if m.get('Destination') == '/app/data' and m.get('Type') == 'volume']
        check(mounts == [self.project + '_shop_data'], 'Изменилось имя постоянного тома')

    def scenario(self, temp):
        self.root = temp / 'installed'; self.public_url = 'https://shop.install.test/shop'
        project = 'pushok-' + hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        self.initial_containers = set(self.run('containers', ['docker', 'ps', '-aq', '--no-trunc'])[0].split())
        self.initial_volumes = set(self.run('volumes', ['docker', 'volume', 'ls', '-q'])[0].split())
        self.initial_tags = set(self.run('images', ['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}'])[0].split())
        check(project + '_shop_data' not in self.initial_volumes and project + '-shop:latest' not in self.initial_tags,
              'Имена стенда уже заняты')
        check(not self.run('namespace', ['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project])[0],
              'Compose-проект уже существует')
        self.project = project  # Только теперь очистка может удалять наши ресурсы.
        archive = temp / 'candidate.tar.gz'
        self.run('archive candidate', ['git', '-C', str(self.args.source), 'archive', '--format=tar.gz',
                                      '--prefix=source/', '-o', str(archive), 'HEAD'])
        self.commit = self.run('source commit', ['git', '-C', str(self.args.source), 'rev-parse', 'HEAD'])[0]
        with tarfile.open(archive) as package:
            members = package.getmembers()
            check(len(members) <= 2000 and sum(m.size for m in members) <= 64 * 1024 * 1024, 'Архив превышает лимиты')
            check(all((m.isfile() or m.isdir()) and not Path(m.name).is_absolute() and '..' not in Path(m.name).parts
                      for m in members), 'Небезопасный исходный архив')
            package.extractall(temp, filter='data')
        check(archive.stat().st_size <= 32 * 1024 * 1024, 'Превышен лимит загрузчика')
        self.source = temp / 'source'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
            self.install('--plan', expected=0)
            check(not self.root.exists(), 'План создал каталог установки')
            self.install(expected=1)
            check(not self.root.exists(), 'Занятый порт не должен создавать установку')
        unmanaged = temp / 'unmanaged'; unmanaged.mkdir(); (unmanaged / 'keep').write_text('keep')
        self.install(expected=1, root=unmanaged)
        check(list(unmanaged.iterdir()) == [unmanaged / 'keep'] and (unmanaged / 'keep').read_text() == 'keep',
              'Изменён чужой каталог')
        check(set(self.run('containers after plan', ['docker', 'ps', '-aq', '--no-trunc'])[0].split()) == self.initial_containers,
              'План/отказ создал контейнер')
        check(set(self.run('volumes after plan', ['docker', 'volume', 'ls', '-q'])[0].split()) == self.initial_volumes,
              'План/отказ создал том')
        self.event('plan_and_conflict_guards_passed')
        self.install()
        settings = json.loads((self.root / 'installer.json').read_text())
        check(settings['project'] == self.project and settings['url'] == self.public_url, 'Потеря параметров установки')
        for name in ('.env', 'installer.json', 'compose.install.json'):
            check((self.root / name).stat().st_mode & 0o777 == 0o600, 'Небезопасные права ' + name)
        check(self.root.stat().st_mode & 0o777 == 0o700, 'Небезопасные права каталога')
        check(not (self.root / 'source/.git').exists() and not (self.root / 'source/.env').exists(), 'Лишние файлы в исходниках')
        self.compose = ['docker', 'compose', '--project-name', self.project, '--env-file', str(self.root / '.env'),
                        '-f', str(self.root / 'compose.install.json')]
        self.verify_runtime(); self.event('fresh_external_install_healthy')
        cert, key = temp / 'certificate.pem', temp / 'key.pem'
        self.run('local certificate', ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                                      '-out', str(cert), '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'])
        self.context = ssl.create_default_context(cafile=str(cert))
        self.start_proxy(self.port, cert, key)
        check(json.loads(self.request('/shop/health')[0])['status'] == 'ok', 'Ошибка health')
        self.request('/shop/'); self.request('/shop/admin'); self.request('/health', expected=404)
        self.request('/shop/api/admin/diagnostics', expected=401)
        diagnostics = json.loads(self.request('/shop/api/admin/diagnostics', admin=True)[0])
        check(diagnostics['release'] == settings['version'], 'Диагностика указывает другую версию')
        password = secrets.token_urlsafe(24); self.secrets.append(password)
        account = {'email': 'installer@example.invalid', 'password': password}
        self.request('/shop/api/auth/register', body=account, origin='https://wrong.invalid', expected=403)
        _, headers = self.request('/shop/api/auth/register', body=account)
        cookies = SimpleCookie(); cookies.load(headers['Set-Cookie']); cookie = cookies['session']
        self.secrets.append(cookie.value)
        check(cookie['secure'] and cookie['httponly'] and cookie['path'] == '/shop/' and cookie['samesite'] == 'lax',
              'Неверные параметры cookie')
        before = json.loads(self.request('/shop/api/me', cookie=cookie.value)[0])
        self.event('https_prefix_admin_and_registration_passed')
        snapshot = self.credentials_snapshot()
        self.run('stop shop before repeat', self.compose + ['stop', 'shop'])
        self.install(); self.verify_runtime()
        check(self.credentials_snapshot() == snapshot, 'Повтор изменил ключи или параметры')
        check(json.loads(self.request('/shop/api/me', cookie=cookie.value)[0]) == before, 'Повтор потерял клиента или сессию')
        self.request('/shop/api/auth/login', body=account)
        self.event('repeat_preserves_keys_account_session_and_volume')
        self.install(expected=1, port=self.port + 1 if self.port < 65535 else self.port - 1)
        check(self.credentials_snapshot() == snapshot, 'Отказ смены порта изменил конфигурацию')
        self.request('/shop/health'); self.event('changed_identity_refused_without_outage')
        return {'status': 'passed', 'source_commit': self.commit, 'release': settings['version'], 'events': self.events,
                'ubuntu': '24.04', 'real_installer_cli': True, 'docker_preinstalled': True, 'mode': 'external',
                'local_https_fixture': True, 'clean_os_docker_install': False, 'public_dns_acme': False,
                'bootstrap_download': False, 'real_provider_payment': False, 'real_vpn_traffic': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable', action='store_true')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    guard(args.disposable, os.geteuid(), platform.freedesktop_os_release())
    args.source = args.source.resolve()
    check(not args.report.exists() and not args.report.is_symlink(), 'Отчёт уже существует')
    for program in ('git', 'docker', 'openssl'): check(shutil.which(program), 'Не найден ' + program)
    with socket.socket() as sock:
        try: sock.bind(('127.0.0.1', 443))
        except OSError: raise StageError('Для локального HTTPS стенда нужен свободный порт 443') from None
    stage = InstallerStage(args); result = {'status': 'failed'}
    with tempfile.TemporaryDirectory(prefix='pushok-installer-stage-') as directory:
        try:
            stage.run('Docker', ['docker', 'info']); stage.run('Compose', ['docker', 'compose', 'version'])
            result = stage.scenario(Path(directory))
        except Exception as error:
            result.update(events=stage.events, error=stage.scrub(str(error)))
        finally: stage.cleanup()
    result['cleanup_ok'] = stage.cleanup_ok
    if not stage.cleanup_ok: result['status'] = 'failed'
    fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump(result, stream, indent=2, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    try: raise SystemExit(main())
    except StageError as error: print(str(error), file=sys.stderr); raise SystemExit(2)
