import importlib.util
import json
import os
from pathlib import Path
import subprocess
import pytest
from dotenv import dotenv_values
from cryptography.fernet import Fernet

spec=importlib.util.spec_from_file_location('installer',Path(__file__).parents[1]/'scripts/install.py')
i=importlib.util.module_from_spec(spec);spec.loader.exec_module(i)

@pytest.mark.parametrize('url',['http://example.com/shop','https://user:pass@example.com/shop','https://example.com','https://example.com/shop?x=1','https://example.com/a;bad','https://example.com:8443/shop','https://example.com/shop#x','https://example.com/../shop'])
def test_url_refuses_unsafe_or_ambiguous_routes(url):
    with pytest.raises(ValueError):i.public_url(url)


def test_url_and_proxy_configs_preserve_prefix_and_hide_login_logs(tmp_path):
    url=i.public_url('https://pushok.shop/shop/')
    settings={'url':url,'port':8010,'mode':'caddy'}
    routes=i.nginx_routes(url,8010)
    assert 'location = /shop/login' in routes and 'access_log off;' in routes
    assert 'proxy_pass http://127.0.0.1:8010;' in routes and 'proxy_pass http://127.0.0.1:8010/;' not in routes
    data=json.loads(i.compose(settings,tmp_path))
    assert data['services']['shop']['network_mode']=='host'
    assert data['services']['shop']['command'][6:8]==['--port','8010']
    assert '/shop/health' in data['services']['shop']['healthcheck']['test'][-1]
    assert 'ports' not in data['services']['shop']
    assert 'caddy_data' in data['volumes']


def test_env_is_private_valid_and_does_not_interpolate(tmp_path):
    path=tmp_path/'.env'
    i.atomic_text(path,i.environment({'url':'https://example.com/shop','support':"Support $HOME ${TOKEN} 'yes' \\ literal"}))
    env=dotenv_values(path,interpolate=False)
    assert env['SUPPORT_TEXT']=="Support $HOME ${TOKEN} 'yes' \\ literal"
    assert len(env['ADMIN_TOKEN'])>=32 and len(env['WG_AGENT_TOKEN'])>=32
    Fernet(env['ENCRYPTION_KEY'].encode())
    assert path.stat().st_mode & 0o777==0o600
    with pytest.raises(ValueError):i.quote_env('bad\nvalue')

@pytest.mark.parametrize('info',[{'ID':'debian','VERSION_ID':'12'},{'ID':'ubuntu','VERSION_ID':'20.04'}])
def test_unsupported_os_fails_before_changes(info):
    with pytest.raises(ValueError):i.check_os(info)


def test_nginx_insertion_preserves_site_and_is_idempotent():
    text='''# site
server { listen 80; server_name example.com; return 301 https://$host$request_uri; }
server {
 listen 443 ssl;
 server_name example.com www.example.com;
 location / { proxy_pass http://127.0.0.1:3000; }
 # brace in comment }
}
'''
    result=i.nginx_insert(text,'example.com',Path('/opt/shop/nginx.shop.conf'),'/shop')
    assert 'proxy_pass http://127.0.0.1:3000;' in result
    assert result.count('include /opt/shop/nginx.shop.conf;')==1
    assert i.nginx_insert(result,'example.com',Path('/opt/shop/nginx.shop.conf'),'/shop')==result

@pytest.mark.parametrize('text',[
    'server { listen 80; server_name example.com; }',
    'server { listen 443 ssl; server_name other.com; }',
    'server { listen 443 ssl; server_name example.com; location /shop/ { proxy_pass http://old; } }',
    'server { listen 443 ssl; server_name example.com; }\nserver {listen 443 ssl;server_name example.com;}',
    'server {listen 443 ssl; server_name ${DOMAIN};}',
])
def test_ambiguous_nginx_requires_explicit_external_mode(text):
    with pytest.raises(ValueError):i.nginx_insert(text,'example.com',Path('/opt/shop/snippet'),'/shop')


@pytest.mark.parametrize('fail_at',[2,3])
def test_nginx_failure_restores_config_and_retains_backup(tmp_path,monkeypatch,fail_at):
    config=tmp_path/'nginx.conf';old='server {listen 443 ssl; server_name example.com; location / {return 200;} }'
    config.write_text(old);calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if len(calls)==fail_at:raise subprocess.CalledProcessError(1,args)
    monkeypatch.setattr(i,'run',run)
    with pytest.raises(RuntimeError):i.attach_nginx(config,{'url':'https://example.com/shop'},tmp_path)
    assert config.read_text()==old
    assert next((tmp_path/'backups').iterdir()).read_text()==old
    assert ['systemctl','reload','nginx'] in calls

