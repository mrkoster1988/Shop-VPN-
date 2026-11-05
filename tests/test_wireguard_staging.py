import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('wg_stage',Path(__file__).with_name('wireguard_staging.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

def stage():return s.Staging(SimpleNamespace(source=Path(__file__).parents[1]))

def test_requires_disposable_root_before_touching_docker(tmp_path,monkeypatch):
    monkeypatch.setattr(s.os,'geteuid',lambda:0)
    monkeypatch.setattr(s.subprocess,'run',lambda *a,**kw:pytest.fail('Docker must not run'))
    with pytest.raises(s.StageError):s.main(['--source',str(tmp_path),'--report',str(tmp_path/'report')])
    assert not (tmp_path/'report').exists()

def test_cleanup_rejects_preexisting_and_unowned_resources():
    data={'Id':'existing','Config':{'Labels':{s.LABEL:'ours'}}}
    assert not s.owned(data,'ours',{'existing'})
    assert s.owned(data,'ours',set())
    data['Config']['Labels'][s.LABEL]='someone-else'
    assert not s.owned(data,'ours',set())
    assert s.owned({'Id':'new','Labels':{s.LABEL:'ours'}},'ours',set())

def test_subprocess_failure_never_prints_private_config(monkeypatch):
    st=stage()
    monkeypatch.setattr(s.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess([],1,b'PrivateKey=private-secret',b'token=private-secret'))
    with pytest.raises(s.StageError) as error:st.run('configure fixture',['private-command','private-secret'])
    assert str(error.value)=='configure fixture: exit 1'

@pytest.mark.parametrize('result',[
    {'reachable':True,'source':'expected','nonce':'expected','interface':'eth0'},
    {'reachable':True,'source':'wrong','nonce':'expected','interface':'wg-client'},
    {'reachable':True,'source':'expected','nonce':'wrong','interface':'wg-client'},
])
def test_traffic_proof_rejects_bypass_wrong_source_and_wrong_sink(monkeypatch,result):
    st=stage();monkeypatch.setattr(st,'probe',lambda *a:result)
    with pytest.raises(s.StageError):st.traffic('client','sink','expected','expected')

def test_cleanup_does_not_remove_foreign_container(monkeypatch):
    st=stage();st.containers=['foreign'];calls=[]
    def run(label,args,**kwargs):
        calls.append(args)
        return '[{"Id":"foreign","Config":{"Labels":{"'+s.LABEL+'":"not-ours"}}}]'
    monkeypatch.setattr(st,'run',run);st.cleanup()
    assert not st.cleanup_ok
    assert all('rm' not in args for args in calls)
