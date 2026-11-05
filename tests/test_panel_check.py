import asyncio
import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import ssl
import stat
import threading
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

spec=importlib.util.spec_from_file_location('panel_check',Path(__file__).parents[1]/'scripts/panel_check.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

def profile():return {'url':'https://panel.example/private-path','api':'legacy','username':'fixture','password':'private-password',
                      'inbound_id':7,'link_template':'vless://{uuid}@vpn.example:443'}

def factory(handler):
    return lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler),**kw)

def test_legacy_check_only_authenticates_and_reads_without_leaking_records():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'success':True} if request.method=='POST' else {'success':True,'obj':{'id':7,'protocol':'vless','settings':{'clients':[{'id':'private-client-id','email':'private@example.invalid'}]}}})
    report=asyncio.run(s.probe(profile(),client_factory=factory(handler)))
    assert report['status']=='api_checks_ok' and all(report['checks'].values())
    assert [(r.method,r.url.path.rsplit('/',1)[-1]) for r in calls]==[('POST','login'),('GET','7')]
    assert not any(value in json.dumps(report) for value in ('private-password','private-client-id','private@example.invalid','panel.example','private-path'))
    assert all(value is False for value in report['scope'].values())

def test_clients_mode_uses_bearer_and_never_posts_client_write():
    calls=[];data=profile();data.pop('username');data.pop('password');data.update(api='clients',token='private-token-'+'t'*40)
    def handler(request):
        calls.append(request);assert request.headers['authorization']=='Bearer '+data['token']
        if '/inbounds/' in request.url.path:return httpx.Response(200,json={'success':True,'obj':{'id':7,'protocol':'vless'}})
        return httpx.Response(200,json={'success':True,'obj':{'items':[],'filtered':0}})
    report=asyncio.run(s.probe(data,client_factory=factory(handler)))
    assert report['status']=='api_checks_ok' and len(calls)==2 and all(r.method=='GET' for r in calls)

@pytest.mark.parametrize('status,code',[(401,'AuthenticationRejected'),(403,'AuthenticationRejected'),(302,'RedirectRefused'),(500,'PanelHTTPError')])
def test_remote_failures_return_only_fixed_codes(status,code):
    def handler(request):return httpx.Response(status,json={'secret':'private-password'},headers={'Location':'https://credential-recipient.example'})
    report=asyncio.run(s.probe(profile(),client_factory=factory(handler)))
    assert report['status']=='needs_attention' and report['error_code']==code
    assert 'private-password' not in json.dumps(report)

@pytest.mark.parametrize('inbound',[{'protocol':'vless','settings':{'clients':[]}}, {'id':8,'protocol':'vless','settings':{'clients':[]}}, {'id':7,'protocol':'vless','settings':{}}, {'id':7,'protocol':'other'}])
def test_unknown_or_unconfirmed_inbound_is_not_ready(inbound):
    def handler(request):return httpx.Response(200,json={'success':True} if request.method=='POST' else {'success':True,'obj':inbound})
    report=asyncio.run(s.probe(profile(),client_factory=factory(handler)))
    assert report['status']=='needs_attention'

def test_timeout_does_not_print_url_or_secret():
    def handler(request):raise httpx.ReadTimeout('private-password https://private.example',request=request)
    report=asyncio.run(s.probe(profile(),client_factory=factory(handler)))
    assert report['error_code']=='PanelTimeout' and 'private-password' not in json.dumps(report)

def test_input_requires_private_regular_owned_file_and_size_bound(tmp_path):
    path=tmp_path/'profile';path.write_text(json.dumps(profile()));path.chmod(0o644)
    with pytest.raises(s.InputError):s.read_profile(path)
    path.chmod(0o600);assert s.read_profile(path)==profile()
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):s.read_profile(link)
    path.write_text('x'*(s.LIMIT+1))
    with pytest.raises(s.InputError):s.read_profile(path)

