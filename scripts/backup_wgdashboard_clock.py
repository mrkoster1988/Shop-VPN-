"""Back up the running WGDashboard before changing its wall clock. Run on its Docker host."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SQLITE_SNAPSHOT = """
import pathlib, shutil, sqlite3, sys, tempfile
source = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as work:
    target = pathlib.Path(work) / 'snapshot'
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with target.open('rb') as file:
        shutil.copyfileobj(file, sys.stdout.buffer)
"""


def docker(*args, output=None):
    return subprocess.run(['docker', *args], check=True, stdout=output or subprocess.PIPE).stdout


def moscow_times(value, source_zone):
    wall = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    instants = {wall.replace(tzinfo=source_zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)}
    return sorted(t.astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
                  for t in instants if t.astimezone(source_zone).replace(tzinfo=None) == wall)


def snapshot_databases(container, data):
    for path in sorted(data.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as file:
            if file.read(16) != b'SQLite format 3\x00':
                continue
        container_path = '/data/' + str(path.relative_to(data))
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fresh:
            docker('exec', container, 'python3', '-c', SQLITE_SNAPSHOT, container_path, output=fresh)
        with sqlite3.connect(Path(fresh.name).as_uri() + '?mode=ro&immutable=1', uri=True) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Не прошла проверка резервной копии ' + path.name)
        # Replace only files in the new private backup. A snapshot has no live WAL.
        os.replace(fresh.name, path)
        for suffix in ('-shm', '-wal', '-journal'):
            Path(str(path) + suffix).unlink(missing_ok=True)


def backup(container, parent):
    os.umask(0o077)
    info = json.loads(docker('inspect', container))[0]
    ident = info['Id']
    env = dict(v.split('=', 1) for v in info['Config']['Env'] if '=' in v)
    zone_name = env.get('TZ') or env.get('tz')
    if not zone_name:
        raise ValueError('Пояс контейнера не указан в TZ. Сначала проверьте его вручную.')
    zone = ZoneInfo(zone_name)
    folder = Path(tempfile.mkdtemp(prefix='wg-clock-', dir=parent)).resolve()
    pending = folder / 'INCOMPLETE'
    pending.touch()
    print('Каталог резервной копии:', folder, flush=True)
    (folder / 'container.json').write_text(json.dumps(info, indent=2))
    for source, name in [('/etc/wireguard', 'wireguard'), ('/data', 'data')]:
        docker('cp', ident + ':' + source, str(folder / name))
    amnezia = '/etc/amnezia/amneziawg'
    if subprocess.run(['docker', 'exec', ident, 'test', '-d', amnezia]).returncode == 0:
        docker('cp', ident + ':' + amnezia, str(folder / 'amneziawg'))
    live = folder / 'live-interfaces'
    live.mkdir()
    for iface in docker('exec', ident, 'wg', 'show', 'interfaces').decode().split():
        if not re.fullmatch(r'[\w.=-]{1,15}', iface):
            raise ValueError('Неожиданное имя интерфейса')
        with (live / (iface + '.conf')).open('xb') as output:
            docker('exec', ident, 'wg', 'showconf', iface, output=output)
    if not (folder / 'data/wg-dashboard.ini').is_file() or not list((folder / 'wireguard').glob('*.conf')):
        raise RuntimeError('Проверьте пути к настройкам панели и WireGuard.')
    snapshot_databases(ident, folder / 'data')
    jobs = folder / 'data/db/wgdashboard_job.db'
    with sqlite3.connect(jobs.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        rows = db.execute("SELECT JobID, Value FROM PeerJobs WHERE Field='date' "
                          "AND ExpireDate IS NULL ORDER BY Value, JobID").fetchall()
    if json.loads(docker('inspect', container))[0]['Id'] != ident:
        raise RuntimeError('Контейнер изменился во время копирования.')
    pending.rename(folder / 'BACKUP_OK')
    print('BACKUP_OK · пояс', zone_name, '· активных заданий по дате:', len(rows))
    for job, value in rows:
        try:
            times = moscow_times(value, zone)
        except ValueError:
            times = []
        print(job, value, '→', ' / '.join(times) + ' МСК' if times else 'нужна сверка срока')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='wgdashboard')
    parser.add_argument('--directory', type=Path, default=Path('/root'))
    arguments = parser.parse_args()
    backup(arguments.container, arguments.directory)
