import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import pytest
from test_shop import client

spec=importlib.util.spec_from_file_location('upgrader',Path(__file__).parents[1]/'scripts/upgrade.py')
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)

@pytest.fixture
def installation(tmp_path,monkeypatch):
    root=tmp_path/'installed';root.mkdir();source=tmp_path/'release';(source/'scripts').mkdir(parents=True);(source/'app').mkdir()
    (source/'scripts/install.py').write_text("VERSION="+repr(u.VERSION)+"\n")
    (source/'scripts/backup.py').write_text('# backup');(source/'app/main.py').write_text('# app');(source/'Dockerfile').write_text('FROM test')
    settings={'version':'v0.021','url':'https://example.com/shop','port':8000,'mode':'caddy','project':'pushok-1234567890','support':'test','nginx_config':None}
    u.write(root/'installer.json',settings);(root/'.env').write_text('ENCRYPTION_KEY=private-key\nBOT_TOKEN=private-bot\n')
    (root/'compose.install.json').write_text(u.i.compose(settings,root));(root/'source').mkdir();(root/'source/sentinel').write_text('old source')
    calls=[];health=[];fail={'predicate':None}
    def run(args,**kwargs):
        calls.append((args,kwargs))
        if fail['predicate'] and fail['predicate'](args):
            fail['predicate']=None;raise subprocess.CalledProcessError(1,args,output=b'private-provider-response')
        if 'ps' in args:return 'c'*64
        if u.CHECK_KEY in args:return 'key_matches'
        if '{{.Image}}' in args:return 'sha256:'+'d'*64
        if '{{json .Mounts}}' in args:return json.dumps([{'Destination':'/app/data','Type':'volume','Name':json.loads((root/'installer.json').read_text()).get('managed_upgrade',{}).get('volume','pushok-1234567890_shop_data'),'RW':True}])
        if args[:2]==['docker','run'] and '-d' in args:return 'e'*64
        if args[:2]==['docker','cp'] and args[2].endswith(':/tmp/shop.pvb'):Path(args[3]).write_bytes(b'encrypted-snapshot')
        return ''
    monkeypatch.setattr(u,'receive_archive',lambda args,path:(run(args),Path(path).write_bytes(b'encrypted-snapshot'),Path(path).chmod(0o600)));monkeypatch.setattr(u.i,'check_os',lambda:'noble');monkeypatch.setattr(u,'run',run);monkeypatch.setattr(u.i,'wait_health',lambda url,*a:health.append(url));monkeypatch.setattr(u.os,'geteuid',lambda:0)
    return root,source,calls,health,fail


def ready(installation):
    root,source,*_=installation;assert u.prepare(root,source,'long-backup-password')['phase']=='ready'
    return u.state(root)


def test_plan_is_read_only_and_secret_free(installation,capsys):
    root,source,calls,health,fail=installation;before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert u.main(['plan','--dir',str(root),'--source',str(source)])==0
    assert json.loads(capsys.readouterr().out)['plan_only']
    assert before=={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()} and not calls and not health
    assert not (root/'upgrade.json').exists() and not (root/'.install.lock').exists()


def test_prepare_uses_readonly_old_volume_and_offline_new_data(installation):
    root,source,calls,health,fail=installation
    (source/'.env').write_text('private source secret');(source/'backups').mkdir();(source/'backups/copy.pvb').write_text('private')
    before=(root/'.env').read_bytes();journal=ready(installation);work=root/'upgrades'/journal['operation']
    candidate=json.loads((root/'compose.install.json').read_text());shop=candidate['services']['shop']
    assert shop['environment']['MAINTENANCE_MODE']=='true' and shop['environment']['BACKGROUND_TASKS']=='false'
    assert shop['environment']['DATA_DIR']=='/app/data/restored' and 'build' not in shop
    assert candidate['services']['https']['image']=='caddy:2'
    assert candidate['volumes']['upgrade_data']['external'] is True
    assert (root/'.env').read_bytes()==before and (root/'source/sentinel').read_text()=='old source'
    assert not (work/'source/.env').exists() and not (work/'source/backups').exists()
    assert (work/'backup.pvb').stat().st_mode&0o777==0o600
    assert 'private-key' not in (root/'upgrade.json').read_text() and 'long-backup-password' not in (root/'upgrade.json').read_text()
    commands=[args for args,kwargs in calls]
    helper=next(a for a in commands if a[:2]==['docker','run'] and '-d' in a)
    assert helper[helper.index('--volumes-from')+1]=='c'*64+':ro'
    assert helper[helper.index('--network')+1]=='none'
    migrate=next(a for a in commands if a[:2]==['docker','run'] and a[-2]=='-c' and 'init_db' in a[-1])
    assert migrate[migrate.index('--network')+1]=='none'
    assert commands.index(next(a for a in commands if 'stop' in a))<commands.index(next(a for a in commands if 'create' in a and 'scripts/backup.py' in a))
    assert all('volume' not in a or 'rm' not in a for a in commands)
    assert health==['http://127.0.0.1:8000/shop/health','https://example.com/shop/health']
    assert json.loads((root/'installer.json').read_text())['version']==u.VERSION