def test_report_refuses_overwrite_before_any_network_and_hides_input(tmp_path,monkeypatch,capsys):
    path=tmp_path/'profile';path.write_text(json.dumps(profile()));path.chmod(0o600)
    output=tmp_path/'report';output.write_text('keep')
    monkeypatch.setattr(s,'probe',lambda *a:pytest.fail('Network must not run'))
    assert s.main(['--profile-file',str(path),'--output',str(output)])==2
    assert output.read_text()=='keep' and 'private-password' not in capsys.readouterr().out

def test_report_file_is_private_and_no_raw_exception_is_printed(tmp_path,monkeypatch,capsys):
    path=tmp_path/'profile';path.write_text('private-password invalid-json');path.chmod(0o600)
    output=tmp_path/'report'
    assert s.main(['--profile-file',str(path),'--output',str(output)])==2
    assert stat.S_IMODE(output.stat().st_mode)==0o600
    assert json.loads(output.read_text())['error_code']=='LocalInputRejected'
    assert 'private-password' not in capsys.readouterr().out

def test_real_https_requires_trusted_ca_and_preserves_strict_verification(tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.datetime.now(datetime.timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]),critical=False).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256())
    pem=tmp_path/'ca.pem';pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private=tmp_path/'key.pem';private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append(('POST',self.path));self.rfile.read(int(self.headers.get('Content-Length','0')));self.answer({'success':True})
        def do_GET(self):
            calls.append(('GET',self.path));self.answer({'success':True,'obj':{'id':7,'protocol':'vless','settings':{'clients':[]}}})
        def answer(self,body):
            raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(pem,private);server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        data=profile();data['url']='https://localhost:'+str(server.server_port)
        rejected=asyncio.run(s.probe(data));assert rejected['status']=='needs_attention' and calls==[]
        accepted=asyncio.run(s.probe(data,str(pem)));assert accepted['status']=='api_checks_ok'
        assert [method for method,path in calls]==['POST','GET']
    finally:server.shutdown();server.server_close();thread.join(timeout=3)

def test_registered_profile_is_read_only_and_missing_database_is_not_created(tmp_path,monkeypatch):
    from app import config as c
    from cryptography.fernet import Fernet
    import sqlite3
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    path=tmp_path/'registry.db';monkeypatch.setattr(c,'DB',path)
    with pytest.raises(sqlite3.OperationalError):s.registered_profile('node')
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE servers(id TEXT,protocol TEXT,settings TEXT)')
        conn.execute('INSERT INTO servers VALUES (?,?,?)',('node','xui',c.encrypt(json.dumps(profile()))))
    before=path.read_bytes()
    assert s.registered_profile('node')==profile()
    assert path.read_bytes()==before
    with pytest.raises(s.InputError):s.registered_profile('unknown')

def test_invalid_ca_refuses_before_network(tmp_path):
    path=tmp_path/'invalid.pem';path.write_text('private-password not a certificate')
    def fail(**kwargs):pytest.fail('Network must not run')
    report=asyncio.run(s.probe(profile(),str(path),client_factory=fail))
    assert report['status']=='invalid_input' and report['error_code']=='InvalidCAFile'
    assert 'private-password' not in json.dumps(report)

def test_stdin_size_limit_and_fifo_refusal(tmp_path):
    import io,os
    assert s.read_profile(stdin=io.BytesIO(json.dumps(profile()).encode()))==profile()
    with pytest.raises(s.InputError):s.read_profile(stdin=io.BytesIO(b'x'*(s.LIMIT+1)))
    fifo=tmp_path/'fifo';os.mkfifo(fifo,mode=0o600)
    with pytest.raises(s.InputError):s.read_profile(fifo)

def test_invalid_arguments_do_not_echo_accidentally_supplied_secret(capsys):
    with pytest.raises(SystemExit) as error:s.main(['--profile-stdin','--password','private-password'])
    captured=capsys.readouterr()
    assert error.value.code==2 and 'private-password' not in captured.out+captured.err
    assert json.loads(captured.out)['error_code']=='InvalidArguments'