@pytest.fixture
def system(tmp_path,monkeypatch):
    root=tmp_path/'installed';source=tmp_path/'src';(source/'scripts').mkdir(parents=True)
    (source/'scripts/install.py').write_text('# source');(source/'Dockerfile').write_text('FROM test')
    monkeypatch.setattr(i,'check_os',lambda:'noble');monkeypatch.setattr(i.os,'geteuid',lambda:0)
    monkeypatch.setattr(i,'port_free',lambda *a:True);monkeypatch.setattr(i,'docker_ready',lambda _:None)
    calls=[];health=[]
    monkeypatch.setattr(i,'run',lambda args,**kw:calls.append(args))
    monkeypatch.setattr(i,'wait_health',lambda url,*a:health.append(url))
    args=['--yes','--dir',str(root),'--source',str(source),'--url','https://example.com/shop','--mode','caddy']
    return root,args,calls,health


def test_dry_plan_has_no_side_effects(system):
    root,args,calls,health=system
    assert i.main(args+['--plan'])==0
    assert not root.exists() and not calls and not health


def test_install_and_repeat_keep_secrets_and_volume_identity(system):
    root,args,calls,health=system
    assert i.main(args)==0
    config=(root/'.env').read_bytes();state=json.loads((root/'installer.json').read_text())
    compose=(root/'compose.install.json').read_bytes()
    assert i.main(args)==0
    assert (root/'.env').read_bytes()==config and (root/'compose.install.json').read_bytes()==compose
    assert state['project'] in calls[0] and state['project'] in calls[1]
    assert health[-1]=='https://example.com/shop/health'
    assert not any('down' in call for call in calls)


def test_existing_unmanaged_directory_is_not_modified(system):
    root,args,calls,_=system;root.mkdir();(root/'.env').write_text('private')
    with pytest.raises(ValueError):i.main(args)
    assert (root/'.env').read_text()=='private' and not calls


def test_busy_ports_do_not_create_installation(system,monkeypatch):
    root,args,calls,_=system;monkeypatch.setattr(i,'port_free',lambda *a:False)
    with pytest.raises(ValueError):i.main(args)
    assert not root.exists() and not calls


def test_external_mode_never_claims_https_ready(system):
    root,args,calls,health=system;args[-1]='external'
    assert i.main(args)==2 and all(url.startswith('http://127.0.0.1:') for url in health)


def test_docker_failure_is_resumable_without_regenerating_env(system,monkeypatch):
    root,args,calls,_=system
    def fail(_):raise RuntimeError('docker unavailable')
    monkeypatch.setattr(i,'docker_ready',fail)
    with pytest.raises(RuntimeError):i.main(args)
    env=(root/'.env').read_bytes()
    monkeypatch.setattr(i,'docker_ready',lambda _:None)
    assert i.main(args)==0 and (root/'.env').read_bytes()==env


def test_repeat_cannot_change_identity(system):
    root,args,calls,_=system;i.main(args)
    changed=args[:];changed[changed.index('--url')+1]='https://other.com/shop'
    with pytest.raises(ValueError):i.main(changed)


def test_no_installation_inside_own_source(system):
    root,args,calls,_=system
    source=Path(args[args.index('--source')+1]);args[args.index('--dir')+1]=str(source/'nested')
    with pytest.raises(ValueError):i.main(args)
    assert not (source/'nested').exists()


def test_bootstrap_release_matches_installer_and_changelog():
    import re
    root=Path(__file__).parents[1]
    bootstrap=re.search(r'^VERSION=(v[0-9]+\.[0-9]{3})$',(root/'install.sh').read_text(),re.M).group(1)
    newest=re.search(r'^## \[([0-9]+\.[0-9]{3})\]',(root/'CHANGELOG.md').read_text(),re.M).group(1)
    assert bootstrap==i.VERSION=='v'+newest


def test_install_path_cannot_hide_a_system_directory(tmp_path):
    link=tmp_path/'system';link.symlink_to('/etc',target_is_directory=True)
    with pytest.raises(ValueError):i.install_path(str(link))


