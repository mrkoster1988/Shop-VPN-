"""План, потеря ответа, повторный запуск и запрет опасного отката."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import uuid
import pytest

spec=importlib.util.spec_from_file_location('host_updater',Path(__file__).parents[1]/'scripts/host_updater.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)


def request(tag='v0.056', **extra):
    return {'request_id':str(uuid.uuid4()),'tag':tag,**extra}


@pytest.fixture
def host(tmp_path, monkeypatch):
    root=tmp_path/'installed';root.mkdir(mode=0o700)
    work=tmp_path/'cache';work.mkdir(mode=0o700)
    (root/'installer.json').write_text(json.dumps({'version':'v0.055'}))
    (root/'compose.install.json').write_text('{}');(root/'.env').write_text('secret-fixture')
    (root/'updater').mkdir(mode=0o700);(root/'updater/backup-password').write_text('long-fixture-secret')
    calls=[];fail={'phase':None}
    def fetch(tag,directory):
        directory.mkdir(mode=0o700);source=directory/'source';source.mkdir()
        return source,'a'*40
    def run(source,root,command,password=None,expected=None):
        calls.append(command)
        if command=='plan':return {'target':'v0.056','previous':'v0.055','plan_only':True,'backup':'encrypted','activation':'explicit','host_updater_protocol':1}
        phase={'prepare':'ready','activate':'committed','rollback':'rolled_back'}[command]
        data={'operation':'b'*24,'previous':'v0.055','target':'v0.056','phase':phase,'activation_started':command=='activate'}
        if fail['phase']==command:
            data['phase']='activation_failed' if command=='activate' else 'recovery_required'
        (root/'upgrade.json').write_text(json.dumps(data))
        if fail['phase']==command:raise RuntimeError('do not expose token from provider')
        return {'phase':phase}
    monkeypatch.setattr(h.update_source,'fetch',fetch);monkeypatch.setattr(h,'run_upgrade',run)
    return h.Controller(root,{'work':str(work)}),calls,fail


def plan(host):
    c,*_=host;payload=request();c.submit('plan',payload);assert c.process_one()
    result=c.status()['job'];assert result['state']=='ready'
    return payload['request_id']


def test_apply_idempotency_survives_lost_ack_and_restart(host):
    c,calls,_=host;pid=plan(host)
    payload=request(plan_id=pid)
    c.submit('apply',payload);c.submit('apply',payload)
    assert c.process_one() and not c.process_one()
    assert c.status()['job']['state']=='succeeded' and calls==['plan','prepare','activate']
    again=h.Controller(c.root,{'work':str(c.work)})
    assert again.submit('apply',payload)['job']['state']=='succeeded'
    assert not again.process_one()
    with pytest.raises(h.Rejected):again.submit('apply',request(plan_id=pid))
    assert 'secret' not in json.dumps(again.status())


def test_single_operation_and_request_id_conflict(host):
    c,*_=host;p=request();c.submit('plan',p)
    with pytest.raises(h.Rejected,match='operation_in_progress'):c.submit('plan',request())
    with pytest.raises(h.Rejected,match='request_id_conflict'):c.submit('plan',{**p,'tag':'v0.057'})


@pytest.mark.parametrize('name',['.env','installer.json','compose.install.json'])
def test_changed_installation_rejects_plan_before_any_stop(host,name):
    c,calls,_=host;pid=plan(host);(c.root/name).write_text('changed')
    with pytest.raises((h.Rejected,ValueError)):c.submit('apply',request(plan_id=pid))
    assert calls==['plan']


def test_changed_after_enqueue_still_rejected(host):
    c,calls,_=host;pid=plan(host);c.submit('apply',request(plan_id=pid))
    (c.root/'.env').write_text('changed');c.process_one()
    assert calls==['plan'] and c.status()['job']['state']=='failed'


def test_expired_plan(host,monkeypatch):
    c,*_=host;pid=plan(host);future=c.get(pid)['data']['expires']+1
    monkeypatch.setattr(h.time,'time',lambda:future)
    with pytest.raises(h.Rejected,match='expired'):c.submit('apply',request(plan_id=pid))


def test_restart_never_resumes_queued_update(host):
    c,calls,_=host;pid=plan(host);payload=request(plan_id=pid);c.submit('apply',payload)
    restarted=h.Controller(c.root,{'work':str(c.work)})
    assert restarted.status()['job']['state']=='interrupted'
    assert not restarted.process_one() and calls==['plan']
    restarted.submit('apply',payload);assert not restarted.process_one()


def test_pre_activation_failure_allows_explicit_rollback(host):
    c,calls,fail=host;pid=plan(host);fail['phase']='prepare'
    c.submit('apply',request(plan_id=pid));c.process_one()
    assert c.status()['job']['state']=='recovery_required'
    with pytest.raises(h.Rejected,match='recovery_required'):c.submit('plan',request())
    fail['phase']=None;c.submit('rollback',request(plan_id=pid));c.process_one()
    assert c.status()['job']['state']=='rolled_back' and calls[-1]=='rollback'


def test_activation_failure_blocks_database_rollback(host):
    c,calls,fail=host;pid=plan(host);fail['phase']='activate'
    c.submit('apply',request(plan_id=pid));c.process_one()
    assert c.status()['job']['activation_started'] is True
    with pytest.raises(h.Rejected,match='rollback_forbidden'):c.submit('rollback',request(plan_id=pid))
    assert 'rollback' not in calls and 'provider' not in json.dumps(c.status())


@pytest.mark.parametrize('payload',[{}, {'request_id':'x','tag':'v0.056'}, request(tag='../main'),request(tag='v0.054'),request(url='https://evil.example'),request(command='sh')])
def test_rejects_unbounded_requests_and_downgrades(host,payload):
    c,calls,_=host
    with pytest.raises((ValueError,KeyError,TypeError)):c.submit('plan',payload)
    assert not calls


def make_tar(path, names):
    with tarfile.open(path,'w:gz') as tar:
        for name,kind in names:
            item=tarfile.TarInfo(name);item.type=kind
            if kind==tarfile.REGTYPE:item.size=2;tar.addfile(item,io.BytesIO(b'ok'))
            else:item.linkname='/etc/passwd';tar.addfile(item)


@pytest.mark.parametrize('names',[[('../escape',tarfile.REGTYPE)],[('/absolute',tarfile.REGTYPE)],
    [('root/link',tarfile.SYMTYPE)],[('root/hard',tarfile.LNKTYPE)],[('root/fifo',tarfile.FIFOTYPE)],
    [('one/file',tarfile.REGTYPE),('two/file',tarfile.REGTYPE)],
    [('root/file',tarfile.REGTYPE),('root/file',tarfile.REGTYPE)]])
def test_archive_rejects_escape_links_duplicates_and_multiple_roots(tmp_path,names):
    archive=tmp_path/'test.tar.gz';make_tar(archive,names)
    with pytest.raises((ValueError,FileExistsError)):h.update_source.extract(archive,tmp_path/'out')
    assert not (tmp_path/'escape').exists()


def test_download_uses_resolved_commit_not_response_url(tmp_path,monkeypatch):
    urls=[]
    def download(url,limit,target=None):
        urls.append(url)
        if '/releases/' in url:return {'tag_name':'v0.056','draft':False,'prerelease':False,'published_at':'2026-11-13','tarball_url':'http://evil/'}
        if '/git/ref/' in url:return {'object':{'type':'tag','sha':'a'*40}}
        if '/git/tags/' in url:return {'object':{'type':'commit','sha':'b'*40}}
        raise AssertionError(url)
    monkeypatch.setattr(h.update_source,'download',download)
    assert h.update_source.resolve('v0.056')=='b'*40
    assert all(url.startswith(h.update_source.API) for url in urls)


def test_rollback_cannot_use_old_unrelated_journal(host):
    c,*_=host;pid=plan(host)
    (c.root/'upgrade.json').write_text(json.dumps({'phase':'ready','target':'v0.056','previous':'v0.055','operation':'e'*24,'activation_started':False}))
    with pytest.raises(h.Rejected):c.submit('rollback',request(plan_id=pid))


def test_http_auth_origin_and_request_size_without_socket(host):
    from email.message import Message
    c,*_=host;handler=h.handler(c,'a'*64)
    def instance(headers):
        obj=object.__new__(handler);obj.headers=Message()
        for key,value in headers.items():obj.headers[key]=value
        return obj
    assert not instance({}).authorized()
    assert not instance({'Authorization':'Bearer wrong'}).authorized()
    assert not instance({'Authorization':'Bearer '+'a'*64,'Origin':'https://site.example'}).authorized()
    assert not instance({'Authorization':'Bearer '+'a'*64,'Transfer-Encoding':'chunked'}).authorized()
    obj=instance({'Authorization':'Bearer '+'a'*64,'Content-Length':'9000'})
    obj.path='/v1/apply';obj.rfile=io.BytesIO(b'');responses=[];obj.respond=lambda code,body:responses.append(code)
    obj.do_POST();assert responses==[409]


def test_unsupported_target_protocol_cannot_be_applied(host,monkeypatch):
    c,*_=host
    monkeypatch.setattr(h,'run_upgrade',lambda *a,**k:{'target':'v0.056','previous':'v0.055','plan_only':True,'backup':'encrypted','activation':'explicit'})
    c.submit('plan',request());c.process_one()
    assert c.status()['job']['state']=='failed'


def test_expanded_stream_limit_includes_archive_headers(tmp_path,monkeypatch):
    archive=tmp_path/'test.tar.gz';make_tar(archive,[('root/file',tarfile.REGTYPE)])
    monkeypatch.setattr(h.update_source,'MAX_EXPANDED',1024)
    with pytest.raises(ValueError,match='expanded_archive_limit'):
        h.update_source.extract(archive,tmp_path/'out')
