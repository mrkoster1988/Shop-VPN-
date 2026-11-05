import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
import zipfile
import pytest
from cryptography.fernet import Fernet
from app import config as c
from app.db import db,execute,one,init_db
from test_shop import client

spec=importlib.util.spec_from_file_location('backup',Path(__file__).parents[1]/'scripts/backup.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
PASSWORD='a-long-separate-backup-passphrase'

@pytest.fixture
def source(client,tmp_path,monkeypatch):
    uid=execute('INSERT INTO users(email,created) VALUES (?,0)',('private@example.com',))
    sid=str(uuid.uuid4())
    execute("INSERT INTO subscriptions(id,user_id,protocol,expires,state,secret,credential_id,server_id) VALUES (?,?,'xui',2000000000,'active',?,?,'test-xui')",(sid,uid,c.encrypt('vless://sensitive-key'),c.encrypt(sid)))
    execute('INSERT INTO sessions VALUES (?,?,2000000000)',('old-session-hash',uid))
    execute('INSERT INTO tickets VALUES (?,?,2000000000)',('old-ticket-hash',uid))
    env=('PUBLIC_URL=https://pushok.shop/shop\nENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\nADMIN_TOKEN=private-admin-token\nBOT_TOKEN=private-bot-token\n').encode()
    return c.DB,env,tmp_path/'copy.pvb',sid


def create(source):
    database,env,output,sid=source;b.create(database,env,output,PASSWORD);return output


def test_round_trip_encrypts_env_checks_key_and_resets_logins(source,tmp_path,monkeypatch):
    database,env,output,sid=source;report=b.create(database,env,output,PASSWORD)
    assert report['encrypted_values']>=4
    raw=output.read_bytes()
    for secret in (b'private@example.com',b'private-admin-token',b'private-bot-token',b'vless://',os.environ['ENCRYPTION_KEY'].encode()):assert secret not in raw
    verified=b.verify(output,PASSWORD);assert verified['release']==b.VERSION
    target=tmp_path/'restored';restored=b.restore(output,PASSWORD,target)
    assert restored['sessions_cleared']==restored['tickets_cleared']==1
    assert (target/'.env').read_bytes()==env
    assert target.stat().st_mode&0o777==0o700
    for file in target.iterdir():assert file.stat().st_mode&0o777==0o600
    assert output.stat().st_mode&0o777==0o600
    with sqlite3.connect(target/'shop.db') as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM tickets').fetchone()[0]==0
        assert c.decrypt(conn.execute('SELECT secret FROM subscriptions WHERE id=?',(sid,)).fetchone()[0])=='vless://sensitive-key'
    assert one('SELECT count(*) AS n FROM sessions')['n']==1  # original installation untouched
    monkeypatch.setattr(c,'DB',target/'shop.db');init_db();init_db()
    assert one('SELECT count(*) AS n FROM subscriptions')['n']==1


def test_sqlite_backup_includes_uncheckpointed_wal(source):
    database,env,output,sid=source
    conn=sqlite3.connect(database);conn.execute('PRAGMA journal_mode=WAL');conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute('INSERT INTO users(email,created) VALUES (?,0)',('committed-in-wal@example.com',));conn.commit()
    assert Path(str(database)+'-wal').stat().st_size>0
    create(source);files,_=b.read_archive(output,PASSWORD)
    check=output.parent/'check.db';check.write_bytes(files['shop.db'])
    with sqlite3.connect(check) as copy:assert copy.execute("SELECT count(*) FROM users WHERE email='committed-in-wal@example.com'").fetchone()[0]==1
    conn.close()


@pytest.mark.parametrize('corruption',['wrong_password','modified','truncated','unknown_header'])
def test_corrupt_or_wrong_password_does_not_create_target(source,tmp_path,corruption):
    output=create(source);password=PASSWORD
    if corruption=='wrong_password':password='another-long-backup-password'
    else:
        raw=bytearray(output.read_bytes())
        if corruption=='modified':raw[-8]^=1
        elif corruption=='truncated':raw=raw[:30]
        else:raw[:8]=b'UNKNOWN!'
        output.write_bytes(raw)
    target=tmp_path/'must-not-exist'
    with pytest.raises(b.BackupError):b.restore(output,password,target)
    assert not target.exists()


def test_wrong_env_key_and_runtime_mismatch_rejected(source,monkeypatch):
    database,env,output,sid=source;wrong=env.replace(os.environ['ENCRYPTION_KEY'].encode(),Fernet.generate_key())
    with pytest.raises(b.BackupError,match='процесса'):b.create(database,wrong,output,PASSWORD)
    monkeypatch.delenv('ENCRYPTION_KEY')
    with pytest.raises(b.BackupError,match='расшифровывает'):b.create(database,wrong,output,PASSWORD)
    assert not output.exists()


@pytest.mark.parametrize('table,column',[(t,col) for t,cols in b.ENCRYPTED.items() for col in cols])
def test_checks_every_encrypted_field(source,table,column):
    database,env,output,sid=source
    # A populated representative row of each encrypted table.
    device='device-id';job=str(uuid.uuid4())
    if table=='devices':
        execute("INSERT INTO devices(id,subscription_id,request_id,name,created,secret,credential_id) VALUES (?,?,?,'Phone',0,?,?)",(device,sid,job,c.encrypt('old'),c.encrypt(device)))
    elif table=='subscription_links':execute('INSERT INTO subscription_links(subscription_id,token_secret,created) VALUES (?,?,0)',(sid,c.encrypt('token')))
    elif table=='primary_rotations':execute('INSERT INTO primary_rotations(id,subscription_id,request_id,old_credential_id,new_credential_id,created) VALUES (?,?,?,?,?,0)',(job,sid,job,c.encrypt('old'),c.encrypt('new')))
    elif table=='device_rotations':
        execute("INSERT INTO devices(id,subscription_id,request_id,name,created) VALUES (?,?,?,'Phone',0)",(device,sid,job))
        execute('INSERT INTO device_rotations(id,device_id,request_id,old_credential_id,new_credential_id,created) VALUES (?,?,?,?,?,0)',(job,device,job,c.encrypt('old'),c.encrypt('new')))
    execute('UPDATE '+table+' SET '+column+'=?',('broken-encrypted-data',))
    with pytest.raises(b.BackupError,match='расшифровывает'):create(source)
    assert not output.exists()


def test_no_overwrite_of_archive_or_existing_directory(source,tmp_path):
    output=create(source);before=output.read_bytes()
    with pytest.raises(FileExistsError):create(source)
    assert output.read_bytes()==before
    target=tmp_path/'existing';target.mkdir();(target/'keep').write_text('do not change')
    with pytest.raises(FileExistsError):b.restore(output,PASSWORD,target)
    assert [p.name for p in target.iterdir()]==['keep']


def test_symlinks_are_not_overwritten(source,tmp_path):
    database,env,output,sid=source;original=tmp_path/'original';original.write_text('keep');output.symlink_to(original)
    with pytest.raises(b.BackupError):create(source)
    assert original.read_text()=='keep'
    output.unlink();create(source);target=tmp_path/'target';target.symlink_to(tmp_path/'absent',target_is_directory=True)
    with pytest.raises(FileExistsError):b.restore(output,PASSWORD,target)
    assert not (tmp_path/'absent').exists()


def test_missing_database_does_not_create_empty_file(source,tmp_path):
    database,env,output,sid=source;missing=tmp_path/'absent.db'
    with pytest.raises(b.BackupError):b.create(missing,env,output,PASSWORD)
    assert not missing.exists() and not output.exists()


def test_broken_foreign_keys_or_schema_rejected(source):
    database,env,output,sid=source
    with sqlite3.connect(database) as conn:conn.execute('UPDATE subscriptions SET user_id=9999')
    with pytest.raises(b.BackupError,match='связи'):create(source)
    with sqlite3.connect(database) as conn:conn.execute('DROP TABLE subscriptions')
    with pytest.raises(b.BackupError):create(source)


def test_wrong_internal_hash_version_or_path(source):
    database,env,output,sid=source
    with __import__('tempfile').TemporaryDirectory() as temp:
        copy=Path(temp)/'shop.db';b.snapshot(database,copy);raw=b.pack(copy,env)
    def archive(change):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:files={i.filename:z.read(i) for i in z.infolist()}
        change(files);buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            for name,value in files.items():z.writestr(name,value)
        return buf.getvalue()
    def manifest_change(files,field,value):
        m=json.loads(files['manifest.json']);m[field]=value;files['manifest.json']=json.dumps(m).encode()
    for bad in (archive(lambda f:f.update({'.env':env+b'#changed'})),archive(lambda f:manifest_change(f,'release','v0.000')),archive(lambda f:f.update({'../outside':b'bad'}))):
        with pytest.raises(b.BackupError):b.unpack(bad)


@pytest.mark.parametrize('raw',[b'',b'ENCRYPTION_KEY=invalid\n',b'ENCRYPTION_KEY="unterminated\n'])
def test_invalid_env_is_rejected(raw):
    with pytest.raises(b.BackupError):b.environment(raw)


def test_duplicate_env_rejected(source):
    with pytest.raises(b.BackupError):b.environment(source[1]+source[1])


def test_password_file_permissions_and_no_cli_secret_output(source,tmp_path):
    password=tmp_path/'password';password.write_text(PASSWORD);password.chmod(0o644)
    args=type('Args',(),{'password_file':str(password),'command':'create'})()
    with pytest.raises(b.BackupError):b.read_password(args)
    password.chmod(0o600);assert b.read_password(args)==PASSWORD
    database,env,output,sid=source;envfile=tmp_path/'env';envfile.write_bytes(env)
    command=[sys.executable,str(Path(b.__file__)),'create','--database',str(database),'--env',str(envfile),'--output',str(output),'--password-file',str(password)]
    result=subprocess.run(command,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['ok']
    assert PASSWORD not in result.stdout+result.stderr and 'private-admin-token' not in result.stdout+result.stderr
    invalid=subprocess.run(command,text=True,capture_output=True)
    assert invalid.returncode==1 and PASSWORD not in invalid.stderr and 'Traceback' not in invalid.stderr


def test_database_size_limit_and_short_password(source,monkeypatch):
    database,env,output,sid=source
    with pytest.raises(b.BackupError):b.create(database,env,output,'short')
    monkeypatch.setattr(b,'MAX_DB',1)
    with pytest.raises(b.BackupError,match='128 MiB'):create(source)
    assert not output.exists()


def test_restore_failure_removes_only_new_staging_directory(source,tmp_path,monkeypatch):
    output=create(source);target=tmp_path/'staging';real_publish=b.publish
    def failing(path,raw):
        if Path(path).name=='.env':raise OSError('disk full')
        return real_publish(path,raw)
    monkeypatch.setattr(b,'publish',failing)
    with pytest.raises(OSError):b.restore(output,PASSWORD,target)
    assert not target.exists() and output.exists()
    assert one('SELECT count(*) AS n FROM sessions')['n']==1


def test_no_terminal_never_reads_password_from_env_stdin(source):
    env=os.environ.copy();env.pop('PUSHOK_BACKUP_PASSWORD',None)
    result=subprocess.run([sys.executable,b.__file__,'create','--env-stdin','--output',str(source[2])],input=source[1],capture_output=True,env=env,timeout=10)
    assert result.returncode==1 and not source[2].exists()
    assert b'private-admin-token' not in result.stderr and b'private-admin-token' not in result.stdout


def test_password_env_removed_and_runtime_backup_uses_saved_env(source,monkeypatch):
    monkeypatch.setenv('PUSHOK_BACKUP_PASSWORD',PASSWORD)
    args=type('Args',(),{'password_file':None,'command':'create'})()
    assert b.read_password(args)==PASSWORD and 'PUSHOK_BACKUP_PASSWORD' not in os.environ
    create(source)
    assert b.verify(source[2],PASSWORD)['encrypted_values']>=4
