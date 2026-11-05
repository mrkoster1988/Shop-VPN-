"""Real Docker integration runner. Only for an explicitly disposable Linux host."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import ast
import hashlib
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen,install_opener,build_opener,ProxyHandler,HTTPSHandler
import uuid

class StageError(RuntimeError):pass


def check(condition,message):
    if not condition:raise StageError(message)


def version(source):
    tree=ast.parse((source/'scripts/install.py').read_text())
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets):return ast.literal_eval(node.value)
    raise StageError('Source release not found')


def volume_names(root,project):
    names={project+'_shop_data'}
    for path in (root/'upgrades').glob('*') if (root/'upgrades').is_dir() else []:
        if re.fullmatch(r'[a-f0-9]{24}',path.name) and not path.is_symlink():names.add(project+'-upgrade-'+path.name)
    return names


def owned_container(info,project,volumes,initial):
    if info['Id'] in initial:return False
    return (info.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==project or any(m.get('Name') in volumes for m in info.get('Mounts',[]))


class Staging:
    def __init__(self,args):
        self.args=args;self.secrets=[];self.events=[];self.initial_containers=set();self.initial_volumes=set();self.initial_tags=set();self.cleanup_ok=True
        self.root=None;self.project=None;self.compose=None;self.proxy=None
    def scrub(self,text):
        for value in self.secrets:
            if value:text=text.replace(value,'[redacted]')
        return text[-6000:]
    def run(self,label,args,*,input=None,env=None,ok=(0,),timeout=900):
        result=subprocess.run(args,input=input,capture_output=True,env=env,timeout=timeout)
        if result.returncode not in ok:raise StageError(label+': exit '+str(result.returncode)+' '+self.scrub(result.stderr.decode(errors='replace')))
        return result.stdout.decode().strip(),result.returncode
    def json(self,label,args,**kwargs):return json.loads(self.run(label,args,**kwargs)[0])
    def event(self,name):
        self.events.append(name);print(json.dumps({'docker_stage':name}),flush=True)
    def container(self):
        cid=self.run('find shop',self.compose+['ps','-q','shop'])[0];check(bool(re.fullmatch(r'[a-f0-9]{12,64}',cid)),'Expected one running shop');return cid
    def data(self,command,payload=None,database=None):
        args=['docker','exec','-i',self.container(),'python','-c',self.data_code,command]
        if database:args.append(database)
        return self.json(command,args,input=json.dumps(payload).encode() if payload else None)
    def snapshot(self):return self.data('snapshot')
    def http(self,path,*,cookie=None,body=None,expected=200):
        headers={'Authorization':'Bearer '+self.admin}
        if cookie:headers['Cookie']='session='+cookie
        if body is not None:headers.update({'Content-Type':'application/json','Origin':'https://127.0.0.1'})
        request=Request('https://127.0.0.1/shop'+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        try:response=urlopen(request,timeout=10)
        except HTTPError as error:response=error
        with response:
            data=response.read(262144);status=response.status;result_headers=response.headers
        check(status==expected,'Unexpected HTTP status for staging request: '+str(status))
        return data,result_headers
    def controller(self,source,command,expected):
        spec=importlib.util.spec_from_file_location('staging_upgrade_'+secrets.token_hex(8),self.args.source/'scripts/upgrade.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        # Construct an older-image fixture with the corrected engine; no Docker calls are mocked.
        if source==self.args.intermediate:module.VERSION=version(source)
        output=io.StringIO()
        try:
            with redirect_stdout(output):module.main([command,'--source',str(source),'--dir',str(self.root),'--password-file',str(self.password_file)])
        except subprocess.CalledProcessError as error:
            detail=error.stderr.decode(errors='replace') if isinstance(error.stderr,bytes) else str(error.stderr or '')
            phase={}
            if (self.root/'upgrade.json').is_file():
                state=json.loads((self.root/'upgrade.json').read_text());phase={k:state.get(k) for k in ('phase','failure_stage','target')}
            raise StageError('upgrade '+command+' '+json.dumps(phase)+' '+self.scrub(detail)) from None
        result=json.loads(output.getvalue());check(result['phase']==expected,'Unexpected controller phase');return result
    def compare(self,expected,*,cleared):
        current=self.snapshot();check(current['digest']==expected['digest'],'Application data changed across snapshot/upgrade')
        check(current['counts']['sessions']==(0 if cleared else 1) and current['counts']['tickets']==(0 if cleared else 1),'Session cleanup mismatch')
        if cleared:check(current['db_uid']==10001 and current['db_gid']==10001 and current['db_mode']==0o600 and current['directory_mode']==0o700,'Restored file permissions mismatch')
        return current
    def start_proxy(self,port,certificate,key):
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.forward()
            def do_POST(self):self.forward()
            def forward(self):
                size=int(self.headers.get('Content-Length','0'));body=self.rfile.read(size) if size else None
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
                try:
                    headers={k:v for k,v in self.headers.items() if k.lower() not in ('connection','transfer-encoding')}
                    conn.request(self.command,self.path,body=body,headers=headers);response=conn.getresponse();raw=response.read()
                    self.send_response(response.status)
                    for name,value in response.getheaders():
                        if name.lower() not in ('content-length','connection','transfer-encoding'):self.send_header(name,value)
                    self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except OSError:self.send_error(502,'Fixture backend unavailable')
                finally:conn.close()
        self.proxy=ThreadingHTTPServer(('127.0.0.1',443),Proxy)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certificate,key)
        self.proxy.socket=context.wrap_socket(self.proxy.socket,server_side=True)
        threading.Thread(target=self.proxy.serve_forever,daemon=True).start()
    def setup(self,temp):
        import importlib.util
        spec=importlib.util.spec_from_file_location('staging_installer',self.args.source/'scripts/install.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        self.root=temp/'installed';self.root.mkdir(mode=0o700);self.project='pushok-'+hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        self.initial_containers=set(self.run('inventory containers',['docker','ps','-aq','--no-trunc'])[0].split())
        self.initial_volumes=set(self.run('inventory volumes',['docker','volume','ls','-q'])[0].split())
        self.initial_tags=set(self.run('inventory images',['docker','image','ls','--format','{{.Repository}}:{{.Tag}}'])[0].split())
        check(self.project+'_shop_data' not in self.initial_volumes and self.project+'-shop:latest' not in self.initial_tags,'Fixture resource name already exists')
        check(not self.run('check fixture namespace',['docker','ps','-aq','--filter','label=com.docker.compose.project='+self.project])[0],'Fixture project already exists')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        settings={'version':'v0.021','url':'https://127.0.0.1/shop','mode':'external','port':port,'project':self.project,'nginx_config':None,'support':'Disposable CI fixture'}
        installer.atomic_text(self.root/'installer.json',json.dumps(settings))
        raw=installer.environment(settings)+"BACKGROUND_TASKS='false'\nMAINTENANCE_MODE='false'\n"
        installer.atomic_text(self.root/'.env',raw)
        values=dict(line.split('=',1) for line in raw.splitlines());self.admin=ast.literal_eval(values['ADMIN_TOKEN'])
        self.secrets.extend(ast.literal_eval(values[k]) for k in ('ADMIN_TOKEN','ENCRYPTION_KEY','WG_AGENT_TOKEN'))
        self.password=secrets.token_urlsafe(32);self.secrets.append(self.password);self.password_file=temp/'password';installer.atomic_text(self.password_file,self.password)
        shutil.copytree(self.args.baseline,self.root/'source',ignore=shutil.ignore_patterns('.git','.env','.venv','__pycache__','data','*.pvb','upgrades','backups'))
        installer.atomic_text(self.root/'compose.install.json',installer.compose(settings,self.root))
        self.compose=['docker','compose','--project-name',self.project,'--env-file',str(self.root/'.env'),'-f',str(self.root/'compose.install.json')]
        self.data_code=(self.args.source/'tests/staging_data.py').read_text()
        cert=temp/'certificate.pem';key=temp/'key.pem'
        self.run('fixture certificate',['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'])
        os.environ['SSL_CERT_FILE']=str(cert);os.environ['NO_PROXY']='127.0.0.1,localhost,::1'
        # Each disposable scenario has a new CA; do not reuse urllib's previous HTTPS context.
        install_opener(build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=str(cert)))))
        self.start_proxy(port,cert,key)
        self.run('build baseline',self.compose+['build','shop'])
        self.fixture={name:str(uuid.uuid4()) for name in ('subscription','trial_subscription','credential','redemption','device','pending_device')}
        self.fixture.update(password=secrets.token_urlsafe(32),session=secrets.token_urlsafe(32),ticket=secrets.token_urlsafe(32),link=secrets.token_urlsafe(32))
        self.fixture['config']='vless://'+self.fixture['credential']+'@example.invalid:443'
        self.secrets.extend(self.fixture[k] for k in ('password','session','ticket','link','config'))
        self.json('seed baseline',self.compose+['run','--rm','--no-deps','--entrypoint','python','shop','-c',self.data_code,'seed'],input=json.dumps(self.fixture).encode())
        self.run('start baseline',self.compose+['up','-d','--no-deps','--no-build','--wait','--wait-timeout','120','shop'])
        self.backend_url='http://127.0.0.1:'+str(port)+'/shop'
        self.http('/health');self.http('/api/me',cookie=self.fixture['session']);self.event('baseline_0.021_ready')
    def scenario(self,temp,managed_origin=True):
        self.setup(temp);initial=self.snapshot()
        if managed_origin:
            self.controller(self.args.intermediate,'prepare','ready');self.http('/api/me',cookie=self.fixture['session'],expected=503);self.compare(initial,cleared=True)
            self.event('real_prepare_0.024_permissions_verified')
            self.controller(self.args.intermediate,'rollback','rolled_back');self.compare(initial,cleared=False);self.http('/api/me',cookie=self.fixture['session'])
            self.event('real_rollback_to_0.021_verified')
            self.controller(self.args.intermediate,'prepare','ready');self.controller(self.args.intermediate,'activate','committed');self.compare(initial,cleared=True)
            self.http('/api/me',cookie=self.fixture['session'],expected=401)
            self.data('sentinel',{'value':secrets.token_hex(24)});managed=self.snapshot();check(managed['digest']!=initial['digest'],'Missing active-volume sentinel')
            self.event('managed_0.024_has_new_data')
        else:managed=initial
        self.controller(self.args.source,'prepare','ready');self.compare(managed,cleared=True);self.http('/api/catalog',expected=503)
        self.controller(self.args.source,'rollback','rolled_back');self.compare(managed,cleared=managed_origin)
        self.event('managed_prepare_and_rollback_verified' if managed_origin else 'direct_prepare_and_rollback_verified')
        self.controller(self.args.source,'prepare','ready');self.controller(self.args.source,'activate','committed');self.compare(managed,cleared=True)
        self.event('target_activated_data_preserved')
        login,headers=self.http('/api/auth/login',body={'email':'fixture1@example.invalid','password':self.fixture['password']})
        cookies=SimpleCookie();cookies.load(headers['Set-Cookie']);token=cookies['session'].value;self.secrets.append(token)
        check(bool(cookies['session']['secure']) and bool(cookies['session']['httponly']) and cookies['session']['path']=='/shop/','Secure cookie attributes mismatch')
        config,_=self.http('/api/subscriptions/'+self.fixture['subscription']+'/config',cookie=token)
        check(json.loads(config)['config']==self.fixture['config'],'Private profile decryption failed')
        self.event('https_login_and_profile_decryption_verified')
        cid=self.container();env={**os.environ,'PUSHOK_BACKUP_PASSWORD':self.password};base=['docker','exec','-i','-e','PUSHOK_BACKUP_PASSWORD',cid,'python','scripts/backup.py']
        self.json('create backup',base+['create','--env-stdin','--output','/tmp/final.pvb'],input=(self.root/'.env').read_bytes(),env=env)
        self.json('verify backup',base+['verify','/tmp/final.pvb'],env=env)
        self.json('restore backup',base+['restore','/tmp/final.pvb','--target','/tmp/final-restore'],env=env)
        restored=self.data('snapshot',database='/tmp/final-restore/shop.db');check(restored['digest']==managed['digest'] and restored['counts']['sessions']==0,'Backup restore mismatch')
        self.run('refuse overwrite',base+['restore','/tmp/final.pvb','--target','/tmp/final-restore'],env=env,ok=(1,))
        diagnostic=self.json('status CLI',['docker','exec',cid,'python','scripts/status.py','--url',self.backend_url],ok=(0,1))
        check(diagnostic['scope']['external_requests'] is False and diagnostic['release']==version(self.args.source),'Diagnostic scope/version mismatch')
        self.event('backup_restore_and_local_status_verified')
        before_probe=self.snapshot()
        probe=self.json('panel CLI safe failure',['docker','exec',cid,'python','scripts/panel_check.py','--server','fixture-xui'],ok=(1,))
        check(probe['status']=='needs_attention' and probe['api_mode']=='legacy' and probe['error_code']=='PanelConnectionFailed','Panel CLI failure classification mismatch')
        after_probe=self.snapshot()
        check(before_probe['digest']==after_probe['digest'] and before_probe['counts']==after_probe['counts'],'Panel CLI changed application records')
        self.event('packaged_panel_cli_safe_failure_and_no_database_writes_verified')
        _,headers=self.http('/api/auth/login',body={'email':'fixture1@example.invalid','password':self.fixture['password']})
        cookies=SimpleCookie();cookies.load(headers['Set-Cookie']);second=cookies['session'].value;self.secrets.append(second)
        ticket=secrets.token_urlsafe(32);new_password=secrets.token_urlsafe(32);self.secrets.extend([ticket,new_password])
        self.data('ticket',{'ticket':ticket})
        _,headers=self.http('/api/auth/password',cookie=token,body={'current_password':self.fixture['password'],'new_password':new_password})
        cookies=SimpleCookie();cookies.load(headers['Set-Cookie']);fresh=cookies['session'].value;self.secrets.append(fresh)
        check(fresh not in (token,second) and bool(cookies['session']['secure']) and bool(cookies['session']['httponly']) and cookies['session']['path']=='/shop/','Password cookie rotation failed')
        for old in (token,second):self.http('/api/me',cookie=old,expected=401)
        self.http('/login?ticket='+ticket,expected=401)
        self.http('/api/auth/login',body={'email':'fixture1@example.invalid','password':self.fixture['password']},expected=401)
        self.http('/api/auth/login',body={'email':'fixture1@example.invalid','password':new_password})
        config,_=self.http('/api/subscriptions/'+self.fixture['subscription']+'/config',cookie=fresh)
        check(json.loads(config)['config']==self.fixture['config'],'Password change affected VPN profile')
        changed=self.snapshot()
        check(changed['business_digest']==managed['business_digest'] and changed['digest']!=managed['digest'],'Password change affected unrelated data')
        self.event('https_password_rotation_revocation_and_business_data_verified')
        return {'status':'passed','scenario':('0.021 -> 0.024 -> ' if managed_origin else '0.021 -> ')+version(self.args.source),'events':self.events,'data_digest':managed['digest'],
                'real_docker':True,'intermediate_fixture_engine':'candidate','real_loopback_https':True,'real_provider_payment':False,'real_vpn_traffic':False}
    def cleanup(self):
        if self.proxy:self.proxy.shutdown();self.proxy.server_close()
        if not self.root or not self.project:return
        volumes=volume_names(self.root,self.project)
        try:
            ids=self.run('cleanup inventory',['docker','ps','-aq','--no-trunc'])[0].split()
            for cid in ids:
                if cid in self.initial_containers:continue
                info=self.json('inspect cleanup',['docker','inspect',cid])[0]
                if owned_container(info,self.project,volumes,self.initial_containers):self.run('remove fixture container',['docker','rm','-f',cid],timeout=30)
            existing=set(self.run('find fixture volumes',['docker','volume','ls','-q'])[0].split())
            for name in sorted((volumes-self.initial_volumes)&existing):
                if name in existing:self.run('remove fixture volume',['docker','volume','rm',name],timeout=30)
            tags={self.project+'-shop:latest'}
            tags.update('pushok-upgrade:'+p.name for p in (self.root/'upgrades').glob('*') if re.fullmatch(r'[a-f0-9]{24}',p.name))
            current=set(self.run('image cleanup inventory',['docker','image','ls','--format','{{.Repository}}:{{.Tag}}'])[0].split())
            for tag in (tags-self.initial_tags)&current:self.run('remove fixture image',['docker','image','rm',tag],timeout=30)
            remaining=set(self.run('verify volume cleanup',['docker','volume','ls','-q'])[0].split())
            check(not ((volumes-self.initial_volumes)&remaining),'Fixture volumes remain after cleanup')
        except Exception as error:
            self.cleanup_ok=False;print(json.dumps({'cleanup_error':self.scrub(str(error))}),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description='Disposable-host Docker staging; never run on a production VPS')
    p.add_argument('--disposable',action='store_true');p.add_argument('--baseline',type=Path,required=True);p.add_argument('--intermediate',type=Path,required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    args=p.parse_args(argv)
    check(args.disposable and os.geteuid()==0,'An explicitly disposable root runner is required')
    for name in ('baseline','intermediate','source'):setattr(args,name,getattr(args,name).resolve())
    check(version(args.baseline)=='v0.021' and version(args.intermediate)=='v0.024' and bool(re.fullmatch(r'v[0-9]+\.[0-9]{3}',version(args.source))) and version(args.source)>'v0.024','Unexpected fixture source versions')
    check(not args.report.exists() and not args.report.is_symlink(),'Report output already exists')
    for executable in ('docker','openssl'):check(shutil.which(executable) is not None,'Missing staging dependency: '+executable)
    results=[];code=0
    for managed_origin in (False,True):
        stage=Staging(args);result={'status':'failed','real_docker':True}
        with tempfile.TemporaryDirectory(prefix='pushok-docker-stage-') as temp:
            try:
                stage.run('Docker',['docker','version']);stage.run('Compose',['docker','compose','version'])
                result=stage.scenario(Path(temp),managed_origin)
            except Exception as error:
                result.update(events=stage.events,error=stage.scrub(str(error)));print(json.dumps({'staging_error':result['error']}),flush=True)
            finally:stage.cleanup()
        result['cleanup_ok']=stage.cleanup_ok
        if not stage.cleanup_ok:result['status']='failed'
        results.append(result)
        if result['status']!='passed':code=1;break
    result={'status':'passed' if code==0 else 'failed','scenarios':results}
    fd=os.open(args.report,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(result,stream,indent=2)
    print(json.dumps(result),flush=True);return code

if __name__=='__main__':
    try:raise SystemExit(main())
    except StageError as error:print(str(error),file=sys.stderr);raise SystemExit(2)
