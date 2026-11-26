"""Exercise the dashboard switch and rollback on a disposable GitHub runner."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SWITCH = ROOT/'scripts/wg_clock_switch.py'
BACKUP = ROOT/'scripts/backup_wgdashboard_clock.py'
ENTRY = """#!/bin/sh
set -eu
mkdir -p /data/db /etc/wireguard
if [ ! -f /data/wg-dashboard.ini ]; then printf '[WireGuardConfiguration]\\nautostart = wg0\\n' > /data/wg-dashboard.ini; fi
for interface in wg0 wg-guest; do
    file="/etc/wireguard/$interface.conf"
    if [ ! -f "$file" ]; then
        port=51820; address=10.14.0.1/24
        if [ "$interface" = wg-guest ]; then port=51821; address=10.15.0.1/24; fi
        printf '[Interface]\\nPrivateKey = %s\\nAddress = %s\\nListenPort = %s\\n[Peer]\\nPublicKey = %s\\nAllowedIPs = %s\\n' "$(wg genkey)" "$address" "$port" "$(wg genkey | wg pubkey)" "${address%1/24}2/32" > "$file"
    fi
    wg-quick up "$interface"
done
exec python -m http.server 10086 --bind 0.0.0.0
"""


def run(*args, expected=True, data=None):
    p = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if (p.returncode == 0) != expected:
        raise RuntimeError(str(args[:2])+'\n'+p.stdout[-2500:])
    return p.stdout.strip()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.getuid() or sys.argv[1:] != ['--disposable']:
        raise SystemExit('Use only on a disposable CI runner.')
    if run('docker','ps','-aq','--filter','name=^/wgdashboard$'):
        raise SystemExit('A dashboard already exists.')
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        (root/'Dockerfile').write_text('FROM python:3.12-slim\nRUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 iptables tzdata && rm -rf /var/lib/apt/lists/*\nENV TZ=Europe/Amsterdam\nCOPY start /start\nENTRYPOINT ["/bin/sh","/start"]\n')
        (root/'start').write_text(ENTRY)
        run('docker','build','-q','-t','clock-ci',tmp)
        original=run('docker','run','-d','--network','bridge','--name','wgdashboard','--cap-add','NET_ADMIN','--restart','unless-stopped','-p','127.0.0.1:10086:10086','-p','127.0.0.1:51820:51820/udp','clock-ci')
        for _ in range(40):
            ok=subprocess.run(['curl','--noproxy','*','-fsS','--max-time','2','http://127.0.0.1:10086'],capture_output=True).returncode==0
            if ok: break
            time.sleep(.25)
        assert ok
        day=time.strftime('%Y-%m-%d 19:52:55',time.gmtime(time.time()+7*86400))
        run('docker','exec','-i','wgdashboard','python','-',day,data="""import sqlite3,sys
with sqlite3.connect('/data/db/wgdashboard_job.db') as c:
 c.execute('CREATE TABLE PeerJobs(JobID TEXT, Value TEXT, Field TEXT, ExpireDate TEXT, Operator TEXT, Action TEXT)')
 c.execute("INSERT INTO PeerJobs VALUES ('test',?,'date',NULL,'lgt','delete')",(sys.argv[1],))
""")
        expect=hashlib.sha256(f'test {day}\n'.encode()).hexdigest()
        run(sys.executable,str(BACKUP),'--directory',tmp)
        run(sys.executable,str(SWITCH),'stage','0'*64,'--backups',tmp,expected=False)
        assert run('docker','inspect','-f','{{.Id}}','wgdashboard')==original
        for action in ('rollback','finish'):
            out=run(sys.executable,str(SWITCH),'stage',expect,'--backups',tmp);print(out,flush=True)
            folder=Path(out.split('Каталог переключения:',1)[1].splitlines()[0].strip())
            assert (folder/'STAGED').exists() and 'WG_STAGED_OK' in out
            run('curl','--noproxy','*','-fsS','--max-time','3','http://127.0.0.1:10086',expected=False)
            print(run(sys.executable,str(SWITCH),action,str(folder)),flush=True)
            run('curl','--noproxy','*','-fsS','--max-time','3','http://127.0.0.1:10086')
            cid=run('docker','inspect','-f','{{.Id}}','wgdashboard')
            assert (cid==original)==(action=='rollback')
            if action=='finish':
                with sqlite3.connect(folder/'data/db/wgdashboard_job.db') as c:
                    assert c.execute('SELECT Value FROM PeerJobs').fetchone()==(day.replace('19:52','22:52'),)
                run(sys.executable,str(SWITCH),'finish',str(folder))
                run(sys.executable,str(SWITCH),'rollback',str(folder),expected=False)


if __name__=='__main__':
    main()