def test_activate_enables_original_policy_then_forbids_database_rollback(installation):
    root,source,calls,health,fail=installation;s=ready(installation)
    assert u.activate(root,s)['phase']=='committed'
    environment=json.loads((root/'compose.install.json').read_text())['services']['shop']['environment']
    assert 'MAINTENANCE_MODE' not in environment and 'BACKGROUND_TASKS' not in environment
    count=len(calls);assert u.activate(root,u.state(root))['phase']=='committed' and len(calls)==count
    with pytest.raises(ValueError,match='сверка'):u.rollback(root,u.state(root))


def test_activate_preserves_explicit_background_setting(installation):
    root,source,*_=installation
    config=json.loads((root/'compose.install.json').read_text());config['services']['shop']['environment']['BACKGROUND_TASKS']='false';u.write(root/'compose.install.json',config)
    s=ready(installation);u.activate(root,s)
    assert json.loads((root/'compose.install.json').read_text())['services']['shop']['environment']['BACKGROUND_TASKS']=='false'


def test_pre_activation_rollback_keeps_original_volume_and_pins_old_image(installation):
    root,source,calls,health,fail=installation;s=ready(installation)
    assert u.rollback(root,s)['phase']=='rolled_back'
    old=json.loads((root/'compose.install.json').read_text());assert old['services']['shop']['volumes']==['shop_data:/app/data']
    assert old['services']['shop']['image']=='sha256:'+'d'*64
    assert json.loads((root/'installer.json').read_text())['version']=='v0.021'
    assert (root/'source/sentinel').read_text()=='old source'
    count=len(calls);u.rollback(root,u.state(root));assert len(calls)==count


@pytest.mark.parametrize('stage',['build','stop','backup','archive_stream','verify','restore','migrate','start'])
def test_prepare_failure_restores_original_and_retains_journal(installation,stage):
    root,source,calls,health,fail=installation
    predicates={
        'build':lambda a:a[:2]==['docker','build'],
        'stop':lambda a:'stop' in a,
        'backup':lambda a:'scripts/backup.py' in a and 'create' in a,
        'archive_stream':lambda a:'-c' in a and a[-1]=='/tmp/shop.pvb',
        'verify':lambda a:'scripts/backup.py' in a and 'verify' in a,
        'restore':lambda a:'scripts/backup.py' in a and 'restore' in a,
        'migrate':lambda a:a[:2]==['docker','run'] and 'init_db' in a[-1],
        'start':lambda a:'up' in a}
    fail['predicate']=predicates[stage]
    with pytest.raises(subprocess.CalledProcessError):u.prepare(root,source,'long-backup-password')
    s=u.state(root);assert s['phase']=='rolled_back' and not s['activation_started'] and s['failure_stage']
    assert json.loads((root/'installer.json').read_text())['version']=='v0.021'
    assert json.loads((root/'compose.install.json').read_text())['services']['shop']['volumes']==['shop_data:/app/data']
    assert (root/'.env').read_text()=='ENCRYPTION_KEY=private-key\nBOT_TOKEN=private-bot\n'


def test_activation_failure_never_automatically_restores_old_data(installation):
    root,source,calls,health,fail=installation;s=ready(installation)
    fail['predicate']=lambda a:'up' in a
    with pytest.raises(subprocess.CalledProcessError):u.activate(root,s)
    s=u.state(root);assert s['phase']=='activation_failed' and s['activation_started']
    with pytest.raises(ValueError):u.rollback(root,s)
    assert json.loads((root/'installer.json').read_text())['version']==u.VERSION


