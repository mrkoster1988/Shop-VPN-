"""Encrypted local snapshots. Never imports app config, starts workers or extracts paths."""
import argparse
import base64
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from contextlib import closing
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv.parser import parse_stream

VERSION='v0.021'
MAGIC=b'PUSHOKB1'
MAX_DB=128*1024*1024
MAX_ENV=1024*1024
MAX_ARCHIVE=180*1024*1024
ENCRYPTED={
    'servers':('settings',), 'subscriptions':('secret','credential_id'),
    'devices':('secret','credential_id'), 'subscription_links':('token_secret',),
    'primary_rotations':('old_credential_id','new_credential_id'),
    'device_rotations':('old_credential_id','new_credential_id')}
REQUIRED={'users','sessions','tickets','orders','plans','meta',*ENCRYPTED}

class BackupError(ValueError):pass


def environment(raw):
    if not raw or len(raw)>MAX_ENV:raise BackupError('Проверьте размер файла .env')
    values={}
    for binding in parse_stream(io.StringIO(raw.decode('utf-8'))):
        if binding.error:raise BackupError('Некорректный формат .env')
        if binding.key:
            if binding.key in values:raise BackupError('Повторяющиеся переменные в .env недопустимы')
            values[binding.key]=binding.value
    try:cipher=Fernet(values['ENCRYPTION_KEY'].encode())
    except (KeyError,AttributeError,ValueError):raise BackupError('В .env нужен корректный ENCRYPTION_KEY') from None
    return values,cipher


def password_key(password,salt):
    if not isinstance(password,str) or len(password)<16:raise BackupError('Пароль копии должен содержать не менее 16 символов')
    return base64.urlsafe_b64encode(PBKDF2HMAC(algorithm=hashes.SHA256(),length=32,salt=salt,iterations=600000).derive(password.encode()))


def connect(path):
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    conn.execute('PRAGMA trusted_schema=OFF')
    return closing(conn)


