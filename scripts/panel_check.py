"""Проверяет API панелей без создания или изменения клиентских записей."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import ssl
import stat
import sys

# Allow both `python scripts/panel_check.py` and execution inside the shop image.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
LIMIT=16384

class InputError(ValueError):pass


def read_profile(path=None,stdin=None):
    if stdin is not None:
        raw=stdin.read(LIMIT+1)
    else:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.geteuid():
                raise InputError('PrivateFileRequired')
            raw=stream.read(LIMIT+1)
    if len(raw)>LIMIT:raise InputError('ProfileTooLarge')
    data=json.loads(raw)
    if not isinstance(data,dict):raise InputError('InvalidProfile')
    return data


def registered_profile(server_id):
    from app import config as c
    with sqlite3.connect(c.DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as conn:
        conn.row_factory=sqlite3.Row
        row=conn.execute('SELECT protocol,settings FROM servers WHERE id=?',(server_id,)).fetchone()
    if not row or row['protocol']!='xui':raise InputError('VLESSServerRequired')
    return json.loads(c.decrypt(row['settings']))


def base_report():
    return {'format':1,'status':'invalid_input','api_mode':None,'not_applicable_checks':[],
            'checks':{'authentication':False,'vless_inbound':False,'supported_schema':False,'inbound_id':False,'user_api':False},
            'scope':{'client_records_modified':False,'real_vpn_traffic':False,'payment_checked':False,'vps_install_checked':False}}


async def probe(profile,ca_file=None,client_factory=None,*,wgdashboard=False,nxpanel=False):
    import httpx
    from app import panel,servers
    report=base_report()
    from app.wgdashboard import CheckProfile, WGDashboard
    from app.nxpanel import CheckProfile as NXProfile, NXPanel, NXCheckError
    if nxpanel and wgdashboard:
        return report | {'error_code':'InvalidPanelMode'}
    if nxpanel:
        report['checks'] = {key:False for key in ('authentication','token_scopes',
            'user_api','template_access','supported_schema')}
        report['scope'].update(provisioning_checked=False,node_checked=False,license_checked=False)
        report['api_mode'] = 'nxpanel'
        report['warnings'] = []
    if wgdashboard:
        report['checks'] = {'wireguard_interface':False,'supported_schema':False}
        report['api_mode'] = 'wgdashboard'
    try:
        profile = (NXProfile.model_validate(profile).model_dump() if nxpanel else
                   CheckProfile.model_validate(profile).model_dump() if wgdashboard else servers.validated('xui',profile))
    except Exception:return report | {'error_code':'InvalidProfile'}
    report['api_mode']='nxpanel' if nxpanel else 'wgdashboard' if wgdashboard else profile.get('api','legacy')
    try:context=ssl.create_default_context(cafile=ca_file)
    except Exception:return report | {'error_code':'InvalidCAFile'}
    try:
        factory=client_factory or httpx.AsyncClient
        # Never send credentials through environment proxies or follow panel redirects.
        async with factory(timeout=20,follow_redirects=False,trust_env=False,verify=context) as client:
            adapter=NXPanel(profile) if nxpanel else WGDashboard(profile) if wgdashboard else panel.adapter(profile)
            result=await adapter.check(client)
        if nxpanel:
            report['checks'].update(result['checks'])
            report['warnings']=result['warnings']
            report['status']='api_checks_ok'
            return report
        if wgdashboard:
            report['checks'].update(result)
            report['status']='api_checks_ok'
            return report
        report['checks']['authentication']=True
        report['checks']['supported_schema']=True
        if profile.get('api') == 'remnawave':
            report['checks']['user_api']=True
            report['not_applicable_checks']=['vless_inbound','inbound_id']
            report['status']='api_checks_ok'
        else:
            report['not_applicable_checks']=['user_api']
            report['checks']['vless_inbound']=True
            identity=result.get('id')
            report['checks']['inbound_id']=type(identity) is int and identity==profile['inbound_id']
            if not report['checks']['inbound_id']:
                report.update(status='needs_attention',error_code='InboundIdentityNotReturned')
            else:report['status']='api_checks_ok'
    except httpx.HTTPStatusError as error:
        status=error.response.status_code
        code='AuthenticationRejected' if status in (401,403) else 'RedirectRefused' if 300<=status<400 else 'PanelHTTPError'
        if nxpanel and status in (402,429):
            code='NXLicenseLimit' if status==402 else 'NXRateLimited'
        report.update(status='needs_attention',error_code=code)
    except NXCheckError as error:
        report.update(status='needs_attention',error_code=str(error))
    except panel.PanelError:
        report.update(status='needs_attention',error_code='UnsupportedPanelResponse')
    except httpx.TimeoutException:
        report.update(status='needs_attention',error_code='PanelTimeout')
    except httpx.TransportError:
        report.update(status='needs_attention',error_code='PanelConnectionFailed')
    except Exception:
        report.update(status='needs_attention',error_code='PanelCheckFailed')
    return report


class SafeParser(argparse.ArgumentParser):
    def error(self,message):
        print(json.dumps(base_report() | {'error_code':'InvalidArguments'}))
        raise SystemExit(2)


def main(argv=None):
    parser=SafeParser(description='Проверка API панели без изменения записей клиентов')
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--server',help='ID VLESS-сервера из реестра текущего магазина')
    source.add_argument('--profile-file',type=Path,help='JSON профиля, владелец текущий пользователь, права 0600')
    source.add_argument('--profile-stdin',action='store_true',help='Читать JSON профиля из stdin, максимум 16 KiB')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--wgdashboard',action='store_true',help='Проверить WGDashboard из JSON профиля без изменения peers')
    mode.add_argument('--nxpanel',action='store_true',help='Проверить NX Panel: токен, чтение пользователей и доступность шаблона')
    parser.add_argument('--ca-file',type=Path,help='Дополнительный локальный PEM CA; TLS-проверка остаётся включена')
    parser.add_argument('--output',type=Path,help='Сохранить JSON 0600 без перезаписи')
    args=parser.parse_args(argv)
    logging.getLogger('httpx').disabled=True;logging.getLogger('httpcore').disabled=True
    report=base_report();fd=None
    try:
        if args.output:fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        if (args.wgdashboard or args.nxpanel) and args.server:raise InputError('ProfileFileRequired')
        profile=registered_profile(args.server) if args.server else read_profile(args.profile_file,sys.stdin.buffer if args.profile_stdin else None)
        kwargs={'wgdashboard':args.wgdashboard}
        if args.nxpanel:kwargs['nxpanel']=True
        report=asyncio.run(probe(profile,str(args.ca_file) if args.ca_file else None,**kwargs))
    except Exception:
        # Validation exceptions can contain passwords/URLs; only fixed local codes may escape.
        report['error_code']='LocalInputRejected'
    if fd is not None:
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(report,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        except Exception:report=base_report() | {'error_code':'LocalReportWriteFailed'}
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='api_checks_ok' else 1 if report['status']=='needs_attention' else 2

if __name__=='__main__':raise SystemExit(main())
