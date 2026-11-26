"""Coordinated clock change for a standalone, unmounted WGDashboard on Docker's bridge."""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MSK = 'Europe/Moscow'
FORMAT = '%Y-%m-%d %H:%M:%S'
HOLD = '.pushok-clock-hold'
GATE = 'pushok-clock-hold'
PORT = '10086'
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def docker(*args, data=None):
    result = subprocess.run([*DOCKER, *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # A Docker error may echo the container environment or private config.
        raise RuntimeError('Docker: ' + args[0] + ' failed')
    return result.stdout.decode()


def inspect(name):
    return json.loads(docker('inspect', name))[0]


def mark(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def digest(values):
    text = ''.join(f'{job} {values[job]}\n' for job in sorted(values))
    return hashlib.sha256(text.encode()).hexdigest()


def jobs(db):
    rows = db.execute("SELECT JobID,Value,Operator,Action FROM PeerJobs WHERE Field='date' AND ExpireDate IS NULL").fetchall()
    require(all(op == 'lgt' and action == 'delete' for _, _, op, action in rows),
            'Обнаружено задание другого типа. Нужна отдельная сверка.')
    return {job: value for job, value, _, _ in rows}


def convert(dbfile, expected):
    with sqlite3.connect(dbfile.as_uri() + '?mode=rw', uri=True) as db:
        db.execute('BEGIN IMMEDIATE')
        current = jobs(db)
        require(current and digest(current) == expected, 'Список или сроки изменились со времени сверки.')
        require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'Повреждена рабочая копия базы.')
        result = {}
        for job, value in current.items():
            instant = datetime.strptime(value, FORMAT).replace(tzinfo=timezone.utc)
            require(instant.timestamp() > time.time() + 900, 'Есть близкий или прошедший срок.')
            result[job] = instant.astimezone(ZoneInfo(MSK)).strftime(FORMAT)
            count = db.execute("UPDATE PeerJobs SET Value=? WHERE JobID=? AND Value=? AND ExpireDate IS NULL",
                               (result[job], job, value)).rowcount
            require(count == 1, 'Неоднозначное задание.')
        require(jobs(db) == result, 'Сверка записанных сроков не прошла.')
    return digest(result)


def shop_change(expected, prefixes):
    # Runs inside the Shop container; keep all registry credentials inside its process.
    from app import servers, config
    from app.main import ServerUpdate
    with sqlite3.connect(config.DB.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT server_id,job_id,target_expires FROM wgdashboard_expirations').fetchall()
        chosen = []
        for prefix in prefixes.split(','):
            require(re.fullmatch('[0-9a-f]{8,36}', prefix), 'Некорректный ID.')
            matches = [r for r in rows if r['job_id'].startswith(prefix)]
            require(len(matches) == 1, 'ID задания изменился или неоднозначен.')
            chosen.append(matches[0])
        require(len({r['server_id'] for r in chosen}) == 1, 'Задания принадлежат нескольким узлам.')
        utc = {r['job_id']: datetime.fromtimestamp(r['target_expires'], timezone.utc).strftime(FORMAT) for r in chosen}
        require(len(utc) == len(chosen) and digest(utc) == expected, 'Сроки Shop изменились после сверки.')
        node = dict(db.execute('SELECT * FROM servers WHERE id=?', (chosen[0]['server_id'],)).fetchone())
    profile = servers.decode(node)
    require(profile.get('api') == 'wgdashboard' and profile.get('schedule_expiry') is True
            and profile.get('panel_timezone') in ('UTC', MSK), 'Настройки узла изменились.')
    if profile['panel_timezone'] != MSK:
        fields = {key: node[key] for key in ('name','location','capacity','state','version')}
        servers.update(node['id'], ServerUpdate(**fields, panel_timezone=MSK))
    with sqlite3.connect(config.DB.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        updated = db.execute('SELECT * FROM servers WHERE id=?', (node['id'],)).fetchone()
        require(servers.decode(dict(updated)).get('panel_timezone') == MSK, 'Пояс Shop не подтверждён.')
    print('SHOP_MSK_OK', len(utc), expected)


class LocalEngine(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(60)
        self.sock.connect('/var/run/docker.sock')


def gate_cmd(operation):
    # Called in the isolated bridge namespace. Use both families where available.
    rule = '! -i lo -p tcp --dport 10086 -m comment --comment ' + GATE + ' -j REJECT'
    cmd = 'for tool in iptables ip6tables; do '
    cmd += 'if [ "$tool" = ip6tables ] && ! ip -6 addr show scope global | grep -q inet6; then continue; fi; '
    cmd += 'if ! command -v "$tool" >/dev/null; then exit 1; fi; '
    if operation == 'hold':
        cmd += f'"$tool" -w -C INPUT {rule} 2>/dev/null || "$tool" -w -I INPUT 1 {rule}; '
    else:
        cmd += f'if "$tool" -w -C INPUT {rule} 2>/dev/null; then "$tool" -w -D INPUT {rule}; fi; '
    return cmd + 'done'


def persist_autostart(ini, interfaces):
    # The dashboard stores interface names separated by a literal double bar.
    # Edit just this setting without rewriting or logging credentials elsewhere in the INI.
    with ini.open('r+', encoding='utf-8', newline='') as file:
        lines = file.readlines()
        start = next((i for i, line in enumerate(lines) if line.strip() == '[WireGuardConfiguration]'), None)
        if start is None:
            return False
        end = next((i for i in range(start+1,len(lines)) if lines[i].lstrip().startswith('[')),len(lines))
        pos = next((i for i in range(start+1,end) if re.match(r'^\s*autostart\s*=',lines[i],re.I)),end)
        configured = lines[pos].split('=',1)[1].strip().split('||') if pos < end else []
        names = list(dict.fromkeys([name for name in configured if name] + list(interfaces)))
        value = 'autostart = ' + '||'.join(names) + '\n'
        if pos == end and pos and not lines[pos-1].endswith('\n'):
            lines[pos-1] += '\n'
        lines[pos:pos+int(pos<end)] = [value]
        file.seek(0)
        file.writelines(lines)
        file.truncate()
    return True


def create_copy(old, image, folder, name, paths, saved_autostart):
    config = dict(old['Config'])
    host = dict(old['HostConfig'])
    host['Binds'] = [f'{folder / key}:{dest}:rw' for key, dest in paths.items()]
    config['HostConfig'] = host
    config['Image'] = image
    config['Env'] = [v for v in config['Env'] if v.split('=', 1)[0] not in ('tz','TZ')] + ['TZ='+MSK, 'tz='+MSK]
    if saved_autostart:
        config['Env'] = [v for v in config['Env'] if not v.startswith('wg_autostart=')] + ['wg_autostart=']
    original = (config.get('Entrypoint') or []) + (config.get('Cmd') or [])
    require(original, 'Исходная команда запуска не найдена.')
    wrapper = f'if test -e /data/{HOLD}; then {gate_cmd("hold")}; fi; exec "$@"'
    config['Entrypoint'] = ['/bin/sh', '-ec', wrapper, 'wg-clock']
    config['Cmd'] = original
    api = docker('version', '--format', '{{.Server.APIVersion}}').strip()
    require(re.fullmatch(r'1\.\d+', api), 'Неизвестная версия Docker Engine API.')
    engine = LocalEngine('localhost')
    try:
        engine.request('POST', f'/v{api}/containers/create?name={name}', json.dumps(config).encode(),
                       {'Content-Type':'application/json'})
        reply = engine.getresponse()
        body = reply.read()
        require(reply.status == 201, f'Docker отказался создать замену (HTTP {reply.status}).')
        return json.loads(body)['Id']
    finally:
        engine.close()


def interface_state(cid):
    result = {}
    for iface in sorted(docker('exec', cid, 'wg','show','interfaces').split()):
        require(re.fullmatch(r'[a-zA-Z0-9_.=-]{1,15}', iface), 'Неверное имя интерфейса.')
        props = {}
        for field in ('public-key','listen-port','fwmark','allowed-ips','preshared-keys','persistent-keepalive'):
            lines = sorted(docker('exec', cid, 'wg','show',iface,field).splitlines())
            props[field] = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
        addrs = json.loads(docker('exec',cid,'ip','-j','addr','show','dev',iface))[0]
        props['addresses'] = sorted((f"{a['local']}/{a['prefixlen']}" for a in addrs['addr_info'] if a['scope'] != 'link'))
        props['mtu'] = addrs['mtu']
        props['peers'] = len(docker('exec',cid,'wg','show',iface,'peers').split())
        result[iface] = props
    return result


def firewall(cid):
    # Compare rules in their original order; counters and generation dates are volatile.
    result = {}
    for binary in ['iptables-save','ip6tables-save']:
        s = docker('exec', cid, 'sh','-ec',
                   f'if command -v {binary} >/dev/null; then {binary}; fi')
        lines = [re.sub(r'\[\d+:\d+\]', '[0:0]', x) for x in s.splitlines() if x and not x.startswith('#') and GATE not in x]
        # Inserting the IPv6 maintenance rule may materialize a previously unused filter table.
        rules = []
        table = ''
        for line in lines:
            if line.startswith('*'):
                table = line
            elif line.startswith('-A') or (line.startswith(':') and ' ACCEPT ' not in line):
                rules.append(table + ' ' + line)
        result[binary] = rules
    return result


def ready(cid):
    script = "import http.client; c=http.client.HTTPConnection('127.0.0.1',10086,timeout=2); c.request('GET','/'); assert c.getresponse().status < 500"
    docker('exec', cid, 'python3','-c',script)


def panel_published():
    conn = http.client.HTTPConnection('127.0.0.1', int(PORT), timeout=3)
    try:
        conn.request('GET', '/')
        return conn.getresponse().status < 500
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def wait_interfaces(cid, original):
    limit = time.monotonic() + 120
    while True:
        try:
            ready(cid)
            break
        except RuntimeError:
            if time.monotonic() >= limit:
                raise
            time.sleep(2)
    for iface in original:
        if iface not in docker('exec',cid,'wg','show','interfaces').split():
            try:
                docker('exec',cid,'wg-quick','up',iface)
            except RuntimeError:
                if iface not in docker('exec',cid,'wg','show','interfaces').split():
                    raise
    require(interface_state(cid) == original, 'Пиры, идентичность, маршруты или MTU отличаются.')


def rollback(folder, s):
    require(not (folder/'COMMITTING').exists(), 'Панель уже открывалась. Для отката нужна повторная сверка данных.')
    if s.get('new'):
        cid = s['new']
        if inspect(cid)['State']['Running']:
            docker('stop','-t','30',cid)
        if inspect(cid)['Name'] == '/'+s['name']:
            docker('rename',cid,s['saved']+'-candidate')
    if inspect(s['old'])['Name'] != '/'+s['name']:
        docker('rename',s['old'],s['name'])
    if not inspect(s['old'])['State']['Running']:
        docker('start',s['old'])
    wait_interfaces(s['old'],s['interfaces'])
    require(firewall(s['old']) == s['firewall'], 'У исходного узла изменились правила фильтрации.')
    (folder/'ROLLED_BACK').touch()
    print('ROLLBACK_OK · прежняя панель и интерфейсы запущены.', flush=True)


def stage(expected, backups):
    name = 'wgdashboard'
    old = inspect(name)
    cid = old['Id']
    h = old['HostConfig']
    require(old['State']['Running'] and not old['Mounts'] and h['NetworkMode']=='bridge'
            and not h['Privileged'] and not h['AutoRemove'] and not h.get('VolumesFrom') and not h.get('Links')
            and not h.get('ContainerIDFile') and not (old['Config'].get('Labels') or {}).get('com.docker.compose.project'),
            'Исходный узел отличается от проверенной standalone-конфигурации.')
    require(set(h['PortBindings']) == {'10086/tcp','51820/udp'}, 'Состав опубликованных портов изменился.')
    require(any(x == 'TZ=Europe/Amsterdam' for x in old['Config']['Env']), 'Пояс исходной панели изменился.')
    backup = next((p for p in sorted(backups.glob('wg-clock-*/BACKUP_OK'), key=lambda p:p.stat().st_mtime, reverse=True)
                   if json.loads((p.parent/'container.json').read_text())['Id'] == cid), None)
    require(backup, 'Нет подтверждённой копии этого контейнера.')
    dbfile = backup.parent/'data/db/wgdashboard_job.db'
    with sqlite3.connect(dbfile.as_uri()+'?mode=ro&immutable=1', uri=True) as db:
        require(digest(jobs(db)) == expected, 'Копия не совпадает со сверенными сроками Shop.')
    before = interface_state(cid)
    require(before, 'Нет активных интерфейсов.')
    for iface in before:
        docker('exec',cid,'test','-s',f'/etc/wireguard/{iface}.conf')
    ready(cid)
    require(panel_published(), 'Панель недоступна на порту хоста до переключения.')
    state_root = Path('/var/lib/pushok-wg-clock')
    state_root.mkdir(mode=0o700,exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='switch-',dir=state_root))
    paths = {'data':'/data','wireguard':'/etc/wireguard'}
    if (backup.parent/'amneziawg').is_dir():
        paths['amneziawg'] = '/etc/amnezia/amneziawg'
    state = {'name':name,'old':cid,'saved':name+'-before-'+folder.name,'expected':expected,
             'interfaces':before,'firewall':firewall(cid),'ip':old['NetworkSettings']['Networks']['bridge']['IPAddress']}
    mark(folder/'state.json',state)
    print('Каталог переключения:',folder,'\nВосстановление при обрыве: python3 wg-msk.py rollback',folder,flush=True)
    try:
        docker('stop','-t','45',cid)
        for key, dest in paths.items():
            docker('cp','-a',cid+':'+dest,str(folder/key))
        state['target'] = convert(folder/'data/db/wgdashboard_job.db', expected)
        (folder/'data'/HOLD).touch()
        # The unnamed local snapshot preserves custom changes outside the mounted directories.
        saved_autostart = persist_autostart(folder/'data/wg-dashboard.ini',before)
        image = docker('commit',cid).strip()
        new = create_copy(old,image,folder,state['saved']+'-candidate',paths,saved_autostart)
        state['new'] = new
        mark(folder/'state.json',state)
        docker('rename',cid,state['saved'])
        docker('rename',new,name)
        docker('start',new)
        wait_interfaces(new,before)
        require(inspect(new)['NetworkSettings']['Networks']['bridge']['IPAddress'] == state['ip'], 'Изменился внутренний адрес узла.')
        require(firewall(new) == state['firewall'], 'Правила маршрутизации или фильтрации отличаются.')
        require(docker('exec',new,'date','+%Z %z').strip() == 'MSK +0300', 'Пояс контейнера не подтверждён.')
        with sqlite3.connect((folder/'data/db/wgdashboard_job.db').as_uri()+'?mode=ro',uri=True) as db:
            require(digest(jobs(db)) == state['target'], 'Сроки после запуска отличаются.')
        require(not panel_published(), 'Не подтверждена изоляция панели на время сверки.')
        (folder/'STAGED').touch()
        print('WG_STAGED_OK · МСК ·', {i: p['peers'] for i,p in before.items()},flush=True)
        print('После SHOP_MSK_OK выполните: python3 wg-msk.py finish',folder,flush=True)
    except (Exception, KeyboardInterrupt):
        rollback(folder,state)
        raise


def finish(folder,s):
    require((folder/'STAGED').exists() and not (folder/'ROLLED_BACK').exists(), 'Нет подготовленной панели.')
    new = s['new']
    require(inspect(s['name'])['Id'] == new, 'Имя узла теперь указывает на другой контейнер.')
    if (folder/'DONE').exists():
        ready(new)
        print('WG_MSK_OK · переключение уже завершено.')
        return
    wait_interfaces(new,s['interfaces'])
    require(docker('exec',new,'date','+%Z %z').strip() == 'MSK +0300', 'Пояс отличается.')
    if not (folder/'COMMITTING').exists():
        with sqlite3.connect((folder/'data/db/wgdashboard_job.db').as_uri()+'?mode=ro',uri=True) as db:
            require(digest(jobs(db)) == s['target'], 'Записанные сроки изменились.')
    (folder/'COMMITTING').touch()
    docker('exec',new,'sh','-ec',gate_cmd('open')+f'; rm -f /data/{HOLD}')
    ready(new)
    require(panel_published(), 'Внешний порт панели пока недоступен. Повторите finish.')
    (folder/'DONE').touch()
    print('WG_MSK_OK ·', {i: p['peers'] for i,p in s['interfaces'].items()}, '· панель открыта.', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_subparsers(dest='action',required=True)
    a=group.add_parser('stage');a.add_argument('expected');a.add_argument('--backups',type=Path,default=Path('/root'))
    a=group.add_parser('shop');a.add_argument('expected');a.add_argument('prefixes')
    for name in ('finish','rollback'):
        group.add_parser(name).add_argument('folder',type=Path)
    args=p.parse_args()
    if args.action=='shop':
        shop_change(args.expected,args.prefixes)
        return
    require(os.geteuid()==0,'Запустите на Docker-хосте от root.')
    os.umask(0o077)
    with open('/run/lock/pushok-wg-clock.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='stage':
            stage(args.expected,args.backups)
        else:
            folder = args.folder.resolve()
            require(folder.parent == Path('/var/lib/pushok-wg-clock'), 'Неверный каталог переключения.')
            s=json.loads((folder/'state.json').read_text())
            if args.action=='rollback':
                rollback(folder,s)
            else:
                finish(folder,s)


if __name__=='__main__':
    try:
        main()
    except (RuntimeError,OSError,sqlite3.Error,ValueError) as e:
        sys.exit(str(e))
