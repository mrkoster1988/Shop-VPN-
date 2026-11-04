"""Private WireGuard control agent. Bind loopback or a private HTTPS network only."""
import asyncio
import base64
import hmac
import ipaddress
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
DATA = Path(os.getenv('WG_DATA_DIR', '/var/lib/pushok-wg'))
INTERFACE = os.getenv('WG_INTERFACE', 'wg-pushok')
NETWORK = ipaddress.ip_network(os.getenv('WG_NETWORK', '10.77.0.0/24'))
TOKEN = os.getenv('WG_AGENT_TOKEN', '')
LOCK = asyncio.Lock()


def conn():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA / 'peers.db')
    connection.row_factory = sqlite3.Row
    return connection


def cipher():
    return Fernet(os.environ['WG_ENCRYPTION_KEY'].encode())


def wg(*args):
    # Argument vector only; never invoke a shell. Do not log stdout or keys.
    return subprocess.run(['wg', *args], check=True, capture_output=True, text=True, timeout=10).stdout.strip()


def apply(peer):
    wg('set', INTERFACE, 'peer', peer['public_key'], 'allowed-ips', peer['address'] + '/32')


def remove(peer):
    wg('set', INTERFACE, 'peer', peer['public_key'], 'remove')


async def reconcile():
    async with LOCK:
        with conn() as db:
            for peer in db.execute('SELECT * FROM peers').fetchall():
                if peer['expires'] > time.time():
                    await asyncio.to_thread(apply, peer)
                else:
                    await asyncio.to_thread(remove, peer)


async def worker():
    while True:
        try:
            await reconcile()
        except Exception:
            # Keep retrying; health and provision endpoints expose failures.
            pass
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 32 or not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', INTERFACE):
        raise RuntimeError('Configure a strong WG_AGENT_TOKEN and valid interface')
    if NETWORK.version != 4 or NETWORK.num_addresses > 65536 or NETWORK.num_addresses < 8:
        raise RuntimeError('Use a dedicated IPv4 /16 through /29 network')
    if not os.getenv('WG_ENDPOINT'):
        raise RuntimeError('Set WG_ENDPOINT to public-host:51820')
    cipher()
    with conn() as db:
        db.execute('CREATE TABLE IF NOT EXISTS peers(id TEXT PRIMARY KEY,address TEXT UNIQUE,private_key TEXT,public_key TEXT,expires INTEGER)')
    await reconcile()
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def auth(request: Request):
    if len(TOKEN) < 32 or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + TOKEN):
        raise HTTPException(403, 'Forbidden')


class Peer(BaseModel):
    expires: int = Field(gt=0)


@app.get('/health', dependencies=[Depends(auth)])
async def health():
    await asyncio.to_thread(wg, 'show', INTERFACE, 'public-key')
    return {'status': 'ok'}


@app.put('/peers/{peer_id}', dependencies=[Depends(auth)])
async def upsert(peer_id: uuid.UUID, data: Peer):
    if data.expires <= time.time():
        raise HTTPException(422, 'Expiry must be in the future')
    async with LOCK:
        with conn() as db:
            peer = db.execute('SELECT * FROM peers WHERE id=?', (str(peer_id),)).fetchone()
            if not peer:
                used = {p[0] for p in db.execute('SELECT address FROM peers')}
                # First usable IP belongs to the server. Never reuse expired peers' IPs.
                address = next((str(ip) for ip in list(NETWORK.hosts())[1:] if str(ip) not in used), None)
                if not address:
                    raise HTTPException(503, 'WireGuard subnet is full')
                private = X25519PrivateKey.generate()
                private_key = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
                public_key = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
                db.execute('INSERT INTO peers VALUES (?,?,?,?,?)',
                           (str(peer_id), address, cipher().encrypt(private_key.encode()).decode(), public_key, data.expires))
            else:
                db.execute('UPDATE peers SET expires=? WHERE id=?', (data.expires, str(peer_id)))
        # Committed key/IP intent survives crashes before kernel operations.
        with conn() as db:
            peer = dict(db.execute('SELECT * FROM peers WHERE id=?', (str(peer_id),)).fetchone())
        await asyncio.to_thread(apply, peer)
        server_key = await asyncio.to_thread(wg, 'show', INTERFACE, 'public-key')
        if not server_key or server_key == '(none)':
            raise HTTPException(503, 'WireGuard server has no public key')
        private_key = cipher().decrypt(peer['private_key'].encode()).decode()
        config = (f"[Interface]\nPrivateKey = {private_key}\nAddress = {peer['address']}/32\n"
                  f"DNS = {os.getenv('WG_DNS', '1.1.1.1')}\nMTU = 1280\n\n[Peer]\n"
                  f"PublicKey = {server_key}\nEndpoint = {os.environ['WG_ENDPOINT']}\n"
                  'AllowedIPs = 0.0.0.0/0, ::/0\nPersistentKeepalive = 25\n')
    return {'config': config}


@app.delete('/peers/{peer_id}', dependencies=[Depends(auth)])
async def revoke(peer_id: uuid.UUID):
    async with LOCK:
        with conn() as db:
            peer = db.execute('SELECT * FROM peers WHERE id=?', (str(peer_id),)).fetchone()
            db.execute('UPDATE peers SET expires=0 WHERE id=?', (str(peer_id),))
        if peer:
            await asyncio.to_thread(remove, peer)
    return {'ok': True}
