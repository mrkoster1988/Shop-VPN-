"""Управляемое обновление магазина. Используется только стандартная библиотека Python."""
import argparse
import ast
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

spec=importlib.util.spec_from_file_location('shop_installer',Path(__file__).with_name('install.py'))
i=importlib.util.module_from_spec(spec);spec.loader.exec_module(i)
VERSION='v0.055'
SUPPORTED={'v0.021','v0.022','v0.023','v0.024','v0.025','v0.026','v0.027','v0.028','v0.029','v0.030','v0.031','v0.032','v0.033','v0.034','v0.035','v0.036','v0.037','v0.038','v0.039','v0.040','v0.041','v0.042','v0.043','v0.044','v0.045','v0.046','v0.047','v0.048','v0.049','v0.050','v0.051','v0.052','v0.053','v0.054'}
IGNORE=shutil.ignore_patterns('.git','.env','.env.*','data','wg-data','.venv','__pycache__','.pytest_cache','backups','upgrades','*.pvb','*.db','*.db-wal','*.db-shm','*.zip')
CHECK_KEY="""import os,sys
from scripts.backup import environment
values,cipher=environment(sys.stdin.buffer.read())
if values['ENCRYPTION_KEY']!=os.environ.get('ENCRYPTION_KEY'):raise ValueError('key_mismatch')
print('key_matches')
"""
STAGE_SQLITE="""import os,shutil,stat,sys
from pathlib import Path
source=Path(sys.argv[1]);target=Path(sys.argv[2])
files=[source/'shop.db']
if (source/'shop.db-wal').exists():files.append(source/'shop.db-wal')
if any(p.is_symlink() or not stat.S_ISREG(p.stat().st_mode) for p in files):raise ValueError('Invalid database files')
if files[0].stat().st_size>128*1024*1024 or sum(p.stat().st_size for p in files)>256*1024*1024:raise ValueError('Database staging limit exceeded')
target.mkdir(mode=0o700)
for p in files:shutil.copyfile(p,target/p.name)
"""



def run(args,*,input=None,env=None,timeout=900):
    result=subprocess.run(args,input=input,capture_output=True,check=True,timeout=timeout,env=env)
    return result.stdout.decode().strip()



