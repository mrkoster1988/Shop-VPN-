"""Read-only diagnostics. Does not create VPN clients, change bot webhooks or print secrets."""
import asyncio
import os
import sys
import sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app import config as c, providers

async def main():
    checks = []
    try:
        c.cipher(); checks.append(('Encryption key',True))
    except Exception:
        checks.append(('Encryption key',False))
    checks.append(('Admin token',len(c.ADMIN_TOKEN)>=32))
    registry = None
    if c.DB.exists():
        try:
            with sqlite3.connect(c.DB.resolve().as_uri() + '?mode=ro', uri=True) as conn:
                conn.row_factory = sqlite3.Row
                initialized = conn.execute("SELECT 1 FROM meta WHERE key='server_registry_initialized'").fetchone()
                if initialized:
                    registry = [dict(row) for row in conn.execute('SELECT * FROM servers')]
        except sqlite3.Error:
            checks.append(('Database registry', False))
            registry = []
    if registry is not None:
        enabled = [row for row in registry if row['state'] in ('active','draining')]
        checks.append(('Registry: enabled servers configured', bool(enabled)))
        for index, row in enumerate(enabled, 1):
            try:
                await providers.probe_server(row)
                checks.append((f'Server #{index}: management API', True))
            except Exception:
                checks.append((f'Server #{index}: management API', False))
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        if c.BOT_TOKEN:
            try:
                r=await client.get('https://api.telegram.org/bot'+c.BOT_TOKEN+'/getMe')
                checks.append(('Telegram token',r.status_code==200 and r.json().get('ok') is True))
                r=await client.get('https://api.telegram.org/bot'+c.BOT_TOKEN+'/getWebhookInfo')
                checks.append(('Telegram polling: webhook empty',not r.json().get('result',{}).get('url')))
            except Exception: checks.append(('Telegram connection',False))
        if registry is None and os.getenv('XUI_URL'):
            try:
                base=os.environ['XUI_URL'].rstrip('/')
                r=await client.post(base+'/login',data={'username':os.getenv('XUI_USERNAME'),'password':os.getenv('XUI_PASSWORD')})
                r.raise_for_status()
                checks.append(('3x-ui login',r.json().get('success') is True))
                r=await client.get(base+'/panel/api/inbounds/get/'+os.getenv('XUI_INBOUND_ID','0'))
                r.raise_for_status(); data=r.json()
                checks.append(('3x-ui legacy API / VLESS inbound',data.get('success') is True and data.get('obj',{}).get('protocol')=='vless'))
                checks.append(('3x-ui link template',os.getenv('XUI_LINK_TEMPLATE','').startswith('vless://{uuid}@')))
            except Exception: checks.append(('3x-ui connection / API',False))
        if registry is None and os.getenv('WG_AGENT_URL'):
            try:
                r=await client.get(os.environ['WG_AGENT_URL'].rstrip('/')+'/health',headers={'Authorization':'Bearer '+os.getenv('WG_AGENT_TOKEN','')})
                checks.append(('WireGuard agent',r.status_code==200))
            except Exception: checks.append(('WireGuard agent',False))
    for name,ok in checks: print(('OK   ' if ok else 'FAIL ')+name)
    return 0 if all(ok for _,ok in checks) else 1

raise SystemExit(asyncio.run(main()))