@pytest.mark.parametrize('file',['.env','compose.install.json','installer.json'])
def test_operator_edits_block_activate_and_rollback(installation,file):
    root,source,*_=installation;s=ready(installation);(root/file).write_bytes((root/file).read_bytes()+b' ')
    with pytest.raises(ValueError):u.activate(root,s)
    with pytest.raises(ValueError):u.rollback(root,s)


def test_rollback_failure_requires_explicit_recovery(installation,monkeypatch):
    root,source,calls,health,fail=installation
    monkeypatch.setattr(u.i,'wait_health',lambda *a:(_ for _ in ()).throw(RuntimeError('unavailable')))
    with pytest.raises(RuntimeError):u.prepare(root,source,'long-backup-password')
    s=u.state(root);assert s['phase']=='recovery_required' and not s['activation_started']
    monkeypatch.setattr(u.i,'wait_health',lambda *a:None)
    assert u.rollback(root,s)['phase']=='rolled_back'


@pytest.mark.parametrize('change',['old_version','source_version','data_dir','volume','symlink'])
def test_unsupported_installations_fail_before_any_docker_call(installation,change):
    root,source,calls,*_=installation
    if change=='old_version':
        data=json.loads((root/'installer.json').read_text());data['version']='v0.020';u.write(root/'installer.json',data)
    elif change=='source_version':(source/'scripts/install.py').write_text("VERSION='v0.999'")
    elif change=='symlink':(source/'app/secret').symlink_to(root/'.env')
    else:
        data=json.loads((root/'compose.install.json').read_text())
        if change=='data_dir':data['services']['shop']['environment']['DATA_DIR']='/other'
        else:data['services']['shop']['volumes']=['unrelated:/app/data']
        u.write(root/'compose.install.json',data)
    with pytest.raises(ValueError):u.plan(root,source)
    assert not calls and not (root/'upgrade.json').exists()


def test_lock_serializes_installer_and_upgrader(installation):
    root,*_=installation
    with u.locked(root):
        with pytest.raises(ValueError):
            with u.locked(root):pass


def test_password_not_in_docker_arguments_or_status(installation,monkeypatch,capsys):
    root,source,calls,*_=installation;monkeypatch.setenv('PUSHOK_BACKUP_PASSWORD','private-long-backup-password')
    assert u.main(['prepare','--dir',str(root),'--source',str(source)])==0
    assert 'PUSHOK_BACKUP_PASSWORD' not in os.environ
    assert 'private-long-backup-password' not in capsys.readouterr().out
    assert all('private-long-backup-password' not in ' '.join(args) for args,kwargs in calls)
    assert u.main(['status','--dir',str(root)])==0
    assert 'private-key' not in capsys.readouterr().out


def test_installer_cannot_rewrite_a_managed_upgrade(installation):
    root,source,*_=installation;ready(installation)
    with pytest.raises(ValueError,match='upgrade.py'):u.i.main(['--dir',str(root),'--plan'])


def test_maintenance_blocks_purchases_auth_links_and_static(client,monkeypatch):
    monkeypatch.setenv('MAINTENANCE_MODE','true')
    for path in ('/','/admin','/static/app.js','/api/catalog','/api/me','/login?ticket=secret','/sub/private'):
        r=client.get(path);assert r.status_code==503 and r.headers['cache-control']=='no-store' and r.headers['retry-after']=='60'
    assert client.post('/api/orders',json={}).status_code==503
    assert client.post('/api/auth/register',json={}).status_code==503
    assert client.get('/health').json()=={'status':'ok'}


def test_maintenance_suppresses_both_background_tasks_at_startup(client,monkeypatch):
    from app.main import lifespan,shop_app
    from app import service,config as c
    monkeypatch.setenv('MAINTENANCE_MODE','true');monkeypatch.setenv('BACKGROUND_TASKS','true');monkeypatch.setattr(c,'BOT_TOKEN','configured')
    def forbidden():raise AssertionError('Background effects in maintenance')
    monkeypatch.setattr(service,'worker',forbidden);monkeypatch.setattr(service,'bot_loop',forbidden)
    async def start():
        async with lifespan(shop_app):pass
    asyncio.run(start())