def receive_archive(args,path):
    """Stream binary data through exec: docker cp cannot reliably read tmpfs files."""
    path=Path(path);fd,temp=tempfile.mkstemp(prefix='.archive-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o600)
            subprocess.run(args,stdout=stream,stderr=subprocess.PIPE,check=True,timeout=120)
            stream.flush();os.fsync(stream.fileno())
        if Path(temp).stat().st_size>180*1024*1024:raise ValueError('Архив превышает лимит размера')
        os.link(temp,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:os.unlink(temp)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def private_file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Нужен обычный файл установки')
    return path.read_bytes()


def write(path,value):i.atomic_text(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def journal(path):
    value=json.loads(private_file(path))
    if not isinstance(value,dict):raise ValueError('Некорректный журнал обновления')
    if not re.fullmatch(r'[a-f0-9]{24}',value.get('operation','')):raise ValueError('Некорректный журнал обновления')
    if value.get('previous') not in SUPPORTED or value.get('target') not in SUPPORTED|{VERSION} or value['previous']>=value['target']:
        raise ValueError('Неизвестный переход в журнале')
    phases={'building','stopping','copying','verifying_backup','backed_up','migrating','switching','ready','activating','activation_failed','committed','rolled_back','recovery_required'}
    if value.get('phase') not in phases or type(value.get('activation_started')) is not bool:raise ValueError('Некорректная фаза журнала')
    return value


def terminal(root):
    if not (root/'upgrade.json').exists():return None
    previous=journal(root/'upgrade.json')
    if previous['phase'] not in ('committed','rolled_back'):
        raise ValueError('Сначала завершите предыдущую операцию инструментом '+previous['target']+': status/activate/rollback')
    return previous


def proof(root,settings):
    managed=settings['managed_upgrade'];operation=managed.get('operation','')
    if not re.fullmatch(r'[a-f0-9]{24}',operation):raise ValueError('Некорректная привязка managed-upgrade')
    work=root/'upgrades'/operation
    if work.resolve()!=work:raise ValueError('Симлинки в истории обновлений недопустимы')
    if managed.get('source')!=str(work/'source') or not (work/'source').is_dir() or (work/'source').is_symlink():raise ValueError('Не найдены сохранённые исходники установки')
    if managed.get('volume')!=settings['project']+'-upgrade-'+operation or managed.get('image')!='pushok-upgrade:'+operation:
        raise ValueError('Изменена привязка image/volume')
    current=terminal(root)
    saved=current if current and current['operation']==operation else journal(work/'activation.json')
    if (work/'activation.json').exists() and journal(work/'activation.json')!=saved:raise ValueError('История завершённой активации отличается')
    if saved['operation']!=operation or saved['phase']!='committed' or not saved['activation_started'] or saved['target']!=settings['version']:
        raise ValueError('Не подтверждена завершённая активация установки')
    if saved.get('new_marker_sha')!=sha(private_file(root/'installer.json')):raise ValueError('Метаданные managed-upgrade изменены')
    actual=sha(private_file(root/'compose.install.json'))
    accepted={saved.get('active_sha')}
    if current and current['phase']=='rolled_back' and current['previous']==settings['version'] and current.get('old_marker_sha')==sha(private_file(root/'installer.json')):
        accepted.add(current.get('rollback_sha'))
    if actual not in accepted:raise ValueError('Compose не соответствует завершённой активации или откату')
    return saved


def remember_activation(root,value):
    if value['phase']!='committed':raise ValueError('Нельзя сохранять незавершённую активацию')
    path=root/'upgrades'/value['operation']/'activation.json'
    if path.exists() or path.is_symlink():
        if json.loads(private_file(path))!=value:raise ValueError('Сохранённая история активации отличается')
    else:write(path,value)


def load(root):
    settings=json.loads(private_file(root/'installer.json'))
    if not isinstance(settings,dict):raise ValueError('Некорректные метаданные установки')
    if (root/'upgrades').is_symlink():raise ValueError('Симлинки в истории обновлений недопустимы')
    if settings.get('version') not in SUPPORTED or settings['version']>=VERSION:raise ValueError('Поддерживаются исходные версии v0.021–v0.054 → '+VERSION)
    if not re.fullmatch(r'pushok-[a-f0-9]{10}',settings.get('project','')):raise ValueError('Неизвестный проект Compose')
    i.public_url(settings['url'])
    if not isinstance(settings.get('port'),int) or not 1024<=settings['port']<=65535:raise ValueError('Некорректный порт установки')
    config=json.loads(private_file(root/'compose.install.json'));shop=config['services']['shop']
    managed=settings.get('managed_upgrade')
    if managed is not None and (not isinstance(managed,dict) or not managed):raise ValueError('Некорректная привязка managed-upgrade')
    if managed:
        proof(root,settings)
        if shop.get('environment',{}).get('DATA_DIR')!='/app/data/restored' or shop.get('volumes')!=['upgrade_data:/app/data']:
            raise ValueError('Изменённая схема managed-upgrade')
        if config.get('volumes',{}).get('upgrade_data')!={'external':True,'name':managed['volume']}:raise ValueError('Изменён текущий volume')
        if shop.get('image')!=managed['image'] and not re.fullmatch(r'sha256:[a-f0-9]{64}',shop.get('image','')):raise ValueError('Изменён текущий образ')
    elif shop.get('environment',{}).get('DATA_DIR')!='/app/data' or shop.get('volumes')!=['shop_data:/app/data']:
        raise ValueError('Изменённая схема данных требует ручного плана обновления')
    if shop.get('network_mode')!='host' or shop.get('env_file')!=[str(root/'.env')]:raise ValueError('Изменённая конфигурация Compose не поддерживается')
    if config.get('volumes',{}).get('shop_data')!={}:raise ValueError('Изменённое имя volume не поддерживается')
    expected=json.loads(i.compose(settings,root));normalized=json.loads(json.dumps(config));normalized_shop=normalized['services']['shop']
    if managed:
        normalized['volumes'].pop('upgrade_data');normalized_shop['volumes']=['shop_data:/app/data'];normalized_shop['environment']['DATA_DIR']='/app/data'
    if 'image' in normalized_shop:
        image=normalized_shop.pop('image')
        if not managed and not re.fullmatch(r'sha256:[a-f0-9]{64}',image):raise ValueError('Нестандартный образ требует ручного плана')
        normalized_shop['build']=str(root/'source')
    for key in ('BACKGROUND_TASKS','MAINTENANCE_MODE'):
        if key in normalized_shop['environment']:
            if normalized_shop['environment'].pop(key) not in ('true','false'):raise ValueError('Некорректный режим работы магазина')
    if normalized!=expected:raise ValueError('Изменённая конфигурация Compose требует ручного обновления')
    previous=terminal(root)
    if previous:
        if previous['phase']=='committed' and previous['target']!=settings['version']:raise ValueError('Версия установки расходится с завершённым обновлением')
        if previous['phase']=='rolled_back' and (previous['previous']!=settings['version'] or previous.get('old_marker_sha')!=sha(private_file(root/'installer.json'))):raise ValueError('Версия установки расходится с откатом')
    private_file(root/'.env')
    return settings,config


def source_version(source):
    tree=ast.parse(private_file(source/'scripts/install.py').decode())
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Версия исходников не найдена')


def check_source(source,root):
    if source_version(source)!=VERSION:raise ValueError('Исходники должны соответствовать '+VERSION)
    for name in ('Dockerfile','app/main.py','scripts/backup.py'):private_file(source/name)
    if source==root or root in source.parents:raise ValueError('Используйте отдельные исходники релиза вне установки')
    count=size=0
    for directory,dirs,files in os.walk(source):
        ignored=IGNORE(directory,dirs+files);dirs[:]=[d for d in dirs if d not in ignored]
        for name in dirs+[f for f in files if f not in ignored]:
            path=Path(directory)/name
            if path.is_symlink():raise ValueError('Симлинки в исходниках не поддерживаются')
            if path.is_file():count+=1;size+=path.stat().st_size
    if count>2000 or size>32*1024*1024:raise ValueError('Исходники превышают лимит релиза')


def compose(root,settings,file=None):
    return ['docker','compose','--project-name',settings['project'],'--env-file',str(root/'.env'),'-f',str(file or root/'compose.install.json')]


def state(root):
    value=journal(root/'upgrade.json')
    if value['target']!=VERSION:raise ValueError('Для завершения этой операции нужен инструмент '+value['target'])
    return value


def report(root):
    if not (root/'upgrade.json').exists():return {'phase':'no_upgrade'}
    s=journal(root/'upgrade.json')
    return {k:s[k] for k in ('operation','phase','previous','target','activation_started','failure_stage') if k in s}


def save(root,s,phase):s['phase']=phase;write(root/'upgrade.json',s)


@contextmanager
def locked(root):
    fd=os.open(root/'.install.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Установщик или обновление уже выполняется') from None
        yield
    finally:os.close(fd)


def unchanged(root,s):
    if sha(private_file(root/'.env'))!=s['env_sha']:raise ValueError('.env изменён; автоматические действия остановлены')
    if sha(private_file(root/'compose.install.json')) not in {s['old_compose_sha'],s.get('candidate_sha'),s.get('active_sha'),s.get('rollback_sha')}:
        raise ValueError('Compose изменён вне обновления; нужен ручной разбор')
    if sha(private_file(root/'installer.json')) not in {s['old_marker_sha'],s.get('new_marker_sha')}:
        raise ValueError('Метаданные установщика изменены вне обновления')


def password(args):
    if args.password_file:
        fd=os.open(args.password_file,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError('Файл пароля должен иметь права 0600')
            raw=stream.read(4097)
        if len(raw)>4096:raise ValueError('Слишком длинный пароль')
        value=raw.decode().rstrip('\r\n')
    elif 'PUSHOK_BACKUP_PASSWORD' in os.environ:value=os.environ.pop('PUSHOK_BACKUP_PASSWORD')
    else:
        with open('/dev/tty','r+') as tty:
            value=getpass.getpass('Отдельный пароль резервной копии (от 16 символов): ',stream=tty)
            if value!=getpass.getpass('Повторите пароль: ',stream=tty):raise ValueError('Пароли не совпали')
    if len(value)<16:raise ValueError('Нужно не менее 16 символов в пароле копии')
    return value


def plan(root,source):
    terminal(root)
    settings,config=load(root);check_source(source,root)
    terminal(root)
    return {'previous':settings['version'],'target':VERSION,'url':settings['url'],'downtime':True,
            'new_volume':True,'activation':'explicit','backup':'encrypted','plan_only':True}


def health(settings):
    i.wait_health('http://127.0.0.1:'+str(settings['port'])+urlsplit(settings['url']).path+'/health')
    i.wait_health(settings['url']+'/health')


def rollback(root,s):
    if s['phase']=='rolled_back':return report(root)
    if s['activation_started']:raise ValueError('После начала активации откат базы заблокирован; нужна сверка новых оплат и VPN-состояний')
    unchanged(root,s);work=root/'upgrades'/s['operation']
    settings=json.loads(private_file(work/'installer.before.json'))
    old=private_file(work/'compose.before.json')
    if sha(old)!=s['old_compose_sha'] or sha(private_file(work/'installer.before.json'))!=s['old_marker_sha']:
        raise ValueError('Сохранённые метаданные повреждены')
    if s['stopped']:
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',s['old_image']):raise ValueError('Некорректный старый образ')
        run(compose(root,settings)+['stop','--timeout','30','shop'])
        config=json.loads(old);config['services']['shop'].pop('build',None);config['services']['shop']['image']=s['old_image']
        encoded=(json.dumps(config,ensure_ascii=False,indent=2)+'\n').encode();s['rollback_sha']=sha(encoded);write(root/'upgrade.json',s)
        i.atomic_text(root/'compose.install.json',encoded.decode())
        i.atomic_text(root/'installer.json',private_file(work/'installer.before.json').decode())
        run(compose(root,settings)+['up','-d','--no-deps','--no-build','--wait','--wait-timeout','120','shop'])
        health(settings)
    save(root,s,'rolled_back')
    return report(root)


def prepare(root,source,secret):
    plan(root,source);settings,config=load(root);base=compose(root,settings)
    cid=run(base+['ps','-q','shop'])
    if not re.fullmatch(r'[a-f0-9]{12,64}',cid):raise ValueError('Нужен один работающий контейнер shop')
    old_image=run(['docker','inspect','--format','{{.Image}}',cid])
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',old_image):raise ValueError('Не найден локальный образ магазина')
    mounts=json.loads(run(['docker','inspect','--format','{{json .Mounts}}',cid]))
    matches=[m for m in mounts if m.get('Destination')=='/app/data' and m.get('Type')=='volume']
    expected_volume=settings['managed_upgrade']['volume'] if settings.get('managed_upgrade') else settings['project']+'_shop_data'
    if len(matches)!=1 or matches[0].get('Name')!=expected_volume or matches[0].get('RW') is not True:raise ValueError('Не найден исходный named volume')
    try:
        key_check=run(['docker','exec','-i',cid,'python','-c',CHECK_KEY],input=private_file(root/'.env'),timeout=30)
    except subprocess.SubprocessError:raise ValueError('Не подтверждён ключ текущего shop и .env; подготовка не начата') from None
    if key_check!='key_matches':raise ValueError('Не подтверждён ключ текущего shop и .env; подготовка не начата')
    operation=secrets.token_hex(12);work=root/'upgrades'/operation
    (root/'upgrades').mkdir(exist_ok=True,mode=0o700);work.mkdir(mode=0o700)
    old_compose=private_file(root/'compose.install.json');old_marker=private_file(root/'installer.json')
    i.atomic_text(work/'compose.before.json',old_compose.decode());i.atomic_text(work/'installer.before.json',old_marker.decode())
    previous=terminal(root)
    if previous:write(work/'journal.before.json',previous)
    if settings.get('managed_upgrade'):remember_activation(root,proof(root,settings))
    s={'operation':operation,'previous':settings['version'],'target':VERSION,'phase':'building','activation_started':False,
       'stopped':False,'env_sha':sha(private_file(root/'.env')),'old_compose_sha':sha(old_compose),'old_marker_sha':sha(old_marker),
       'old_image':old_image,'old_volume':matches[0]['Name'],'new_volume':settings['project']+'-upgrade-'+operation,
       'new_image':'pushok-upgrade:'+operation}
    save(root,s,'building');helper=None
    try:
        shutil.copytree(source,work/'source',ignore=IGNORE)
        run(['docker','build','--tag',s['new_image'],str(work/'source')])
        unchanged(root,s)
        # Persist stop intent first: recovery is safe even if stop succeeded but the host died.
        s['stopped']=True;save(root,s,'stopping')
        run(base+['stop','--timeout','30','shop'])
        run(['docker','volume','create',s['new_volume']])
        run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--cap-add','CHOWN',
             '--user','0:0','-v',s['new_volume']+':/upgrade-data',s['old_image'],'python','-c',
             "import os;os.chmod('/upgrade-data',0o700);os.chown('/upgrade-data',10001,10001)"])
        helper=run(['docker','run','-d','--rm','--network','none','--read-only','--cap-drop','ALL',
                    '--security-opt','no-new-privileges:true','--user','10001:10001','--tmpfs','/tmp:rw,nosuid,noexec,size=768m',
                    '--volumes-from',cid+':ro','-v',s['new_volume']+':/upgrade-data',old_image,'python','-c','import time;time.sleep(900)'])
        if not re.fullmatch(r'[a-f0-9]{12,64}',helper):helper=None;raise ValueError('Не создан контейнер копирования')
        save(root,s,'copying')
        run(['docker','exec',helper,'python','-c',STAGE_SQLITE,config['services']['shop']['environment']['DATA_DIR'],'/tmp/source'])
        secure_env={**os.environ,'PUSHOK_BACKUP_PASSWORD':secret}
        backup=['docker','exec','-i','-e','PUSHOK_BACKUP_PASSWORD',helper,'python','scripts/backup.py']
        run(backup+['create','--database','/tmp/source/shop.db','--env-stdin','--output','/tmp/shop.pvb'],input=private_file(root/'.env'),env=secure_env)
        receive_archive(['docker','exec',helper,'python','-c',"import shutil,sys;shutil.copyfileobj(open(sys.argv[1],'rb'),sys.stdout.buffer)",'/tmp/shop.pvb'],work/'backup.pvb')
        # Verify the persisted host copy, not just the helper's original file.
        run(['docker','exec','-i',helper,'python','-c',"import os,sys;f=os.open('/tmp/saved.pvb',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);s=os.fdopen(f,'wb');s.write(sys.stdin.buffer.read());s.close()"],input=private_file(work/'backup.pvb'))
        save(root,s,'verifying_backup')
        run(backup+['verify','/tmp/saved.pvb'],env=secure_env)
        run(backup+['restore','/tmp/saved.pvb','--target','/upgrade-data/restored'],env=secure_env)
        save(root,s,'backed_up')
        save(root,s,'migrating')
        migrate="import io,os,sys;from dotenv import load_dotenv;raw=sys.stdin.buffer.read();load_dotenv(stream=io.StringIO(raw.decode()),override=True);os.environ['DATA_DIR']='/app/data/restored';os.environ['BACKGROUND_TASKS']='false';from app.db import init_db;init_db();from scripts.backup import validate;validate('/app/data/restored/shop.db',raw);print('validated')"
        run(['docker','run','--rm','-i','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
             '--tmpfs','/tmp','--user','10001:10001','-v',s['new_volume']+':/app/data',s['new_image'],'python','-c',migrate],input=private_file(root/'.env'))
        unchanged(root,s)
        candidate=json.loads(old_compose);shop=candidate['services']['shop'];shop.pop('build',None);shop['image']=s['new_image']
        shop['environment'].update(DATA_DIR='/app/data/restored',MAINTENANCE_MODE='true',BACKGROUND_TASKS='false')
        shop['volumes']=['upgrade_data:/app/data'];candidate['volumes']['upgrade_data']={'external':True,'name':s['new_volume']}
        encoded=(json.dumps(candidate,ensure_ascii=False,indent=2)+'\n').encode()
        new_marker={**settings,'version':VERSION,'managed_upgrade':{'operation':operation,'source':str(work/'source'),'image':s['new_image'],'volume':s['new_volume']}}
        marker=(json.dumps(new_marker,ensure_ascii=False,indent=2)+'\n').encode()
        s.update(candidate_sha=sha(encoded),new_marker_sha=sha(marker));save(root,s,'switching')
        i.atomic_text(root/'compose.install.json',encoded.decode());i.atomic_text(root/'installer.json',marker.decode())
        run(base+['up','-d','--no-deps','--no-build','--wait','--wait-timeout','120','shop']);health(settings)
        save(root,s,'ready')
        return report(root)
    except Exception:
        s['failure_stage']=s['phase'];write(root/'upgrade.json',s)
        try:rollback(root,s)
        except Exception:save(root,s,'recovery_required')
        raise
    finally:
        if helper:
            try:run(['docker','rm','-f',helper],timeout=30)
            except Exception:pass


def activate(root,s):
    if s['phase']=='committed':
        remember_activation(root,s);return report(root)
    if s['phase']!='ready':raise ValueError('Активация разрешена только после успешной подготовки')
    unchanged(root,s);work=root/'upgrades'/s['operation'];settings=json.loads(private_file(work/'installer.before.json'))
    old_raw=private_file(work/'compose.before.json')
    if sha(old_raw)!=s['old_compose_sha'] or sha(private_file(work/'installer.before.json'))!=s['old_marker_sha']:raise ValueError('Сохранённые метаданные повреждены')
    candidate=json.loads(private_file(root/'compose.install.json'));old=json.loads(old_raw)
    for key in ('MAINTENANCE_MODE','BACKGROUND_TASKS'):
        if key in old['services']['shop']['environment']:candidate['services']['shop']['environment'][key]=old['services']['shop']['environment'][key]
        else:candidate['services']['shop']['environment'].pop(key,None)
    encoded=(json.dumps(candidate,ensure_ascii=False,indent=2)+'\n').encode()
    s['activation_started']=True;s['active_sha']=sha(encoded);save(root,s,'activating')
    try:
        i.atomic_text(root/'compose.install.json',encoded.decode())
        run(compose(root,settings)+['up','-d','--no-deps','--no-build','--wait','--wait-timeout','120','shop']);health(settings)
        save(root,s,'committed');remember_activation(root,s);return report(root)
    except Exception:s['failure_stage']='activating';save(root,s,'activation_failed');raise


def main(argv=None):
    parser=argparse.ArgumentParser(description='Обновление v0.021–v0.054 → '+VERSION+' в новый volume, с резервной копией и отдельной активацией')
    parser.add_argument('command',choices=['plan','prepare','status','activate','rollback'])
    parser.add_argument('--dir',default='/opt/pushok-vpn');parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--password-file')
    args=parser.parse_args(argv);root=i.install_path(args.dir);source=args.source.resolve()
    if args.command=='plan':result=plan(root,source)
    elif args.command=='status':result=report(root)
    else:
        if os.geteuid()!=0:raise ValueError('Нужны права root')
        with locked(root):
            if args.command=='prepare':result=prepare(root,source,password(args))
            elif args.command=='activate':result=activate(root,state(root))
            else:result=rollback(root,state(root))
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:
        print('Обновление прервано. Выполните status; для незавершённой активации нужен ручной разбор.',file=sys.stderr);raise SystemExit(1)
    except ValueError as exc:print('Обновление остановлено: '+str(exc),file=sys.stderr);raise SystemExit(1)
    except (OSError,subprocess.SubprocessError,KeyError,TypeError,RuntimeError,SyntaxError):
        print('Обновление остановлено. Проверьте status; подробности Docker и секреты не выводятся.',file=sys.stderr);raise SystemExit(1)
