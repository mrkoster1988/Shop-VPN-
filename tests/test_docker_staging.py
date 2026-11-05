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