def test_maintenance_under_shop_prefix(client,monkeypatch):
    from app.main import create_app
    from app import config as c
    from fastapi.testclient import TestClient
    monkeypatch.setenv('MAINTENANCE_MODE','true');monkeypatch.setattr(c,'BASE_PATH','/shop')
    with TestClient(create_app('/shop')) as page:
        assert page.get('/shop/health').status_code==200
        assert page.get('/shop/sub/private').status_code==503
        assert page.post('/shop/api/orders',json={}).status_code==503


def test_saved_metadata_tampering_blocks_activation(installation):
    root,source,calls,*_=installation;s=ready(installation);work=root/'upgrades'/s['operation']
    (work/'compose.before.json').write_bytes((work/'compose.before.json').read_bytes()+b' ')
    count=len(calls)
    with pytest.raises(ValueError):u.activate(root,s)
    with pytest.raises(ValueError):u.rollback(root,s)
    assert len(calls)==count


def test_custom_entrypoint_is_rejected_before_any_change(installation):
    root,source,calls,*_=installation
    config=json.loads((root/'compose.install.json').read_text());config['services']['shop']['entrypoint']=['custom'];u.write(root/'compose.install.json',config)
    with pytest.raises(ValueError):u.prepare(root,source,'long-backup-password')
    assert not calls and not (root/'upgrade.json').exists()


