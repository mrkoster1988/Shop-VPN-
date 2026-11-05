"""Fetch the aggregate admin report from loopback only; never prints response errors."""
import argparse
import json
import os
import re
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def fetch_report(url,token):
    parsed=urlsplit(url)
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*',parsed.path.rstrip('/'))):raise ValueError('Нужен HTTP loopback URL магазина, например http://127.0.0.1:8000/shop')
    if len(token)<32:raise ValueError('Нужен ADMIN_TOKEN или файл токена с правами 0600')
    opener=build_opener(ProxyHandler({}),NoRedirect())
    request=Request(url.rstrip('/')+'/api/admin/diagnostics',headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
    with opener.open(request,timeout=10) as response:raw=response.read(262145)
    if len(raw)>262144:raise ValueError('Отчёт превышает лимит размера')
    report=json.loads(raw)
    if not isinstance(report,dict) or report.get('format')!=1 or report.get('status') not in ('local_checks_ok','needs_attention'):raise ValueError('Неизвестный формат отчёта')
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description='Локальный отчёт магазина без проверок провайдеров')
    p.add_argument('--url',default='http://127.0.0.1:8000'+urlsplit(os.getenv('PUBLIC_URL','')).path.rstrip('/'));p.add_argument('--token-file')
    args=p.parse_args(argv);token=os.getenv('ADMIN_TOKEN','')
    if args.token_file:
        fd=os.open(args.token_file,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError('Файл токена должен иметь права 0600')
            token=stream.read(4097).rstrip('\r\n')
            if len(token)>4096:raise ValueError('Некорректный файл токена')
    report=fetch_report(args.url,token);print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='local_checks_ok' else 1

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('Не удалось получить локальный отчёт. Проверьте адрес, токен, права и режим обслуживания; ответ сервера не выводится.',file=sys.stderr);raise SystemExit(2)
