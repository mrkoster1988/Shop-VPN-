"""Container-only helpers for the disposable WireGuard data-plane acceptance job."""
import configparser
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler


def run(*args, input=None):
    return subprocess.run(args,input=input,text=True,capture_output=True,check=True,timeout=10).stdout.strip()


def server():
    folder=Path(os.environ['WG_DATA_DIR']);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    key=folder/'server.key'
    if not key.exists():
        fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:output.write(run('wg','genkey')+'\n')
    run('ip','link','add','wg-pushok','type','wireguard')
    run('wg','set','wg-pushok','private-key',str(key),'listen-port','51820')
    run('ip','address','add','10.77.0.1/24','dev','wg-pushok')
    run('ip','link','set','wg-pushok','up')
    route=json.loads(run('ip','-j','route','get',os.environ['SINK_IP']))[0]
    interface=route['dev']
    run('iptables','-A','FORWARD','-i','wg-pushok','-o',interface,'-j','ACCEPT')
    run('iptables','-A','FORWARD','-i',interface,'-o','wg-pushok','-m','conntrack','--ctstate','RELATED,ESTABLISHED','-j','ACCEPT')
    run('iptables','-t','nat','-A','POSTROUTING','-s','10.77.0.0/24','-o',interface,'-j','MASQUERADE')
    os.execvp('python',['python','-m','uvicorn','agent.main:app','--host','0.0.0.0','--port','8787','--workers','1','--no-access-log'])


def configure_client(data):
    config=configparser.ConfigParser();config.read_string(data['config'])
    address=ipaddress.ip_interface(config['Interface']['Address'])
    if address.ip not in ipaddress.ip_network('10.77.0.0/24'):raise ValueError('Unexpected fixture address')
    target=str(ipaddress.ip_address(data['sink']))
    # wg-quick's DNS/default-route host integration is deliberately outside this isolated test.
    wire='[Interface]\nPrivateKey = '+config['Interface']['PrivateKey']+'\n\n[Peer]\n'
    for field in ('PublicKey','Endpoint','AllowedIPs','PersistentKeepalive'):wire+=field+' = '+config['Peer'][field]+'\n'
    if subprocess.run(['ip','link','show','wg-client'],capture_output=True).returncode==0:run('ip','link','del','wg-client')
    run('ip','link','add','wg-client','type','wireguard')
    fd=os.open('/tmp/client.conf',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as output:output.write(wire)
        run('wg','setconf','wg-client','/tmp/client.conf')
    finally:Path('/tmp/client.conf').unlink(missing_ok=True)
    run('ip','address','add',str(address),'dev','wg-client')
    run('ip','link','set','wg-client','mtu','1280','up')
    run('ip','route','replace',target+'/32','dev','wg-client')
    return {'configured':True,'address':str(address.ip)}


def peers():
    interface='wg-pushok'
    keys=run('wg','show',interface,'peers').split()
    handshakes=dict(line.split() for line in run('wg','show',interface,'latest-handshakes').splitlines())
    transfers={parts[0]:[int(x) for x in parts[1:]] for line in run('wg','show',interface,'transfer').splitlines() if (parts:=line.split())}
    return {'count':len(keys),'handshake':all(int(handshakes.get(key,0))>0 for key in keys) and bool(keys),
            'received':sum(transfers[key][0] for key in keys),'sent':sum(transfers[key][1] for key in keys)}


def probe(data):
    try:route=json.loads(run('ip','-j','route','get',data['sink']))[0]
    except subprocess.CalledProcessError:return {'reachable':False,'interface':None}
    try:
        with build_opener(ProxyHandler({})).open('http://'+str(ipaddress.ip_address(data['sink']))+':8080/',timeout=2) as response:
            body=json.load(response)
        return {'reachable':True,'source':body['source'],'nonce':body['nonce'],'interface':route['dev']}
    except Exception:return {'reachable':False,'interface':route['dev']}


def sink():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            body=json.dumps({'source':self.client_address[0],'nonce':os.environ['SINK_NONCE']}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    HTTPServer(('0.0.0.0',8080),Handler).serve_forever()


def main():
    command=sys.argv[1]
    if command=='server':server();return
    if command=='sink':sink();return
    if command=='configure':result=configure_client(json.load(sys.stdin))
    elif command=='peers':result=peers()
    elif command=='probe':result=probe(json.load(sys.stdin))
    elif command=='expire':
        # Only the disposable shop container calls this controlled clock shortcut.
        from app.db import execute
        data=json.load(sys.stdin)
        execute('UPDATE subscriptions SET expires=? WHERE id=?',(int(time.time())-1,data['subscription']))
        result={'expired':True}
    else:raise ValueError('Unknown fixture operation')
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception as error:
        # Never print subprocess args, WG output, config lines or credential-bearing errors.
        print(json.dumps({'fixture_error':type(error).__name__}),file=sys.stderr);raise SystemExit(1)
