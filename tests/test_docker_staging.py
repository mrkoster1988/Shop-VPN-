import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

spec=importlib.util.spec_from_file_location('docker_stage',Path(__file__).with_name('docker_staging.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)


def test_disposable_root_guard_refuses_before_docker(tmp_path,monkeypatch):
    monkeypatch.setattr(s.os,'geteuid',lambda:0)
    with pytest.raises(s.StageError):s.main(['--baseline',str(tmp_path),'--intermediate',str(tmp_path),'--source',str(tmp_path),'--report',str(tmp_path/'report')])
    assert not (tmp_path/'report').exists()


def test_cleanup_inventory_only_derives_own_volume_names(tmp_path):
    (tmp_path/'upgrades').mkdir();(tmp_path/'upgrades'/('a'*24)).mkdir();(tmp_path/'upgrades'/'other').mkdir()
    (tmp_path/'upgrades'/('b'*24)).symlink_to(tmp_path,target_is_directory=True)
    assert s.volume_names(tmp_path,'pushok-1234567890')=={'pushok-1234567890_shop_data','pushok-1234567890-upgrade-'+'a'*24}


def test_existing_containers_are_never_cleanup_candidates():
    data={'Id':'old','Config':{'Labels':{'com.docker.compose.project':'our-project'}},'Mounts':[{'Name':'our-volume'}]}
    assert not s.owned_container(data,'our-project',{'our-volume'},{'old'})
    data['Id']='new';assert s.owned_container(data,'our-project',{'our-volume'},{'old'})
    data['Config']['Labels']=None;assert s.owned_container(data,'our-project',{'our-volume'},{'old'})
    data['Mounts']=[{'Name':'unrelated'}];assert not s.owned_container(data,'our-project',{'our-volume'},{'old'})


def test_log_redaction_never_prints_known_fixture_secrets(tmp_path):
    stage=s.Staging(type('Args',(),{})());stage.secrets=['secret-cookie','secret-encryption-key']
    text=stage.scrub('failure: secret-cookie and secret-encryption-key')
    assert 'secret-cookie' not in text and 'secret-encryption-key' not in text


def test_synthetic_fixture_snapshot_survives_real_backup(client,tmp_path):
    import uuid
    from app import config as c
    from test_backup import b
    code=Path(__file__).with_name('staging_data.py').read_text()
    # Empty isolated SQLite file; use the same container-side program against it.
    folder=tmp_path/'fixture';folder.mkdir();key=os.environ['ENCRYPTION_KEY'];env={**os.environ,'DATA_DIR':str(folder),'ENCRYPTION_KEY':key}
    data={k:str(uuid.uuid4()) for k in ('subscription','trial_subscription','credential','redemption','device','pending_device')}
    data.update(password='long-fixture-password',session='long-fixture-session',ticket='long-fixture-ticket',link='long-fixture-link');data['config']='vless://'+data['credential']+'@example.invalid:443'
    subprocess.run([sys.executable,'-c',code,'seed'],input=json.dumps(data).encode(),env=env,capture_output=True,check=True)
    snap=lambda path:json.loads(subprocess.run([sys.executable,'-c',code,'snapshot',str(path)],env=env,capture_output=True,check=True).stdout)
    before=snap(folder/'shop.db');archive=tmp_path/'fixture.pvb'
    b.create(folder/'shop.db',('ENCRYPTION_KEY='+key+'\n').encode(),archive,'long-fixture-backup-password')
    b.restore(archive,'long-fixture-backup-password',tmp_path/'restored');after=snap(tmp_path/'restored/shop.db')
    assert before['digest']==after['digest'] and before['counts']['sessions']==1 and after['counts']['sessions']==0
    assert before['counts']['referral_rewards']==1 and before['counts']['trial_claims']==1 and before['counts']['coupon_uses']==1

from test_shop import client


def test_upgrade_allows_only_empty_new_key_table_and_preserves_existing_rows(monkeypatch):
    stage=s.Staging(type('Args',(),{})())
    base={'table_digests':{'users':'users-hash'},'counts':{'sessions':1,'tickets':1}}
    current={'table_digests':{'users':'users-hash','wgdashboard_keys':'empty-hash'},
             'counts':{'sessions':1,'tickets':1,'wgdashboard_keys':0}}
    monkeypatch.setattr(stage,'snapshot',lambda:current)
    stage.compare(base,cleared=False)
    current['counts']['wgdashboard_keys']=1
    with pytest.raises(s.StageError,match='Unexpected keys'):stage.compare(base,cleared=False)
    baseline={**current,'table_digests':dict(current['table_digests'])}
    stage.compare(baseline,cleared=False)
    current['table_digests']['wgdashboard_keys']='modified-key'
    with pytest.raises(s.StageError,match='changed=wgdashboard_keys'):stage.compare(baseline,cleared=False)
    del current['table_digests']['wgdashboard_keys']
    with pytest.raises(s.StageError,match='missing=wgdashboard_keys'):stage.compare(baseline,cleared=False)


def test_encrypted_wgdashboard_key_survives_real_backup_restore(client,tmp_path):
    import sqlite3,uuid
    from app import config as c
    from app.wgdashboard import stored_private_key
    from test_backup import b
    identity=str(uuid.uuid4());key=stored_private_key('test-wg',identity)
    archive=tmp_path/'keys.pvb'
    b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'test-backup-password-long')
    b.restore(archive,'test-backup-password-long',tmp_path/'restored-keys')
    with sqlite3.connect(tmp_path/'restored-keys/shop.db') as conn:
        row=conn.execute('SELECT server_id,credential_id,private_key FROM wgdashboard_keys').fetchone()
    assert row[:2]==('test-wg',identity) and row[2]!=key
    assert c.decrypt(row[2])==key


def test_upgrade_preserves_expiration_jobs_and_only_allows_empty_initial_table(monkeypatch):
    stage=s.Staging(type('Args',(),{})())
    baseline={'table_digests':{'users':'users-hash'},'counts':{'sessions':1,'tickets':1}}
    current={'table_digests':{'users':'users-hash','wgdashboard_expirations':'empty-hash'},
             'counts':{'sessions':1,'tickets':1,'wgdashboard_expirations':0}}
    monkeypatch.setattr(stage,'snapshot',lambda:current)
    stage.compare(baseline,cleared=False)
    current['counts']['wgdashboard_expirations']=1
    with pytest.raises(s.StageError,match='Unexpected expiration jobs'):
        stage.compare(baseline,cleared=False)
    existing={**current,'table_digests':dict(current['table_digests'])}
    stage.compare(existing,cleared=False)
    current['table_digests']['wgdashboard_expirations']='changed-job'
    with pytest.raises(s.StageError,match='changed=wgdashboard_expirations'):
        stage.compare(existing,cleared=False)
    del current['table_digests']['wgdashboard_expirations']
    with pytest.raises(s.StageError,match='missing=wgdashboard_expirations'):
        stage.compare(existing,cleared=False)


@pytest.mark.parametrize('table',['hysteria2_credentials','tuic_credentials'])
def test_upgrade_preserves_sui_passwords_and_allows_only_empty_new_table(monkeypatch,table):
    stage=s.Staging(type('Args',(),{})())
    base={'table_digests':{'users':'users-hash'},'counts':{'sessions':1,'tickets':1}}
    current={'table_digests':{'users':'users-hash',table:'empty-hash'},
             'counts':{'sessions':1,'tickets':1,table:0}}
    monkeypatch.setattr(stage,'snapshot',lambda:current)
    stage.compare(base,cleared=False)
    current['counts'][table]=1
    with pytest.raises(s.StageError,match='Unexpected credentials'):stage.compare(base,cleared=False)
    baseline={**current,'table_digests':dict(current['table_digests'])}
    stage.compare(baseline,cleared=False)
    current['table_digests'][table]='modified-secret'
    with pytest.raises(s.StageError,match='changed='+table):stage.compare(baseline,cleared=False)
    del current['table_digests'][table]
    with pytest.raises(s.StageError,match='missing='+table):stage.compare(baseline,cleared=False)


def test_upgrade_allows_exact_initial_update_settings_and_preserves_existing_rows(monkeypatch):
    stage=s.Staging(type('Args',(),{})())
    base={'table_digests':{'users':'users-hash'},'counts':{'sessions':1,'tickets':1}}
    current={'table_digests':{'users':'users-hash','update_checker':'default-settings'},
             'counts':{'sessions':1,'tickets':1,'update_checker':1}}
    settings={'id':1,'enabled':1,'check_time':'21:00','timezone':'Europe/Moscow','version':1,
              'next_check':0,'last_attempt':None,'last_success':None,'error_code':None,'release_json':None,
              'etag':'','failures':0,'retry_at':0,'lease_token':None,'lease_until':0}
    monkeypatch.setattr(stage,'snapshot',lambda:current)
    monkeypatch.setattr(stage,'data',lambda command:dict(settings))
    stage.compare(base,cleared=False)
    settings['check_time']='12:00'
    with pytest.raises(s.StageError,match='Unexpected initial update'):stage.compare(base,cleared=False)
    existing={**current,'table_digests':dict(current['table_digests'])}
    stage.compare(existing,cleared=False)
    current['table_digests']['update_checker']='changed-settings'
    with pytest.raises(s.StageError,match='changed=update_checker'):stage.compare(existing,cleared=False)
    del current['table_digests']['update_checker']
    with pytest.raises(s.StageError,match='missing=update_checker'):stage.compare(existing,cleared=False)


def test_custom_update_schedule_survives_real_encrypted_backup_restore(client,tmp_path):
    import sqlite3
    from app import config as c,updates
    from test_backup import b
    updates.save_schedule(updates.Schedule(enabled=False,check_time='22:30',timezone='UTC',version=1))
    with sqlite3.connect(c.DB) as conn:before=conn.execute('SELECT * FROM update_checker').fetchall()
    archive=tmp_path/'schedule.pvb'
    b.create(c.DB,('ENCRYPTION_KEY='+os.environ['ENCRYPTION_KEY']+'\n').encode(),archive,'test-backup-password-long')
    b.restore(archive,'test-backup-password-long',tmp_path/'restored-schedule')
    with sqlite3.connect(tmp_path/'restored-schedule/shop.db') as conn:after=conn.execute('SELECT * FROM update_checker').fetchall()
    assert after==before and after[0][1:4]==(0,'22:30','UTC')
