"""Служба обновления на VPS. Узкий API на loopback, журнал вне БД магазина."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_source

ACTIVE = ('queued', 'running')
TERMINAL = ('succeeded', 'failed', 'rolled_back', 'recovery_required', 'interrupted')
SAFE_PHASES = {'building','stopping','copying','verifying_backup','backed_up','migrating','switching','ready',
               'activating','activation_failed','committed','rolled_back','recovery_required'}


class Rejected(ValueError):
    pass


def private_dir(path):
    path = Path(path)
    if path.resolve() != path or not path.is_dir() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise Rejected('unsafe_directory')
    return path


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise Rejected('invalid_local_state')
    return json.loads(path.read_text())


def fingerprint(root):
    result = {}
    for name in ('.env', 'installer.json', 'compose.install.json'):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise Rejected('invalid_installation')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def run_upgrade(source, root, command, password_file=None, expected=None):
    args = [sys.executable, str(source / 'scripts/upgrade.py'), command, '--dir', str(root), '--source', str(source)]
    if password_file:
        args += ['--password-file', str(password_file)]
    if expected:
        args += ['--expected-fingerprint', hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()).hexdigest()]
    # Вывод Docker не публикуется; никаких команд/путей из HTTP-запроса.
    with tempfile.TemporaryFile() as output:
        environment = {'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                       'LANG':'C.UTF-8', 'PYTHONDONTWRITEBYTECODE':'1'}
        if os.getenv('SSL_CERT_FILE'):
            environment['SSL_CERT_FILE'] = os.environ['SSL_CERT_FILE']
        proc = subprocess.Popen(args, stdout=output, stderr=subprocess.DEVNULL, start_new_session=True,
                                env=environment)
        try:
            proc.wait(timeout=3600 if command == 'prepare' else 300)
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        if proc.returncode or output.tell() > 65536:
            raise Rejected('upgrade_command_failed')
        output.seek(0)
        return json.load(output)


class Controller:
    def __init__(self, root, config, *, recover=True):
        self.root = private_dir(root)
        self.work = private_dir(Path(config['work']))
        self.password_file = self.root / 'updater/backup-password'
        self.database = self.work / 'jobs.db'
        if self.database.is_symlink():
            raise Rejected('unsafe_database')
        with self.db() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                plan_id TEXT, tag TEXT NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                created INTEGER NOT NULL, updated INTEGER NOT NULL, data TEXT NOT NULL DEFAULT '{}')''')
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_apply ON jobs(plan_id) WHERE kind='apply'")
            if recover:
                conn.execute("UPDATE jobs SET state='interrupted',error='service_restarted',updated=? WHERE state IN ('queued','running')", (int(time.time()),))
        self.database.chmod(0o600)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.database, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, job_id):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise Rejected('unknown_operation')
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def upgrade_state(self):
        path = self.root / 'upgrade.json'
        if not path.exists():
            return {}
        raw = read_json(path)
        return {key:raw.get(key) for key in ('phase','activation_started','previous','target','operation')}

    def status(self):
        with self.db() as conn:
            row = conn.execute('SELECT id FROM jobs ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
        result = {'enabled':True, 'reachable':True, 'job':None}
        if row:
            job = self.get(row['id'])
            public = {k:job[k] for k in ('id','kind','plan_id','tag','state','error','created','updated')}
            public.update({k:job['data'][k] for k in ('previous','commit','expires') if k in job['data']})
            public['can_apply'] = job['kind'] == 'plan' and job['state'] == 'ready' and job['data']['expires'] > time.time()
            if job['kind'] in ('apply','rollback'):
                journal = self.upgrade_state()
                # Фаза может относиться только к этой попытке, а не старому обновлению.
                plan = self.get(job['plan_id'])
                if (journal.get('target') == job['tag'] and journal.get('previous') == plan['data']['previous']
                        and journal.get('operation') != plan['data'].get('prior_operation')):
                    public['phase'] = journal.get('phase') if journal.get('phase') in SAFE_PHASES else 'unknown'
                    public['activation_started'] = journal.get('activation_started') is True
            result['job'] = public
        return result

    def submit(self, kind, payload):
        expected = {'request_id','tag'} if kind == 'plan' else {'request_id','plan_id','tag'}
        if kind not in ('plan','apply','rollback') or not isinstance(payload, dict) or set(payload) != expected:
            raise Rejected('invalid_request')
        for key in ('request_id',) if kind == 'plan' else ('request_id','plan_id'):
            if not isinstance(payload[key], str) or str(uuid.UUID(payload[key])) != payload[key]:
                raise Rejected('invalid_request_id')
        update_source.version(payload['tag'])
        now = int(time.time())
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM jobs WHERE id=?', (payload['request_id'],)).fetchone()
            if previous:
                if (previous['kind'], previous['tag'], previous['plan_id']) != (kind, payload['tag'], payload.get('plan_id')):
                    raise Rejected('request_id_conflict')
                return self.status()
            if conn.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
                raise Rejected('operation_in_progress')
            journal = self.upgrade_state()
            if kind in ('plan','apply') and journal and journal.get('phase') not in ('committed','rolled_back'):
                raise Rejected('recovery_required')
            if kind != 'plan':
                plan = self.get(payload['plan_id'])
                if plan['kind'] != 'plan' or plan['state'] != 'ready' or plan['tag'] != payload['tag']:
                    raise Rejected('invalid_plan')
                if kind == 'apply':
                    if plan['data']['expires'] <= now or fingerprint(self.root) != plan['data']['fingerprint']:
                        raise Rejected('plan_expired_or_changed')
                    if conn.execute("SELECT 1 FROM jobs WHERE plan_id=? AND kind='apply'", (plan['id'],)).fetchone():
                        raise Rejected('plan_already_used')
                else:
                    if (journal.get('activation_started') is not False or journal.get('target') != plan['tag']
                            or journal.get('previous') != plan['data']['previous']
                            or journal.get('operation') == plan['data'].get('prior_operation')
                            or not conn.execute("SELECT 1 FROM jobs WHERE plan_id=? AND kind='apply'", (plan['id'],)).fetchone()):
                        raise Rejected('rollback_forbidden')
            else:
                current = read_json(self.root / 'installer.json')['version']
                if update_source.version(payload['tag']) <= update_source.version(current):
                    raise Rejected('not_newer')
            conn.execute('INSERT INTO jobs(id,kind,plan_id,tag,state,created,updated) VALUES (?,?,?,?,?,?,?)',
                         (payload['request_id'],kind,payload.get('plan_id'),payload['tag'],'queued',now,now))
        return self.status()

    def finish(self, job, state, data=None, error=''):
        with self.db() as conn:
            conn.execute('UPDATE jobs SET state=?,data=?,error=?,updated=? WHERE id=?',
                         (state,json.dumps(data or job['data']),error,int(time.time()),job['id']))

    def process_one(self):
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if not row:
                return False
            conn.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (int(time.time()),row['id']))
        job = self.get(row['id'])
        try:
            if job['kind'] == 'plan':
                before = fingerprint(self.root)
                prior = self.upgrade_state().get('operation')
                source, commit = update_source.fetch(job['tag'], self.work / job['id'])
                result = run_upgrade(source, self.root, 'plan')
                if (result.get('target') != job['tag'] or result.get('plan_only') is not True
                        or result.get('activation') != 'explicit' or result.get('backup') != 'encrypted'
                        or result.get('host_updater_protocol') != 1
                        or fingerprint(self.root) != before):
                    raise Rejected('invalid_plan_result')
                self.finish(job, 'ready', {'source':str(source),'commit':commit,'previous':result['previous'],
                                          'fingerprint':before,'prior_operation':prior,'expires':int(time.time()) + 900})
            else:
                plan = self.get(job['plan_id'])
                source = Path(plan['data']['source'])
                if source.resolve() != source or self.work / plan['id'] not in source.parents:
                    raise Rejected('invalid_saved_source')
                if job['kind'] == 'apply':
                    if plan['data']['expires'] <= time.time() or fingerprint(self.root) != plan['data']['fingerprint']:
                        raise Rejected('plan_expired_or_changed')
                    prepared = run_upgrade(source, self.root, 'prepare', self.password_file, plan['data']['fingerprint'])
                    if prepared.get('phase') != 'ready':
                        raise Rejected('prepare_not_ready')
                    result = run_upgrade(source, self.root, 'activate')
                    if result.get('phase') != 'committed':
                        raise Rejected('activation_not_confirmed')
                    self.finish(job, 'succeeded')
                else:
                    result = run_upgrade(source, self.root, 'rollback')
                    if result.get('phase') != 'rolled_back':
                        raise Rejected('rollback_not_confirmed')
                    self.finish(job, 'rolled_back')
        except Exception:
            # Никаких stdout/stderr Docker, токенов или ответа GitHub в публичном статусе.
            state = 'failed'
            if job['kind'] != 'plan':
                journal = self.upgrade_state()
                if journal.get('operation') != self.get(job['plan_id'])['data'].get('prior_operation'):
                    if journal.get('phase') == 'rolled_back':
                        state = 'rolled_back'
                    elif journal:
                        state = 'recovery_required'
            self.finish(job, state, error='operation_failed')
        return True


def handler(controller, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def respond(self, code, data):
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            # Ни browser-origin, ни cookie-аутентификация службой не принимаются.
            return (not self.headers.get('Origin') and not self.headers.get('Transfer-Encoding')
                    and hmac.compare_digest(self.headers.get('Authorization','').encode('utf-8'), ('Bearer ' + token).encode('ascii')))

        def do_GET(self):
            if not self.authorized():
                return self.respond(403, {'error':'forbidden'})
            if self.path != '/v1/status':
                return self.respond(404, {'error':'not_found'})
            self.respond(200, controller.status())

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {'error':'forbidden'})
            if self.path not in ('/v1/plan','/v1/apply','/v1/rollback'):
                return self.respond(404, {'error':'not_found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048:
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                result = controller.submit(self.path.rsplit('/',1)[1], data)
            except (ValueError, KeyError, TypeError):
                return self.respond(409, {'error':'request_rejected'})
            self.respond(202, result)
    return Handler


def serve(root):
    config = read_json(root / 'updater/config.json')
    if not re.fullmatch(r'[a-f0-9]{64}', config.get('token','')) or type(config.get('port')) is not int or not 1024 <= config['port'] <= 65535:
        raise Rejected('invalid_configuration')
    lock = os.open(root / 'updater/service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        controller = Controller(root, config)
        server = ThreadingHTTPServer(('127.0.0.1',config['port']), handler(controller, config['token']))
        def worker():
            while True:
                try:
                    controller.process_one()
                except Exception:
                    # Не оставляем живой HTTP-сервис с погибшим worker: systemd
                    # перезапустит процесс и пометит работу interrupted.
                    os._exit(1)
                time.sleep(1)
        threading.Thread(target=worker,daemon=True).start()
        server.serve_forever()
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description='Локальная служба обновления Pushok')
    parser.add_argument('command', choices=['serve','status','rollback'])
    parser.add_argument('--dir', required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Rejected('root_required')
    os.umask(0o077)
    root = private_dir(args.dir)
    if args.command == 'serve':
        serve(root)
    else:
        import urllib.request
        config = read_json(root / 'updater/config.json')
        url = 'http://127.0.0.1:' + str(config['port']) + '/v1/'
        headers = {'Authorization':'Bearer ' + config['token'], 'Content-Type':'application/json'}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url + 'status', headers=headers), timeout=5) as response:
            result = json.load(response)
        if args.command == 'rollback':
            job = result.get('job') or {}
            if job.get('kind') not in ('apply','rollback'):
                raise Rejected('no_update_to_rollback')
            payload = {'request_id':str(uuid.uuid4()),'plan_id':job['plan_id'],'tag':job['tag']}
            with opener.open(urllib.request.Request(url + 'rollback', data=json.dumps(payload).encode(), headers=headers), timeout=5) as response:
                result = json.load(response)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Служба обновления недоступна или действие отклонено. Проверьте локальную установку и журнал операции.', file=sys.stderr)
        raise SystemExit(1)
