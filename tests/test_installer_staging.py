import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('installer_stage', Path(__file__).with_name('installer_staging.py'))
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)


@pytest.mark.parametrize('disposable,uid,info', [
    (False, 0, {'ID':'ubuntu','VERSION_ID':'24.04'}),
    (True, 1000, {'ID':'ubuntu','VERSION_ID':'24.04'}),
    (True, 0, {'ID':'debian','VERSION_ID':'13'}),
    (True, 0, {'ID':'ubuntu','VERSION_ID':'22.04'}),
    (True, 0, {}),
])
def test_installer_staging_refuses_non_disposable_or_wrong_host(disposable,uid,info):
    with pytest.raises(s.StageError): s.guard(disposable,uid,info)


def test_guard_accepts_explicit_ubuntu_24_root():
    s.guard(True,0,{'ID':'ubuntu','VERSION_ID':'24.04'})


def test_main_guard_runs_before_commands_or_report(tmp_path,monkeypatch):
    monkeypatch.setattr(s.os,'geteuid',lambda:0)
    monkeypatch.setattr(s.platform,'freedesktop_os_release',lambda:{'ID':'ubuntu','VERSION_ID':'24.04'})
    monkeypatch.setattr(s.helpers.subprocess,'run',lambda *a,**k:pytest.fail('Docker must not run'))
    with pytest.raises(s.StageError):
        s.main(['--source',str(tmp_path),'--report',str(tmp_path/'result.json')])
    assert not (tmp_path/'result.json').exists()


def stage_with_env(tmp_path):
    stage=s.InstallerStage(SimpleNamespace())
    stage.root=tmp_path/'installed'; stage.root.mkdir()
    stage.source=tmp_path/'source'; stage.port=18901; stage.public_url='https://shop.install.test/shop'
    (stage.root/'.env').write_text("ADMIN_TOKEN='private-admin'\nENCRYPTION_KEY='private-key'\nWG_AGENT_TOKEN='private-agent'\n")
    return stage


def test_failed_installer_output_redacts_newly_created_secrets(tmp_path,monkeypatch):
    stage=stage_with_env(tmp_path)
    monkeypatch.setattr(s.helpers.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stderr='private-admin private-key private-agent',stdout=''))
    with pytest.raises(s.StageError) as error: stage.install()
    assert '[redacted]' in str(error.value)
    assert all(value not in str(error.value) for value in ('private-admin','private-key','private-agent'))


@pytest.mark.parametrize('stdout',['Готово: https://shop.install.test/shop','HTTPS пока не подключён\nГотово:'])
def test_external_exit_two_cannot_be_reported_as_public_success(tmp_path,monkeypatch,stdout):
    stage=stage_with_env(tmp_path)
    monkeypatch.setattr(s.helpers.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=2,stderr='',stdout=stdout))
    with pytest.raises(s.StageError): stage.install()