def test_subscription_urls_are_redacted_in_proxy_templates():
    root=Path(__file__).parents[1]
    routes=i.nginx_routes('https://example.com/shop',8010)
    block=routes.split('location ^~ /shop/sub/')[1].split('location ^~ /shop/')[0]
    assert 'access_log off;' in block and 'error_log /dev/null crit;' in block
    caddy=i.caddy_config('example.com','/shop',8010)
    assert '@private_shop path /shop/login /shop/sub/*' in caddy and 'log_skip @private_shop' in caddy
    assert 'request>uri replace REDACTED' in caddy
    assert 'location ^~ /shop/sub/' in (root/'nginx.example.conf').read_text()


@pytest.mark.parametrize('upgrade',[False,True])
def test_bootstrap_pins_install_and_upgrade_to_current_release(tmp_path,upgrade):
    import io,tarfile
    root=Path(__file__).parents[1];archive=tmp_path/'mock.tar.gz';binary=tmp_path/'bin';binary.mkdir();log=tmp_path/'curl-args'
    with tarfile.open(archive,'w:gz') as tar:
        code=b'import json,sys;print(json.dumps({"script":sys.argv[0],"args":sys.argv[1:]}))\n'
        for name in ('install.py','upgrade.py'):
            member=tarfile.TarInfo('release/scripts/'+name);member.size=len(code);tar.addfile(member,io.BytesIO(code))
    curl=binary/'curl';curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$MOCK_LOG"\nprevious=""\nfor item do\n if [ "$previous" = "-o" ]; then cp "$MOCK_ARCHIVE" "$item"; fi\n previous="$item"\ndone\n');curl.chmod(0o755)
    sudo=binary/'sudo';sudo.write_text('#!/bin/sh\nexec "$@"\n');sudo.chmod(0o755)
    env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'MOCK_LOG':str(log),'MOCK_ARCHIVE':str(archive)}
    result=subprocess.run(['bash',str(root/'install.sh'),*(['--upgrade','plan'] if upgrade else ['--plan'])],env=env,capture_output=True,text=True,check=True)
    output=json.loads(result.stdout)
    assert output['script'].endswith('/scripts/'+('upgrade.py' if upgrade else 'install.py'))
    assert ('/'+i.VERSION+'.tar.gz') in log.read_text()
    assert '--source' in output['args'] and ('plan' if upgrade else '--plan') in output['args']


def run_bootstrap_archive(tmp_path, archive):
    """Подмена только скачивания: настоящий bash-загрузчик и распаковка Python."""
    binary=tmp_path/'bootstrap-bin';binary.mkdir()
    curl=binary/'curl'
    curl.write_text('''#!/usr/bin/env python3
import os,sys,shutil
from pathlib import Path
args=sys.argv[1:];source=Path(os.environ['MOCK_ARCHIVE'])
limit=int(args[args.index('--max-filesize')+1])
if source.stat().st_size>limit:sys.exit(63)
shutil.copyfile(source,args[args.index('-o')+1])
''')
    curl.chmod(0o755)
    env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'MOCK_ARCHIVE':str(archive)}
    return subprocess.run(['bash',str(Path(__file__).parents[1]/'install.sh'),'--plan'],
                          env=env,capture_output=True,text=True)


def test_bootstrap_accepts_source_archive_larger_than_old_ten_mib_limit(tmp_path):
    import io,tarfile
    archive=tmp_path/'large.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in [('scripts/install.py',b'print("plan-reached")\n'),
                          ('docs/artwork.bin',os.urandom(11*1024*1024))]:
            member=tarfile.TarInfo('release/'+name);member.size=len(data)
            tar.addfile(member,io.BytesIO(data))
    assert archive.stat().st_size>10*1024*1024
    result=run_bootstrap_archive(tmp_path,archive)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='plan-reached'