def validate(database,env):
    _,cipher=environment(env)
    with connect(database) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise BackupError('Проверка целостности SQLite не пройдена')
        if conn.execute('PRAGMA foreign_key_check').fetchone():raise BackupError('Нарушены связи базы данных')
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED<=tables:raise BackupError('Неполная схема базы магазина')
        encrypted=0
        for table,columns in ENCRYPTED.items():
            actual={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            if not set(columns)<=actual:raise BackupError('Неполная схема шифрования; нужен актуальный формат базы')
            for row in conn.execute('SELECT '+','.join(columns)+' FROM '+table):
                for value in row:
                    if value is not None:
                        try:cipher.decrypt(value.encode()).decode('utf-8')
                        except (InvalidToken,AttributeError,UnicodeError):raise BackupError('Ключ .env не расшифровывает все приватные записи') from None
                        encrypted+=1
        return {'tables':len(tables),'encrypted_values':encrypted}


def publish(path,raw):
    path=Path(path)
    if path.is_symlink():raise BackupError('Ссылка вместо файла назначения недопустима')
    fd,tmp=tempfile.mkstemp(prefix='.pushok-backup-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            os.fchmod(f.fileno(),0o600);f.write(raw);f.flush();os.fsync(f.fileno())
        # Hard-link publishes atomically and fails if any destination already exists.
        os.link(tmp,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:os.unlink(tmp)


def snapshot(database,dest):
    source=Path(database)
    if not source.is_file():raise BackupError('Исходная база не найдена')
    deadline=time.monotonic()+30
    def progress(status,remaining,total):
        if time.monotonic()>deadline:raise BackupError('Снимок не завершён за 30 секунд; повторите при меньшей нагрузке')
        if total*page_size>MAX_DB:raise BackupError('База превышает лимит копии 128 MiB')
    with connect(source) as conn:
        page_size=conn.execute('PRAGMA page_size').fetchone()[0]
        if conn.execute('PRAGMA page_count').fetchone()[0]*page_size>MAX_DB:raise BackupError('База превышает лимит копии 128 MiB')
        with closing(sqlite3.connect(dest)) as target:conn.backup(target,pages=256,progress=progress,sleep=0.05)
    if Path(dest).stat().st_size>MAX_DB:raise BackupError('База превышает лимит копии 128 MiB')


def pack(database,env):
    files={'shop.db':Path(database).read_bytes(),'.env':env}
    manifest={'format':1,'release':VERSION,'created':datetime.now(timezone.utc).isoformat(),
        'files':{k:{'size':len(v),'sha256':hashlib.sha256(v).hexdigest()} for k,v in files.items()}}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
        for name,raw in files.items():archive.writestr(name,raw)
        archive.writestr('manifest.json',json.dumps(manifest).encode())
    return output.getvalue()


def unpack(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos=archive.infolist()
            if len(infos)!=3 or {i.filename for i in infos}!={'shop.db','.env','manifest.json'}:raise BackupError('Неизвестные файлы внутри копии')
            limits={'shop.db':MAX_DB,'.env':MAX_ENV,'manifest.json':16384}
            for i in infos:
                if i.file_size>limits[i.filename] or i.compress_type!=zipfile.ZIP_STORED:raise BackupError('Недопустимый размер или формат файла в копии')
            files={i.filename:archive.read(i) for i in infos}
        manifest=json.loads(files.pop('manifest.json'))
        if not isinstance(manifest['created'],str) or len(manifest['created'])>64:raise BackupError('Некорректное время создания копии')
        datetime.fromisoformat(manifest['created'])
        if type(manifest['format']) is not int or manifest['format']!=1 or manifest['release']!=VERSION:raise BackupError('Восстанавливайте копию инструментом того же релиза')
        if set(manifest['files'])!=set(files):raise BackupError('Некорректный манифест копии')
        for name,value in files.items():
            record=manifest['files'][name]
            if record['size']!=len(value) or record['sha256']!=hashlib.sha256(value).hexdigest():raise BackupError('Контрольная сумма файла не совпала')
        return files,manifest
    except BackupError:raise
    except (ValueError,TypeError,KeyError,zipfile.BadZipFile,RuntimeError,NotImplementedError):raise BackupError('Повреждён внутренний формат копии') from None


def create(database,env,output,password):
    values,_=environment(env)
    runtime=os.getenv('ENCRYPTION_KEY')
    if runtime and runtime!=values['ENCRYPTION_KEY']:raise BackupError('Ключ текущего процесса отличается от .env')
    salt=secrets.token_bytes(16);cipher=Fernet(password_key(password,salt))
    with tempfile.TemporaryDirectory(prefix='pushok-snapshot-') as temp:
        dbpath=Path(temp)/'shop.db';snapshot(database,dbpath);report=validate(dbpath,env)
        encrypted=MAGIC+salt+cipher.encrypt(pack(dbpath,env))
    if len(encrypted)>MAX_ARCHIVE:raise BackupError('Копия превышает лимит размера')
    publish(output,encrypted)
    return report


def read_archive(path,password):
    with open(path,'rb') as stream:raw=stream.read(MAX_ARCHIVE+1)
    if len(raw)>MAX_ARCHIVE or len(raw)<25 or raw[:8]!=MAGIC:raise BackupError('Недопустимый размер или формат копии')
    try:plain=Fernet(password_key(password,raw[8:24])).decrypt(raw[24:])
    except InvalidToken:raise BackupError('Неверный пароль или копия повреждена') from None
    return unpack(plain)


def verify(path,password):
    files,manifest=read_archive(path,password)
    with tempfile.TemporaryDirectory(prefix='pushok-verify-') as temp:
        database=Path(temp)/'shop.db';database.write_bytes(files['shop.db']);report=validate(database,files['.env'])
    return {**report,'release':manifest['release'],'created':manifest['created']}


def restore(path,password,target):
    files,manifest=read_archive(path,password)
    with tempfile.TemporaryDirectory(prefix='pushok-restore-') as temp:
        database=Path(temp)/'shop.db';database.write_bytes(files['shop.db']);report=validate(database,files['.env'])
        with closing(sqlite3.connect(database)) as conn:
            sessions=conn.execute('DELETE FROM sessions').rowcount;tickets=conn.execute('DELETE FROM tickets').rowcount;conn.commit()
        # No extraction, replacement, container startup or mutation of the source installation.
        target=Path(target);target.mkdir(mode=0o700)
        try:
            publish(target/'shop.db',database.read_bytes());publish(target/'.env',files['.env'])
            publish(target/'restore.json',json.dumps({'release':manifest['release'],'snapshot_created':manifest['created'],
                'sessions_cleared':sessions,'tickets_cleared':tickets}).encode())
        except BaseException:
            for name in ('shop.db','.env','restore.json'):
                try:(target/name).unlink()
                except FileNotFoundError:pass
            target.rmdir();raise
    return {**report,'release':manifest['release'],'sessions_cleared':sessions,'tickets_cleared':tickets}


def read_password(args):
    if args.password_file:
        path=Path(args.password_file)
        if path.is_symlink():raise BackupError('Файл пароля не должен быть ссылкой')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            mode=os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_mode&0o077:raise BackupError('Файл пароля должен быть обычным файлом с правами 0600')
            raw=stream.read(4097)
        if len(raw)>4096:raise BackupError('Слишком длинный пароль')
        password=raw.decode().rstrip('\r\n')
    elif 'PUSHOK_BACKUP_PASSWORD' in os.environ:password=os.environ.pop('PUSHOK_BACKUP_PASSWORD')
    else:
        if not sys.stdin.isatty():raise BackupError('Для запуска без терминала задайте файл пароля или PUSHOK_BACKUP_PASSWORD')
        password=getpass.getpass('Пароль резервной копии (минимум 16 символов): ')
        if args.command=='create' and password!=getpass.getpass('Повторите пароль: '):raise BackupError('Пароли не совпали')
    if len(password)<16:raise BackupError('Пароль копии должен содержать не менее 16 символов')
    return password


def main(argv=None):
    parser=argparse.ArgumentParser(description='Зашифрованная копия магазина; восстановление только в новый каталог')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('create','verify','restore'):
        p=sub.add_parser(command);p.add_argument('--password-file')
        if command=='create':
            p.add_argument('--database',default=str(Path(os.getenv('DATA_DIR','data'))/'shop.db'))
            env=p.add_mutually_exclusive_group();env.add_argument('--env',default='.env');env.add_argument('--env-stdin',action='store_true')
            p.add_argument('--output',required=True)
        else:
            p.add_argument('archive')
            if command=='restore':p.add_argument('--target',required=True)
    args=parser.parse_args(argv);password=read_password(args)
    if args.command=='create':
        if args.env_stdin:raw=sys.stdin.buffer.read(MAX_ENV+1)
        else:
            with open(args.env,'rb') as stream:raw=stream.read(MAX_ENV+1)
        result=create(args.database,raw,args.output,password)
    elif args.command=='verify':result=verify(args.archive,password)
    else:result=restore(args.archive,password,args.target)
    print(json.dumps({'ok':True,'operation':args.command,**result},ensure_ascii=False))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except BackupError as exc:
        print('Операция остановлена: '+str(exc),file=sys.stderr);raise SystemExit(1)
    except (OSError,sqlite3.Error,UnicodeError,ValueError,EOFError):
        print('Операция остановлена: проверьте входные файлы, права и формат базы; секреты не выводятся.',file=sys.stderr);raise SystemExit(1)