def test_stopped_staging_keeps_wal_and_never_changes_original(client,tmp_path):
    import sys,sqlite3
    from app import config as c
    from test_backup import b
    source=tmp_path/'old-volume';source.mkdir();database=source/'shop.db';b.snapshot(c.DB,database)
    conn=sqlite3.connect(database);conn.execute('PRAGMA journal_mode=WAL');conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute("INSERT INTO users(email,created) VALUES ('staged-wal@example.com',0)");conn.commit()
    before={p.name:p.read_bytes() for p in (database,Path(str(database)+'-wal'))}
    target=tmp_path/'staging'
    subprocess.run([sys.executable,'-c',u.STAGE_SQLITE,str(source),str(target)],check=True)
    env=('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode();archive=tmp_path/'staged.pvb'
    b.create(target/'shop.db',env,archive,'long-backup-password')
    files,manifest=b.read_archive(archive,'long-backup-password');check=tmp_path/'check.db';check.write_bytes(files['shop.db'])
    with sqlite3.connect(check) as db:assert db.execute("SELECT count(*) FROM users WHERE email='staged-wal@example.com'").fetchone()[0]==1
    assert before=={p.name:p.read_bytes() for p in (database,Path(str(database)+'-wal'))}
    conn.close()


def test_backup_tool_accepts_compatible_previous_release(client,tmp_path):
    import io,zipfile
    from app import config as c
    from test_backup import b
    env=('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode();copy=tmp_path/'copy.db';b.snapshot(c.DB,copy)
    raw=b.pack(copy,env)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:files={name:archive.read(name) for name in archive.namelist()}
    manifest=json.loads(files['manifest.json']);manifest['release']='v0.021';files['manifest.json']=json.dumps(manifest).encode();output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        for name,value in files.items():archive.writestr(name,value)
    _,read=b.unpack(output.getvalue());assert read['release']=='v0.021'


def legacy_installation(installation,monkeypatch,*,committed=True):
    root,source,calls,health,fail=installation
    # Produce the metadata/layout of the previously published 0.021 -> 0.022 transition.
    with monkeypatch.context() as context:
        context.setattr(u,'VERSION','v0.022')
        (source/'scripts/install.py').write_text("VERSION='v0.022'\n")
        legacy=ready(installation)
        if committed:u.activate(root,legacy)
    operation=json.loads((root/'installer.json').read_text())['managed_upgrade']['operation']
    saved=root/'upgrades'/operation/'activation.json'
    if saved.exists():saved.unlink()  # v0.022 did not create this additional proof file.
    (source/'scripts/install.py').write_text('VERSION='+repr(u.VERSION)+'\n')
    calls.clear();health.clear()
    return operation


@pytest.mark.parametrize('version',['v0.021','v0.022','v0.023','v0.024','v0.025'])
def test_supported_fresh_origins_choose_original_volume(installation,version):
    root,source,calls,health,fail=installation
    marker=json.loads((root/'installer.json').read_text());marker['version']=version;u.write(root/'installer.json',marker)
    assert u.plan(root,source)['previous']==version
    result=u.prepare(root,source,'long-backup-password');assert result['previous']==version and result['target']==u.VERSION
    command=next(a for a,k in calls if u.STAGE_SQLITE in a)
    assert command[-2:]==['/app/data','/tmp/source']
    assert u.state(root)['old_volume']=='pushok-1234567890_shop_data'


def test_managed_upgrade_uses_current_volume_and_preserves_legacy_history(installation,monkeypatch):
    root,source,calls,health,fail=installation;old_operation=legacy_installation(installation,monkeypatch)
    old_marker=(root/'installer.json').read_bytes();old_compose=(root/'compose.install.json').read_bytes();old_journal=(root/'upgrade.json').read_bytes()
    assert u.plan(root,source)['previous']=='v0.022'
    assert not (root/'upgrades'/old_operation/'activation.json').exists()  # plan remains read-only
    result=u.prepare(root,source,'long-backup-password');assert result['phase']=='ready'
    current=u.state(root);work=root/'upgrades'/current['operation']
    assert current['old_volume']=='pushok-1234567890-upgrade-'+old_operation
    assert (work/'installer.before.json').read_bytes()==old_marker and (work/'compose.before.json').read_bytes()==old_compose
    assert json.loads((work/'journal.before.json').read_text())==json.loads(old_journal)
    assert json.loads((root/'upgrades'/old_operation/'activation.json').read_text())['phase']=='committed'
    stage=next(a for a,k in calls if u.STAGE_SQLITE in a);assert stage[-2:]==['/app/data/restored','/tmp/source']
    assert current['new_volume']!=current['old_volume']
    assert json.loads((root/'compose.install.json').read_text())['services']['shop']['environment']['DATA_DIR']=='/app/data/restored'
    assert u.activate(root,current)['phase']=='committed'
    assert (work/'activation.json').is_file()


def test_managed_rollback_can_be_followed_by_another_prepare(installation,monkeypatch):
    root,source,calls,health,fail=installation;operation=legacy_installation(installation,monkeypatch)
    for _ in range(2):
        s=ready(installation);assert s['old_volume']=='pushok-1234567890-upgrade-'+operation
        assert u.rollback(root,s)['phase']=='rolled_back'
        assert json.loads((root/'installer.json').read_text())['managed_upgrade']['operation']==operation
        assert u.plan(root,source)['previous']=='v0.022'
    final=ready(installation);u.activate(root,final)
    assert json.loads((root/'installer.json').read_text())['version']==u.VERSION


def test_failed_build_does_not_lose_proof_of_previous_activation(installation,monkeypatch):
    root,source,calls,health,fail=installation;operation=legacy_installation(installation,monkeypatch)
    fail['predicate']=lambda a:a[:2]==['docker','build']
    with pytest.raises(subprocess.CalledProcessError):u.prepare(root,source,'long-backup-password')
    assert u.state(root)['phase']=='rolled_back'
    assert (root/'upgrades'/operation/'activation.json').exists()
    assert u.plan(root,source)['previous']=='v0.022'
    assert ready(installation)['old_volume'].endswith(operation)


def test_pending_legacy_operation_is_visible_but_not_overwritten(installation,monkeypatch):
    root,source,calls,health,fail=installation;legacy_installation(installation,monkeypatch,committed=False)
    before=(root/'upgrade.json').read_bytes()
    assert u.report(root)['target']=='v0.022' and u.report(root)['phase']=='ready'
    with pytest.raises(ValueError,match='v0.022'):u.plan(root,source)
    with pytest.raises(ValueError,match='v0.022'):u.state(root)
    assert (root/'upgrade.json').read_bytes()==before and not calls


@pytest.mark.parametrize('change',['marker','compose','history','source_missing','volume','proof_conflict'])
def test_managed_drift_fails_before_build_or_stopping_shop(installation,monkeypatch,change):
    root,source,calls,health,fail=installation;operation=legacy_installation(installation,monkeypatch)
    if change=='marker':(root/'installer.json').write_bytes((root/'installer.json').read_bytes()+b' ')
    elif change=='compose':(root/'compose.install.json').write_bytes((root/'compose.install.json').read_bytes()+b' ')
    elif change=='history':(root/'upgrade.json').unlink()
    elif change=='source_missing':(root/'upgrades'/operation/'source').rename(root/'upgrades'/operation/'removed')
    elif change=='volume':
        config=json.loads((root/'compose.install.json').read_text());config['volumes']['upgrade_data']['name']='unrelated';u.write(root/'compose.install.json',config)
    else:
        wrong=json.loads((root/'upgrade.json').read_text());wrong['active_sha']='0'*64;u.write(root/'upgrades'/operation/'activation.json',wrong)
    with pytest.raises((ValueError,OSError)):u.plan(root,source)
    assert not calls


def test_unexpected_runtime_volume_is_rejected_before_changes(installation,monkeypatch):
    root,source,calls,health,fail=installation;legacy_installation(installation,monkeypatch)
    original=u.run
    def run(args,**kwargs):
        if '{{json .Mounts}}' in args:return json.dumps([{'Destination':'/app/data','Type':'volume','Name':'stale-original-volume','RW':True}])
        return original(args,**kwargs)
    monkeypatch.setattr(u,'run',run);before=(root/'upgrade.json').read_bytes()
    with pytest.raises(ValueError):u.prepare(root,source,'long-backup-password')
    assert (root/'upgrade.json').read_bytes()==before
    assert not any('stop' in a or a[:2]==['docker','build'] for a,k in calls)


def test_symlink_history_is_not_followed(installation,tmp_path):
    root,source,calls,health,fail=installation;other=tmp_path/'other';other.mkdir();(root/'upgrades').symlink_to(other,target_is_directory=True)
    with pytest.raises(ValueError):u.plan(root,source)
    assert not calls and not list(other.iterdir())


@pytest.mark.parametrize('stage',['backup','migrate','start'])
def test_managed_prepare_failure_returns_to_current_external_volume(installation,monkeypatch,stage):
    root,source,calls,health,fail=installation;operation=legacy_installation(installation,monkeypatch)
    predicates={
        'backup':lambda a:'scripts/backup.py' in a and 'create' in a,
        'migrate':lambda a:a[:2]==['docker','run'] and 'init_db' in a[-1],
        'start':lambda a:'up' in a}
    fail['predicate']=predicates[stage]
    with pytest.raises(subprocess.CalledProcessError):u.prepare(root,source,'long-backup-password')
    assert u.state(root)['phase']=='rolled_back'
    old=json.loads((root/'compose.install.json').read_text())
    assert old['volumes']['upgrade_data']['name'].endswith(operation)
    assert old['services']['shop']['environment']['DATA_DIR']=='/app/data/restored'
    assert u.plan(root,source)['previous']=='v0.022'


def test_runtime_key_mismatch_refuses_before_build_stop_or_journal(installation):
    root,source,calls,health,fail=installation;fail['predicate']=lambda args:u.CHECK_KEY in args
    before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    with pytest.raises(ValueError,match='ключ'):u.prepare(root,source,'long-backup-password')
    assert before=={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert not (root/'upgrades').exists()
    assert not any('stop' in args or args[:2]==['docker','build'] for args,kwargs in calls)


def test_key_guard_survives_python_optimization_without_printing_keys():
    import sys
    from cryptography.fernet import Fernet
    old=Fernet.generate_key().decode();new=Fernet.generate_key().decode()
    result=subprocess.run([sys.executable,'-O','-c',u.CHECK_KEY],input=('ENCRYPTION_KEY='+new+'\n').encode(),env={**os.environ,'ENCRYPTION_KEY':old},cwd=Path(u.__file__).parents[1],capture_output=True)
    assert result.returncode!=0 and b'key_matches' not in result.stdout
    assert old.encode() not in result.stdout+result.stderr and new.encode() not in result.stdout+result.stderr


def test_binary_archive_stream_preserves_salt_and_never_overwrites(tmp_path):
    import sys
    value=b'PUSHOKB1'+bytes(range(256));target=tmp_path/'backup.pvb'
    args=[sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'PUSHOKB1'+bytes(range(256)))"]
    u.receive_archive(args,target)
    assert target.read_bytes()==value and target.stat().st_mode&0o777==0o600
    with pytest.raises(FileExistsError):u.receive_archive(args,target)
    assert target.read_bytes()==value and not list(tmp_path.glob('.archive-*'))


def test_failed_archive_stream_removes_partial_output(tmp_path):
    import sys
    target=tmp_path/'backup.pvb'
    with pytest.raises(subprocess.CalledProcessError):u.receive_archive([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'partial');sys.exit(1)"],target)
    assert not target.exists() and not list(tmp_path.glob('.archive-*'))