@pytest.mark.parametrize('fault',['traversal','absolute','symlink','hardlink','device','expanded_size','member_count'])
def test_bootstrap_still_rejects_unsafe_archives_before_execution(tmp_path,fault):
    import io,tarfile
    archive=tmp_path/'unsafe.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        script=b'print("must-not-run")\n'
        member=tarfile.TarInfo('release/scripts/install.py');member.size=len(script)
        tar.addfile(member,io.BytesIO(script))
        bad=tarfile.TarInfo('release/bad')
        if fault=='traversal':bad.name='release/../../escape'
        elif fault=='absolute':bad.name=str(tmp_path/'escape')
        elif fault=='symlink':bad.type=tarfile.SYMTYPE;bad.linkname='../escape'
        elif fault=='hardlink':bad.type=tarfile.LNKTYPE;bad.linkname='../escape'
        elif fault=='device':bad.type=tarfile.CHRTYPE
        elif fault=='expanded_size':
            data=b'\0'*(64*1024*1024+1);bad.size=len(data);tar.addfile(bad,io.BytesIO(data))
        elif fault=='member_count':
            for n in range(2001):tar.addfile(tarfile.TarInfo('release/empty-'+str(n)))
        if fault not in ('expanded_size','member_count'):tar.addfile(bad)
    result=run_bootstrap_archive(tmp_path,archive)
    assert result.returncode!=0 and 'must-not-run' not in result.stdout
    assert not (tmp_path/'escape').exists()


def test_bootstrap_download_remains_bounded(tmp_path):
    archive=tmp_path/'oversize.tar.gz'
    with archive.open('wb') as stream:stream.truncate(33*1024*1024)
    result=run_bootstrap_archive(tmp_path,archive)
    assert result.returncode==63

@pytest.mark.parametrize('raw,expected',[
    ('https://vpn.alpha.example/shop/','https://vpn.alpha.example/shop'),
    ('https://SHOP.BETA.EXAMPLE/shop','https://shop.beta.example/shop'),
    ('https://магазин.рф/shop','https://xn--80aairftm.xn--p1ai/shop'),
    (' https://vpn.example.com/store/vpn/ ','https://vpn.example.com/store/vpn'),
])
def test_custom_domains_are_normalized_without_project_domain(raw,expected):
    assert i.public_url(raw)==expected

@pytest.mark.parametrize('raw',[
    'https://bad..example/shop','https://-bad.example/shop','https://bad-.example/shop',
    'https://example.com./shop','https://example.com:443/shop','https://example.com:bad/shop',
    'https://'+'a'*64+'.example/shop','https://example.com/%2e%2e/shop',
])
def test_invalid_domain_labels_and_ports_refused(raw):
    with pytest.raises(ValueError):i.public_url(raw)

def test_domain_prompt_has_no_personal_default_and_retries_empty(monkeypatch,capsys):
    answers=iter(['','https://custom-owner.example/shop']);prompts=[]
    def ask(label,default='',secret=False):
        prompts.append((label,default));return next(answers)
    monkeypatch.setattr(i,'ask',ask)
    assert i.choose_public_url(None)=='https://custom-owner.example/shop'
    assert len(prompts)==2 and all(default=='' for label,default in prompts)
    assert 'pushok.shop' not in capsys.readouterr().out

def test_explicit_url_does_not_prompt(monkeypatch):
    monkeypatch.setattr(i,'ask',lambda *a,**kw:pytest.fail('No prompt for --url'))
    assert i.choose_public_url('https://owner.example/shop')=='https://owner.example/shop'

@pytest.mark.parametrize('url',['https://pushok.shop/shop','https://OTHER.OWNER.EXAMPLE/shop/'])
def test_install_persists_selected_owner_domain_and_repeat_never_replaces_it(system,url):
    root,args,calls,health=system
    args[args.index('--url')+1]=url
    assert i.main(args)==0
    saved=(root/'.env').read_bytes();settings=json.loads((root/'installer.json').read_text())
    canonical=i.public_url(url)
    assert settings['url']==canonical and dotenv_values(root/'.env')['PUBLIC_URL']==canonical
    assert canonical.split('/')[2] in (root/'Caddyfile').read_text()
    assert i.main(args)==0 and (root/'.env').read_bytes()==saved
    args[args.index('--url')+1]='https://different-owner.example/shop'
    with pytest.raises(ValueError):i.main(args)
    assert (root/'.env').read_bytes()==saved


def test_panel_dialog_retries_and_selects_both(monkeypatch):
    answers=iter(['bad','3'])
    monkeypatch.setattr(i,'ask',lambda *a,**kw:next(answers))
    assert i.choose_panels()=='both'


def test_panel_dialog_skips_existing_and_unattended(monkeypatch):
    monkeypatch.setattr(i,'ask',lambda *a,**kw:pytest.fail('Unexpected prompt'))
    assert i.choose_panels(existing=True)=='none'
    assert i.choose_panels(unattended=True)=='none'
    assert i.choose_panels('xui')=='xui'
    with pytest.raises(ValueError):i.choose_panels('both',unattended=True)
