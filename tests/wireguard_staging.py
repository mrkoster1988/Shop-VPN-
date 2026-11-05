"""Real WireGuard CI acceptance. Requires an explicitly disposable Linux Docker host."""
import argparse
import base64
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

LABEL='pushok.wireguard-staging'

class StageError(RuntimeError):pass

def check(condition,message):
    if not condition:raise StageError(message)

def owned(info,run_id,initial):
    return info['Id'] not in initial and (info.get('Labels') or info.get('Config',{}).get('Labels') or {}).get(LABEL)==run_id

class Staging:
    def __init__(self,args):
        self.args=args;self.run_id=secrets.token_hex(12);self.label=LABEL+'='+self.run_id
        self.containers=[];self.networks=[];self.volumes=[];self.images=[];self.events=[];self.cleanup_ok=True
        self.initial=set();self.cookie=None;self.base=None
        self.admin=secrets.token_urlsafe(40);self.agent_token=secrets.token_urlsafe(40)
        self.opener=build_opener(ProxyHandler({}))
        self.helper=(args.source/'tests/wireguard/fixture.py').read_text()
    def event(self,name):
        self.events.append(name);print(json.dumps({'wireguard_stage':name}),flush=True)
    def run(self,label,args,*,input=None,env=None,ok=(0,),timeout=600):
        try:result=subprocess.run(args,input=input,capture_output=True,env=env,timeout=timeout)
        except subprocess.TimeoutExpired:raise StageError(label+': timeout') from None
        # Intentionally omit output and command arguments: either may contain a private key.
        if result.returncode not in ok:raise StageError(label+': exit '+str(result.returncode))
        return result.stdout.decode().strip()
    def docker_json(self,*args):return json.loads(self.run('Docker metadata',['docker',*args]))
    def inspect(self,kind,identifier):return self.docker_json(kind,'inspect',identifier)[0]
    def ip(self,container,network):return self.inspect('container',container)['NetworkSettings']['Networks'][network]['IPAddress']
    def helper_call(self,container,command,payload=None):
        return json.loads(self.run('fixture '+command,['docker','exec','-i',container,'python','-c',self.helper,command],input=json.dumps(payload).encode() if payload is not None else None,timeout=20))
    def create_network(self,role):
        name=self.run_id+'-'+role
        identifier=self.run('create isolated network',['docker','network','create','--internal','--label',self.label,name]);self.networks.append(identifier);return name
    def create_volume(self,role):
        name=self.run('create fixture volume',['docker','volume','create','--label',self.label,self.run_id+'-'+role]);self.volumes.append(name);return name
    def start(self,role,image,network,command,*,env=None,volume=None,net_admin=False):
        args=['docker','run','-d','--name',self.run_id+'-'+role,'--label',self.label,'--network',network,'--cap-drop','ALL','--security-opt','no-new-privileges:true']
        if net_admin:args+=['--cap-add','NET_ADMIN']
        if role=='server':args+=['--sysctl','net.ipv4.ip_forward=1']
        if volume:args+=['--mount','type=volume,source='+volume+',target='+('/app/data' if role=='shop' else '/var/lib/pushok-wg')]
        args+=['--tmpfs','/tmp:rw,noexec,nosuid,size=16m']
        if role=='shop':args+=['--read-only']
        environment=os.environ.copy()
        for key,value in (env or {}).items():args+=['-e',key];environment[key]=value
        identifier=self.run('start '+role,args+[image]+command,env=environment)
        self.containers.append(identifier);return identifier
    def api(self,path,body=None,*,admin=False,expected=200,method=None,raw=False,authenticated=True):
        headers={}
        if authenticated and self.cookie:headers['Cookie']='session='+self.cookie
        if admin:headers['Authorization']='Bearer '+self.admin
        if body is not None:headers.update({'Content-Type':'application/json','Origin':self.origin})
        request=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
        try:response=self.opener.open(request,timeout=5)
        except HTTPError as error:response=error
        with response:
            data=response.read(1048576);status=response.status
            if response.headers.get('Set-Cookie'):
                cookies=SimpleCookie();cookies.load(response.headers['Set-Cookie']);self.cookie=cookies['session'].value
        check(status==expected,'Unexpected API response: '+str(status))
        return data.decode() if raw else json.loads(data)
    def until(self,label,callback,timeout=70):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                result=callback()
                if result:return result
            except (URLError,ConnectionError,TimeoutError):pass
            time.sleep(1)
        raise StageError(label+': timeout')
    def probe(self,client,sink):return self.helper_call(client,'probe',{'sink':sink})
    def scenario(self,temp):
        for kind,args in [('container',['ps','-aq','--no-trunc']),('network',['network','ls','-q','--no-trunc']),('image',['image','ls','-q','--no-trunc'])]:
            self.initial.update(self.run('inventory '+kind,['docker',*args]).split())
        self.initial.update(self.run('inventory volumes',['docker','volume','ls','-q']).split())
        context=temp/'fixture';context.mkdir()
        # Explicit allowlist: no developer .env, databases or other workspace files enter the image.
        for name in ('requirements.txt',):shutil.copy2(self.args.source/name,context/name)
        shutil.copytree(self.args.source/'agent',context/'agent',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(self.args.source/'tests/wireguard/fixture.py',context/'fixture.py')
        shutil.copy2(self.args.source/'tests/wireguard/Dockerfile',context/'Dockerfile')
        fixture_image='pushok-wg-stage:'+self.run_id;shop_image='pushok-wg-shop:'+self.run_id
        for image,source in ((fixture_image,context),(shop_image,self.args.source)):
            self.images.append(image)
            self.run('build fixture' if image==fixture_image else 'build production shop',['docker','build','--label',self.label,'-t',image,str(source)],timeout=900)
        self.event('production_shop_and_agent_images_built')
        transport=self.create_network('transport');egress=self.create_network('egress')
        agent_volume=self.create_volume('agent');shop_volume=self.create_volume('shop')
        nonce=secrets.token_urlsafe(24)
        sink=self.start('sink',fixture_image,egress,['python','fixture.py','sink'],env={'SINK_NONCE':nonce})
        sink_ip=self.ip(sink,egress)
        server=self.start('server',fixture_image,transport,['python','-c','import time; time.sleep(3600)'],net_admin=True,volume=agent_volume)
        self.run('attach egress',['docker','network','connect',egress,server])
        server_ip=self.ip(server,transport);server_egress=self.ip(server,egress)
        agent_env={'WG_DATA_DIR':'/var/lib/pushok-wg','WG_AGENT_TOKEN':self.agent_token,'WG_ENCRYPTION_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                   'WG_ENDPOINT':server_ip+':51820','SINK_IP':sink_ip}
        # A dedicated bootstrap exec creates the interface in the container's network namespace.
        environment=os.environ.copy();args=['docker','exec','-d']
        for key,value in agent_env.items():args+=['-e',key];environment[key]=value
        self.run('start real WireGuard agent',args+[server,'python','fixture.py','server'],env=environment)
        self.until('agent startup',lambda:self.agent_health(server_ip))
        check(self.helper_call(server,'peers')['count']==0,'Unexpected initial peer')
        client=self.start('client',fixture_image,transport,['python','-c','import time; time.sleep(3600)'],net_admin=True)
        check(not self.probe(client,sink_ip)['reachable'],'Negative control reached sink without tunnel')
        self.event('isolated_kernel_wireguard_and_negative_control_verified')
        shop=self.start('shop',shop_image,transport,['python','-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8000','--workers','1','--no-access-log'],volume=shop_volume,
            env={'DATA_DIR':'/app/data','PUBLIC_URL':'http://shop:8000/shop','ADMIN_TOKEN':self.admin,'ENCRYPTION_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                 'PAYMENT_MODE':'manual','BACKGROUND_TASKS':'true','BOT_TOKEN':''})
        shop_ip=self.ip(shop,transport);self.base='http://'+shop_ip+':8000/shop'
        # Origin follows PUBLIC_URL while the request uses the same isolated container directly.
        self.origin='http://shop:8000'
        self.until('shop startup',lambda:self.api('/health'))
        self.api('/api/auth/register',{'email':'wireguard@example.invalid','password':secrets.token_urlsafe(32)})
        created=self.api('/api/admin/servers',{'name':'Isolated WireGuard','protocol':'wg','settings':{'url':'http://'+server_ip+':8787','token':self.agent_token}},admin=True,expected=201)
        sid=created['id'];checked=self.api('/api/admin/servers/'+sid+'/check',{},admin=True)
        check(checked['health']=='ok','Real agent health check failed')
        self.api('/api/admin/servers/'+sid,{'name':created['name'],'state':'active','version':checked['version']},admin=True,method='PUT')
        purchase=self.api('/api/orders',{'plan':'month','protocol':'wg'})
        check(purchase['status']=='pending' and self.helper_call(server,'peers')['count']==0,'VPN issued before payment confirmation')
        for _ in range(2):self.api('/api/admin/orders/'+purchase['id']+'/confirm',{},admin=True)
        self.until('paid order fulfillment',lambda:(value if (value:=self.api('/api/orders/'+purchase['id']))['status']=='active' else None))
        subscription=self.api('/api/me')['subscriptions'][0];sub_id=subscription['id']
        config=self.api('/api/subscriptions/'+sub_id+'/download',raw=True)
        check(config==self.api('/api/subscriptions/'+sub_id+'/config')['config'],'Download/config mismatch')
        self.api('/api/subscriptions/'+sub_id+'/download',expected=401,raw=True,authenticated=False)
        check(self.helper_call(server,'peers')['count']==1,'Repeated confirmation created duplicate peer')
        self.helper_call(client,'configure',{'config':config,'sink':sink_ip})
        self.until('encrypted traffic',lambda:self.traffic(client,sink_ip,server_egress,nonce))
        stats=self.helper_call(server,'peers')
        check(stats['handshake'] and stats['received']>0 and stats['sent']>0,'Missing kernel handshake/traffic counters')
        self.event('manual_order_real_fulfillment_download_handshake_and_ipv4_nat_verified')
        # A renewal must retain the existing UUID, private key, address and server binding.
        renewal=self.api('/api/orders',{'plan':'month','protocol':'wg'})
        for _ in range(2):self.api('/api/admin/orders/'+renewal['id']+'/confirm',{},admin=True)
        self.until('renewal fulfillment',lambda:self.api('/api/orders/'+renewal['id'])['status']=='active')
        renewed=self.api('/api/me')['subscriptions'][0]
        check(renewed['id']==sub_id and renewed['expires']==subscription['expires']+30*86400,'Renewal identity/expiry mismatch')
        check(config==self.api('/api/subscriptions/'+sub_id+'/download',raw=True) and self.helper_call(server,'peers')['count']==1,'Renewal changed key/address or duplicated peer')
        self.until('renewed traffic',lambda:self.traffic(client,sink_ip,server_egress,nonce))
        self.event('renewal_keeps_single_peer_profile_and_paid_days')
        self.run('restart shop',['docker','restart',shop]);self.until('shop restart',lambda:self.api('/health'))
        check(config==self.api('/api/subscriptions/'+sub_id+'/download',raw=True),'Shop restart changed encrypted profile')
        self.event('shop_restart_preserves_profile_and_session')
        self.helper_call(shop,'expire',{'subscription':sub_id})
        self.until('automatic expiry revoke',lambda:self.api('/api/me')['subscriptions'][0]['state']=='expired')
        check(self.helper_call(server,'peers')['count']==0,'Expired peer remains in kernel')
        self.api('/api/subscriptions/'+sub_id+'/download',expected=404,raw=True)
        check(not self.probe(client,sink_ip)['reachable'],'Revoked peer can still pass traffic')
        self.event('worker_expiry_removes_kernel_peer_and_blocks_new_traffic')
        return {'status':'passed','events':self.events,'real_wireguard_kernel':True,'real_handshake':True,'controlled_ipv4_nat':True,
                'real_public_internet':False,'real_external_payment':False,'public_tls_acme':False,'clean_vps_install':False}
    def agent_health(self,server_ip):
        request=Request('http://'+server_ip+':8787/health',headers={'Authorization':'Bearer '+self.agent_token})
        with self.opener.open(request,timeout=3) as response:return json.load(response).get('status')=='ok'
    def traffic(self,client,sink,source,nonce):
        result=self.probe(client,sink)
        if not result['reachable']:return False
        check(result['interface']=='wg-client' and result['source']==source and result['nonce']==nonce,'Traffic bypassed expected WireGuard/NAT path')
        return True
    def cleanup(self):
        for kind,identifiers,verb in [('container',self.containers,'rm'),('network',self.networks,'rm'),('volume',self.volumes,'rm'),('image',self.images,'rm')]:
            for identifier in reversed(identifiers):
                try:
                    raw=self.run('inspect cleanup',['docker',kind,'inspect',identifier],ok=(0,1))
                    if not raw or raw=='[]':continue
                    info=json.loads(raw)[0]
                    if kind=='volume':info={**info,'Id':info['Name']}
                    check(owned(info,self.run_id,self.initial),'Cleanup ownership mismatch')
                    args=['docker',kind,verb]+(['-f'] if kind=='container' else [])+[identifier]
                    self.run('remove owned '+kind,args,timeout=40)
                except Exception:self.cleanup_ok=False


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable',action='store_true');parser.add_argument('--source',type=Path,required=True);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args(argv)
    check(args.disposable and sys.platform=='linux' and os.geteuid()==0,'Use --disposable on a dedicated Linux root runner only')
    check(not args.report.exists() and not args.report.is_symlink(),'Report already exists')
    args.source=args.source.resolve()
    stage=Staging(args);result={'status':'failed'}
    with tempfile.TemporaryDirectory(prefix='pushok-wg-stage-') as folder:
        try:result=stage.scenario(Path(folder))
        except Exception as error:
            result={'status':'failed','events':stage.events,'error':str(error) if isinstance(error,StageError) else type(error).__name__}
        finally:stage.cleanup()
    result['cleanup_ok']=stage.cleanup_ok
    if not stage.cleanup_ok:result['status']='failed'
    fd=os.open(args.report,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:json.dump(result,output,indent=2)
    print(json.dumps(result),flush=True)
    return 0 if result['status']=='passed' else 1

if __name__=='__main__':
    try:raise SystemExit(main())
    except StageError as error:print(str(error),file=sys.stderr);raise SystemExit(2)
